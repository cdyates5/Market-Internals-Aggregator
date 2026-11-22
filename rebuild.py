#!/usr/bin/env python3
"""Rebuild the S&P 500 Market Internals Aggregator dashboard from scratch.

Standalone: drop this file in an empty directory and run it. It downloads every
series, scores the six internals, runs the evidence tests and writes a single
self-contained HTML file.

    pip install numpy yfinance
    export FRED_API_KEY=your_key          # free from fred.stlouisfed.org
    python3 rebuild.py                    # -> sp500_market_internals_aggregator.html

Options:
    --out PATH      where to write the dashboard
    --cache DIR     where to keep the downloaded series (default: cache)
    --refetch       download again even if the cache is less than a day old

Data: Yahoo Finance (SPHB, SPLV, XLY, XLP, HYG, IEI, LQD, IEF, ^VIX, ^VIX3M,
DX-Y.NYB, ^GSPC) and FRED (BAA10Y, AAA10Y, BAMLH0A0HYM2, DGS10).
"""
import argparse
import datetime as dt
import json
import math
import os
import pickle
import re
import sys
import time
import urllib.parse
import urllib.request

import numpy as np

FRED_KEY = os.environ.get("FRED_API_KEY", "")

# -------------------------------------------------------------------------- downloads
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/122 Safari/537.36"}

# Yahoo tickers: key -> symbol
YAHOO = {
    "spx": "^GSPC",
    "sphb": "SPHB",     # S&P 500 High Beta
    "splv": "SPLV",     # S&P 500 Low Volatility
    "xly": "XLY",       # Consumer Discretionary (cyclicals)
    "xlp": "XLP",       # Consumer Staples (defensives)
    "hyg": "HYG",       # iShares HY corporate
    "iei": "IEI",       # iShares 3-7y Treasury
    "lqd": "LQD",       # iShares IG corporate
    "ief": "IEF",       # iShares 7-10y Treasury
    "vix": "^VIX",
    "vix3m": "^VIX3M",
    "dxy": "DX-Y.NYB",
}

# FRED series: key -> series_id
FRED = {
    "baa10y": "BAA10Y",            # Moody's Baa minus 10y Treasury, daily from 1986
    "aaa10y": "AAA10Y",            # Moody's Aaa minus 10y Treasury, daily from 1983
    "hyoas": "BAMLH0A0HYM2",       # ICE BofA HY OAS - history truncated by FRED licensing
    "dgs10": "DGS10",
}


# Wall-clock budget for the whole download, so one dead source cannot hang a CI run.
# fetch_all() sets it; outside fetch_all there is no limit.
DEADLINE = None


def _budget_left():
    return None if DEADLINE is None else DEADLINE - time.time()


def _retryable(e):
    """Retry throttling, server faults and network errors. Never retry a client error:
    a 400 or 401 from FRED means a bad key or bad request, and waiting will not fix it."""
    code = getattr(e, "code", None)
    if code is None:
        return True
    return code in (408, 429) or code >= 500


def _get(url, tries=6, timeout=60):
    """Yahoo throttles aggressively from shared IPs; wait it out rather than giving up."""
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            code = getattr(e, "code", None)
            if not _retryable(e):
                raise RuntimeError(f"{type(e).__name__} {code} (not retryable): {url.split('?')[0]}") from e
            wait = (60, 120, 180, 240, 300, 300)[min(i, 5)] if code == 429 else 5 + 10 * i
            left = _budget_left()
            if left is not None and left < wait + 20:
                raise RuntimeError(f"out of time after {type(e).__name__} {code or ''}") from e
            print(f"    retry {i+1}/{tries} after {wait}s ({type(e).__name__} {code or ''})", flush=True)
            time.sleep(wait)
    raise RuntimeError(f"failed {url[:90]}: {last}")


def yahoo(sym, start="1985-01-01", tries=5):
    """Daily close plus dividend-adjusted close, via yfinance.

    The plain v8/chart endpoint gets a hard 429 from shared cloud IPs; yfinance's
    cookie-and-crumb session goes through, so it is the only route used here.
    """
    import warnings

    import yfinance as yf

    warnings.filterwarnings("ignore")
    last = None
    for i in range(tries):
        try:
            df = yf.download(
                sym, start=start, progress=False, auto_adjust=False,
                actions=False, threads=False, timeout=60,
            )
            if df is None or df.empty:
                raise RuntimeError("empty frame")
            if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
                df.columns = df.columns.get_level_values(0)
            out = {}
            for ts, r in df.iterrows():
                c, a = r.get("Close"), r.get("Adj Close")
                if c is None or a is None or c != c or a != a:
                    continue
                out[ts.strftime("%Y-%m-%d")] = {"c": float(c), "a": float(a)}
            if not out:
                raise RuntimeError("no usable rows")
            return out
        except Exception as e:  # noqa: BLE001
            last = e
            wait = (20, 60, 120, 240, 300)[min(i, 4)]
            left = _budget_left()
            if left is not None and left < wait + 20:
                raise RuntimeError(f"out of time fetching {sym}: {last}") from e
            print(f"    {sym}: retry {i+1}/{tries} after {wait}s ({type(e).__name__} {str(e)[:60]})", flush=True)
            time.sleep(wait)
    raise RuntimeError(f"failed {sym}: {last}")


def fred(sid, start="1980-01-01"):
    if not FRED_KEY:
        raise SystemExit("FRED_API_KEY is not set. Export it or add it as a repository secret; see README.")
    url = (
        "https://api.stlouisfed.org/fred/series/observations"
        f"?series_id={sid}&api_key={FRED_KEY}&file_type=json"
        f"&observation_start={start}&limit=100000&sort_order=asc"
    )
    obs = json.loads(_get(url))["observations"]
    return {o["date"]: float(o["value"]) for o in obs if o["value"] not in (".", "")}


# Series the aggregator cannot be scored without. Anything else may go missing.
REQUIRED = ["spx", "sphb", "splv", "xly", "xlp", "hyg", "iei", "vix", "vix3m", "dxy", "baa10y"]


def fetch_all():
    """Refresh every series, keeping yesterday's copy of anything that fails.

    On a shared runner Yahoo throttles unpredictably, so one bad ticker must not
    throw away a whole run. Each series falls back to the cached copy and the
    staleness is recorded, which check.py then enforces before publishing.
    """
    global DEADLINE
    DEADLINE = time.time() + float(os.environ.get("INT_FETCH_BUDGET", 18 * 60))
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, "raw.pkl")
    old = {}
    if os.path.exists(path):
        try:
            with open(path, "rb") as f:
                old = pickle.load(f)
            print(f"  cache found, built {old.get('_built', '?')}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"  cache unreadable ({type(e).__name__}), starting fresh", flush=True)

    data, stale, failed = {}, [], []
    for kind, table, getter, pause in (("yahoo", YAHOO, yahoo, 2), ("fred", FRED, fred, 0.4)):
        for k, sym in table.items():
            try:
                got = getter(sym)
                if not got:
                    raise RuntimeError("empty series")
                data[k] = got
                d = sorted(got)
                print(f"  {kind:5s} {k:6s} {sym:14s} n={len(d):6d}  {d[0]} -> {d[-1]}", flush=True)
            except Exception as e:  # noqa: BLE001
                if k in old and old[k]:
                    data[k] = old[k]
                    d = sorted(old[k])
                    stale.append(k)
                    print(f"  {kind:5s} {k:6s} {sym:14s} FAILED ({type(e).__name__}) "
                          f"- kept cached copy to {d[-1]}", flush=True)
                else:
                    failed.append(k)
                    print(f"  {kind:5s} {k:6s} {sym:14s} FAILED ({type(e).__name__}) "
                          f"- no cached copy", flush=True)
            time.sleep(pause)

    missing = [k for k in REQUIRED if k not in data or not data[k]]
    if missing:
        raise SystemExit(f"cannot build: required series unavailable with no cache: {', '.join(missing)}")
    if failed:
        print(f"  note: optional series dropped this run: {', '.join(failed)}", flush=True)

    data["_built"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    data["_stale"] = stale
    with open(path, "wb") as f:
        pickle.dump(data, f)
    print(f"cached -> {path}" + (f"  ({len(stale)} series stale: {', '.join(stale)})" if stale else ""))
    return stale

# -------------------------------------------------------------------------- series plumbing
CACHE = os.environ.get("INT_CACHE", "cache")

# Each internal: a ratio of two adjusted price series, or a level.
# "dirn" is the sign that means risk-on: +1 = higher is risk-on, -1 = lower is risk-on.
SPEC = [
    dict(
        key="beta", name="High beta vs low beta", fam="Equity risk appetite",
        num="sphb", den="splv", dirn=+1, unit="ratio",
        src="SPHB / SPLV (adjusted)",
        what="S&P 500 High Beta index against S&P 500 Low Volatility index. The purest "
             "read on whether the index is being led by its most or least cyclical members.",
    ),
    dict(
        key="cycdef", name="Cyclicals vs defensives", fam="Equity risk appetite",
        num="xly", den="xlp", dirn=+1, unit="ratio",
        src="XLY / XLP (adjusted)",
        what="Consumer Discretionary against Consumer Staples. The classic sector read on "
             "whether the market is paying for growth or for safety.",
    ),
    dict(
        key="credit", name="Credit spreads", fam="Credit",
        lvl="baa10y", dirn=-1, unit="pp",
        src="FRED BAA10Y (Moody's Baa minus 10y Treasury)",
        what="Moody's Baa corporate yield over the 10-year Treasury. Widening spreads are "
             "the single most reliable internal warning in the set.",
    ),
    dict(
        key="bondra", name="Bond market risk appetite", fam="Credit",
        num="hyg", den="iei", dirn=+1, unit="ratio",
        src="HYG / IEI (adjusted, total return)",
        what="High-yield corporate bonds against 3-7 year Treasuries. Prices the same "
             "risk appetite as credit spreads but from the total-return side.",
    ),
    dict(
        key="vixts", name="VIX term structure", fam="Volatility",
        num="vix3m", den="vix", dirn=+1, unit="ratio",
        src="VIX3M / VIX",
        what="Three-month implied volatility over spot. Above 1 the curve is in contango, "
             "the normal calm state; below 1 it is inverted and the market is paying up "
             "for immediate protection.",
    ),
    dict(
        key="dxy", name="US dollar", fam="Dollar",
        lvl="dxy", dirn=-1, unit="index",
        src="DX-Y.NYB (ICE US Dollar Index)",
        what="A rising dollar tightens global financial conditions and is usually a "
             "headwind for the index; a falling dollar loosens them.",
    ),
]
BYKEY = {s["key"]: s for s in SPEC}
KEYS = [s["key"] for s in SPEC]


def load(cache=None):
    with open(os.path.join(cache or CACHE, "raw.pkl"), "rb") as f:
        return pickle.load(f)


def adj(raw, k):
    return {d: v["a"] for d, v in raw[k].items()}


def close(raw, k):
    return {d: v["c"] for d, v in raw[k].items()}


def ffill_on(dates, src, max_stale=7):
    """Project a dated dict onto `dates`, carrying the last value forward a few days."""
    out = np.full(len(dates), np.nan)
    sd = sorted(src)
    j, last, lastd = 0, np.nan, None
    for i, d in enumerate(dates):
        while j < len(sd) and sd[j] <= d:
            last, lastd = src[sd[j]], sd[j]
            j += 1
        if lastd is None:
            continue
        gap = (dt.date.fromisoformat(d) - dt.date.fromisoformat(lastd)).days
        if gap <= max_stale:
            out[i] = last
    return out


def build(raw, start=None):
    """Align everything on S&P 500 trading days and return the internals' raw levels."""
    spx_a = adj(raw, "spx")
    spx_c = close(raw, "spx")
    dates = sorted(spx_a)
    if start:
        dates = [d for d in dates if d >= start]
    px = np.array([spx_c[d] for d in dates])        # index level, for display
    tr = np.array([spx_a[d] for d in dates])        # adjusted, for returns

    lv = {}
    for s in SPEC:
        if "lvl" in s:
            lv[s["key"]] = ffill_on(dates, close(raw, s["lvl"]) if s["lvl"] == "dxy" else raw[s["lvl"]])
        else:
            n = ffill_on(dates, adj(raw, s["num"]), max_stale=4)
            d_ = ffill_on(dates, adj(raw, s["den"]), max_stale=4)
            with np.errstate(invalid="ignore", divide="ignore"):
                lv[s["key"]] = n / d_
    # companion series shown alongside, not scored
    lqd = ffill_on(dates, adj(raw, "lqd"), max_stale=4)
    ief = ffill_on(dates, adj(raw, "ief"), max_stale=4)
    with np.errstate(invalid="ignore", divide="ignore"):
        lv["_lqdief"] = lqd / ief
    lv["_hyoas"] = ffill_on(dates, raw["hyoas"])
    lv["_vixsprd"] = ffill_on(dates, close(raw, "vix3m"), 4) - ffill_on(dates, close(raw, "vix"), 4)
    return dates, px, tr, lv


def sma(x, n):
    out = np.full(len(x), np.nan)
    ok = ~np.isnan(x)
    c = np.where(ok, x, 0.0).cumsum()
    k = ok.cumsum()
    for i in range(n - 1, len(x)):
        lo = i - n
        num = c[i] - (c[lo] if lo >= 0 else 0.0)
        cnt = k[i] - (k[lo] if lo >= 0 else 0)
        if cnt >= n * 0.8:
            out[i] = num / cnt
    return out


def score_on(lv_key, x, n):
    """Risk-on flag: above the trailing average for 'higher is risk-on', below for the rest."""
    m = sma(x, n)
    d = BYKEY[lv_key]["dirn"]
    with np.errstate(invalid="ignore"):
        on = (x > m) if d > 0 else (x < m)
    on = on.astype(float)
    on[np.isnan(x) | np.isnan(m)] = np.nan
    return on


def fwd(tr, h):
    """Forward total return over h sessions, NaN where it runs off the end."""
    out = np.full(len(tr), np.nan)
    if h < len(tr):
        out[:-h] = tr[h:] / tr[:-h] - 1.0
    return out


def maxdd(tr, h):
    """Worst peak-to-trough fall over the next h sessions."""
    out = np.full(len(tr), np.nan)
    for i in range(len(tr) - h):
        w = tr[i:i + h + 1]
        out[i] = (w / np.maximum.accumulate(w) - 1.0).min()
    return out

# -------------------------------------------------------------------------- model
MA = 100          # trailing window each internal is measured against
HI = 5            # score at or above this is risk-on
CONF_DOWN = 2     # sessions needed to confirm a move DOWN a state (warn quickly)
CONF_UP = 5       # sessions needed to confirm a move UP a state (slow to call the all-clear)
HZ = [("1m", 21), ("3m", 63), ("6m", 126), ("12m", 252)]
STATE_NAME = {2: "Risk-on", 1: "Fragile", 0: "Washout"}

# Longer, narrower panels used only to ask whether the pattern survives more history
PANELS = [
    ("All six", ["beta", "cycdef", "credit", "bondra", "vixts", "dxy"]),
    ("Five, no high/low beta", ["cycdef", "credit", "bondra*", "vixts", "dxy"]),
    ("Four, no beta or VIX curve", ["cycdef", "credit", "bondra*", "dxy"]),
    ("Three, longest span", ["cycdef", "credit", "dxy"]),
]


def r4(x, nd=4):
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return None
    return round(float(x), nd)


def arr(a, nd=4):
    return [r4(v, nd) for v in a]


def boot_p(flag, y, nb=3000, bl=252, seed=11):
    """Circular block bootstrap. Keeps the autocorrelation of overlapping forward windows."""
    ok = ~np.isnan(y) & ~np.isnan(flag)
    fl, yy = flag[ok].astype(bool), y[ok]
    if fl.sum() < 30 or (~fl).sum() < 30:
        return None
    obs = yy[fl].mean() - yy[~fl].mean()
    n = len(fl)
    rng = np.random.default_rng(seed)
    idx = np.arange(n)
    nblk = int(np.ceil(n / bl))
    hits = used = 0
    for _ in range(nb):
        st = rng.integers(0, n, nblk)
        order = np.concatenate([(idx[s:s + bl] if s + bl <= n else np.r_[idx[s:], idx[:s + bl - n]])
                                for s in st])[:n]
        sh = fl[order]
        if sh.sum() < 10 or (~sh).sum() < 10:
            continue
        used += 1
        if abs(yy[sh].mean() - yy[~sh].mean()) >= abs(obs):
            hits += 1
    return (hits + 1) / (used + 1) if used else None


def confirm(raw_state, valid, kdown=CONF_DOWN, kup=CONF_UP):
    """Publish a state change once the raw read has held long enough.

    Asymmetric on purpose: a deterioration is published after two sessions so the
    warning is prompt, an improvement only after five so the all-clear is slow.
    """
    out = np.full(len(raw_state), np.nan)
    cur = None
    prev = None
    run = 0
    for i in range(len(raw_state)):
        if not valid[i]:
            continue
        s = raw_state[i]
        if cur is None:
            cur, prev, run = s, s, 1
            out[i] = cur
            continue
        run = run + 1 if s == prev else 1
        prev = s
        k = kdown if s < cur else kup
        if s != cur and run >= k:
            cur = s
        out[i] = cur
    return out


def mstat(mask, y):
    m = mask & ~np.isnan(y)
    return (float(y[m].mean()), int(m.sum())) if m.sum() else (None, 0)


def compute(cache=None):
    raw = load(cache)
    dates, px, tr, lv = build(raw)
    n = len(dates)
    years = np.array([int(d[:4]) for d in dates])

    # ---- forward outcomes -------------------------------------------------
    FW = {k: fwd(tr, h) for k, h in HZ}
    dd = {k: maxdd(tr, h) for k, h in (("1m", 21), ("3m", 63), ("6m", 126))}
    lr = np.r_[np.nan, np.diff(np.log(tr))]
    vol = np.full(n, np.nan)
    for i in range(n - 21):
        vol[i] = np.nanstd(lr[i + 1:i + 22]) * np.sqrt(252) * 100

    # ---- score every internal --------------------------------------------
    on = {k: score_on(k, lv[k], MA) for k in KEYS}
    ma = {k: sma(lv[k], MA) for k in KEYS}
    # bond risk appetite extended back with LQD/IEF, used only in the longer panels
    on["bondra*"] = np.where(np.isnan(on["bondra"]), score_on("bondra", lv["_lqdief"], MA), on["bondra"])

    def panel(keys):
        mat = np.vstack([on[k] for k in keys])
        valid = ~np.isnan(mat).any(axis=0)
        sc = np.where(valid, np.nansum(mat, axis=0), np.nan)
        return sc, valid, len(keys)

    score, valid, K = panel(KEYS)
    i0 = int(np.argmax(valid))
    rawst = np.where(score >= HI, 2.0, np.where(score == 0, 0.0, 1.0))
    state = confirm(rawst, valid)

    # ---- current read -----------------------------------------------------
    last = n - 1
    cur_state = int(state[last])
    since = last
    while since > i0 and state[since - 1] == state[last]:
        since -= 1
    legs_now = []
    for s in SPEC:
        k = s["key"]
        o = on[k]
        d_since = last
        while d_since > 0 and not np.isnan(o[d_since - 1]) and o[d_since - 1] == o[last]:
            d_since -= 1
        # signed so a positive gap always means risk-on, whichever way the internal runs
        gap = (lv[k][last] / ma[k][last] - 1.0) * 100 * s["dirn"] if ma[k][last] else None
        legs_now.append(dict(
            key=k, on=int(o[last]) if not np.isnan(o[last]) else None,
            lvl=r4(lv[k][last]), ma=r4(ma[k][last]), gap=r4(gap, 2),
            since=dates[d_since], days=int(last - d_since + 1),
        ))

    # ---- evidence 1: per-internal screen against risk ---------------------
    screen = []
    for s in SPEC:
        k = s["key"]
        o = on[k]
        m1, m0 = o == 1, o == 0
        row = dict(key=k, n_on=int(m1.sum()), n_off=int(m0.sum()))
        for lab, y in (("dd", dd["1m"]), ("vol", vol), ("r12", FW["12m"])):
            a, _ = mstat(m1, y)
            b, _ = mstat(m0, y)
            row[lab + "_on"], row[lab + "_off"] = r4(a, 4), r4(b, 4)
            row[lab + "_gap"] = r4((a - b) if (a is not None and b is not None) else None, 4)
        row["p_dd"] = r4(boot_p(o, dd["1m"]), 4)
        row["p_vol"] = r4(boot_p(o, vol), 4)
        row["p_r12"] = r4(boot_p(o, FW["12m"]), 4)
        pd_, pv = row["p_dd"], row["p_vol"]
        best = min([p for p in (pd_, pv) if p is not None], default=1.0)
        row["grade"] = "pass" if best <= 0.05 else ("weak" if best <= 0.20 else "none")
        row["first"] = next((dates[i] for i in range(n) if not np.isnan(o[i])), None)
        screen.append(row)

    # ---- evidence 2: three-state table on the full six -------------------
    def state_rows(sc, vd, kk, st=None):
        if st is None:
            st = confirm(np.where(sc >= kk, 2.0, np.where(sc == 0, 0.0, 1.0)), vd)
        rows = []
        for v in (2, 1, 0):
            m = st == v
            row = dict(state=STATE_NAME[v], n=int(m.sum()),
                       pct=r4(m.sum() / max(vd.sum(), 1) * 100, 1))
            for lab, y in (("dd1", dd["1m"]), ("dd3", dd["3m"]), ("vol", vol)):
                row[lab] = r4(mstat(m, y)[0], 4)
            for hk, _ in HZ:
                row[hk] = r4(mstat(m, FW[hk])[0], 4)
            row["hit12"] = r4(float((FW["12m"][m & ~np.isnan(FW["12m"])] > 0).mean())
                              if (m & ~np.isnan(FW["12m"])).sum() else None, 3)
            rows.append(row)
        base = dict(state="All days", n=int(vd.sum()), pct=100.0)
        for lab, y in (("dd1", dd["1m"]), ("dd3", dd["3m"]), ("vol", vol)):
            base[lab] = r4(mstat(vd, y)[0], 4)
        for hk, _ in HZ:
            base[hk] = r4(mstat(vd, FW[hk])[0], 4)
        base["hit12"] = r4(float((FW["12m"][vd & ~np.isnan(FW["12m"])] > 0).mean()), 3)
        rows.append(base)
        ro = (st == 2).astype(float)
        ro[~vd] = np.nan
        wo = (st == 0).astype(float)
        wo[~vd] = np.nan
        return rows, dict(
            p_dd=r4(boot_p(ro, dd["1m"]), 4), p_vol=r4(boot_p(ro, vol), 4),
            p_r12=r4(boot_p(ro, FW["12m"]), 4), p_wo_dd=r4(boot_p(wo, dd["1m"]), 4),
        )

    st_rows, st_p = state_rows(score, valid, HI, state)

    # ---- evidence 3: the same test on longer, narrower panels -------------
    pan_rows = []
    for nm, keys in PANELS:
        sc, vd, kk = panel(keys)
        hi = kk if kk < 5 else HI
        st = confirm(np.where(sc >= hi, 2.0, np.where(sc == 0, 0.0, 1.0)), vd)
        ro = (st == 2).astype(float)
        ro[~vd] = np.nan
        r = dict(name=nm, k=kk, start=dates[int(np.argmax(vd))], n=int(vd.sum()), hi=hi)
        for v, tag in ((2, "on"), (1, "fr"), (0, "wo")):
            m = st == v
            r[f"dd_{tag}"] = r4(mstat(m, dd["1m"])[0], 4)
            r[f"vol_{tag}"] = r4(mstat(m, vol)[0], 4)
            r[f"r12_{tag}"] = r4(mstat(m, FW["12m"])[0], 4)
        r["p_dd"] = r4(boot_p(ro, dd["1m"]), 4)
        r["p_vol"] = r4(boot_p(ro, vol), 4)
        r["p_r12"] = r4(boot_p(ro, FW["12m"]), 4)
        pan_rows.append(r)

    # ---- evidence 4: era split on the four-internal panel -----------------
    sc4, vd4, k4 = panel(PANELS[2][1])
    st4 = confirm(np.where(sc4 >= HI, 2.0, np.where(sc4 == 0, 0.0, 1.0)), vd4)
    st4 = confirm(np.where(sc4 >= k4 - 1, 2.0, np.where(sc4 == 0, 0.0, 1.0)), vd4)
    era_rows = []
    for lab, m in (("2002-2009", years <= 2009), ("2010-2017", (years >= 2010) & (years <= 2017)),
                   ("2018-2026", years >= 2018)):
        r = dict(era=lab)
        for v, tag in ((2, "on"), (0, "wo")):
            mm = m & (st4 == v)
            r[f"n_{tag}"] = int(mm.sum())
            r[f"dd_{tag}"] = r4(mstat(mm, dd["1m"])[0], 4)
            r[f"vol_{tag}"] = r4(mstat(mm, vol)[0], 4)
            r[f"r12_{tag}"] = r4(mstat(mm, FW["12m"])[0], 4)
        mm = m & (st4 == 1)
        r["n_fr"], r["dd_fr"] = int(mm.sum()), r4(mstat(mm, dd["1m"])[0], 4)
        r["vol_fr"], r["r12_fr"] = r4(mstat(mm, vol)[0], 4), r4(mstat(mm, FW["12m"])[0], 4)
        era_rows.append(r)

    # ---- evidence 5: divergence - price high, internals weak --------------
    roll = np.full(n, np.nan)
    for i in range(251, n):
        roll[i] = px[i - 251:i + 1].max()
    near = px >= roll * 0.99
    div_rows = []
    for lab, m in (
        ("Within 1% of the 52-week high, score 5-6", near & valid & (score >= HI)),
        ("Within 1% of the 52-week high, score 0-2", near & valid & (score <= 2)),
        ("Within 1% of the 52-week high, any score", near & valid),
        ("More than 1% below the high", (~near) & valid),
    ):
        r = dict(case=lab, n=int(m.sum()))
        for hk in ("1m", "3m", "6m"):
            r[hk] = r4(mstat(m, FW[hk])[0], 4)
        r["dd3"] = r4(mstat(m, dd["3m"])[0], 4)
        r["dd6"] = r4(mstat(m, dd["6m"])[0], 4)
        r["vol"] = r4(mstat(m, vol)[0], 4)
        div_rows.append(r)

    # ---- state change log -------------------------------------------------
    log = []
    for i in range(i0 + 1, n):
        if np.isnan(state[i]) or np.isnan(state[i - 1]) or state[i] == state[i - 1]:
            continue
        e = dict(date=dates[i], frm=STATE_NAME[int(state[i - 1])], to=STATE_NAME[int(state[i])],
                 score=int(score[i]), px=r4(px[i], 2))
        for hk in ("1m", "3m", "12m"):
            e[hk] = r4(FW[hk][i], 4)
        e["dd3"] = r4(dd["3m"][i], 4)
        log.append(e)
    log = log[::-1][:28]

    # ---- series for the charts -------------------------------------------
    # aggregator: daily over the full-six window
    sl = slice(i0, n)
    legs = []
    # detail charts: each internal's own full history, sampled at week-end
    wk = [i for i in range(n) if i == n - 1 or dates[i][:4] + str(dt.date.fromisoformat(dates[i]).isocalendar()[1])
          != dates[i + 1][:4] + str(dt.date.fromisoformat(dates[i + 1]).isocalendar()[1])]
    for s in SPEC:
        k = s["key"]
        sr = next(r for r in screen if r["key"] == k)
        legs.append(dict(
            key=k, name=s["name"], fam=s["fam"], src=s["src"], what=s["what"],
            unit=s["unit"], dirn=s["dirn"], grade=sr["grade"],
            on=[None if np.isnan(v) else int(v) for v in on[k][sl]],
            w_lvl=arr([lv[k][i] for i in wk]),
            w_ma=arr([ma[k][i] for i in wk]),
            w_on=[None if np.isnan(on[k][i]) else int(on[k][i]) for i in wk],
        ))
    extras = dict(
        lqdief=arr([lv["_lqdief"][i] for i in wk]),
        hyoas=arr([lv["_hyoas"][i] for i in wk]),
        vixsprd=arr([lv["_vixsprd"][i] for i in wk]),
    )

    meta = dict(
        built=dt.datetime.now(dt.timezone.utc).strftime("%d %b %Y %H:%M UTC"),
        # series that failed to refresh on the last fetch and are running on cached values
        stale=sorted(raw.get("_stale") or []),
        asof=dates[last], start=dates[i0], n_int=K, ma=MA, hi=HI,
        conf_down=CONF_DOWN, conf_up=CONF_UP,
        n_days=int(valid.sum()),
        evid_start=dates[i0],
        long_start=pan_rows[-1]["start"],
    )
    now = dict(
        score=int(score[last]), raw_state=int(rawst[last]), state=cur_state,
        state_name=STATE_NAME[cur_state], since=dates[since],
        days=int(last - since + 1), px=r4(px[last], 2),
        legs={l["key"]: l for l in legs_now},
        n_on=int(score[last]), n_int=K,
    )
    return dict(
        meta=meta, now=now,
        dates=[dates[i] for i in range(i0, n)],
        px=arr(px[sl], 2),
        score=[None if np.isnan(v) else int(v) for v in score[sl]],
        state=[None if np.isnan(v) else int(v) for v in state[sl]],
        wdates=[dates[i] for i in wk],
        wpx=arr([px[i] for i in wk], 2),
        legs=legs, extras=extras,
        screen=screen, st_rows=st_rows, st_p=st_p,
        pan_rows=pan_rows, era_rows=era_rows, div_rows=div_rows, log=log,
    )

# -------------------------------------------------------------------------- page
TEMPLATE = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>S&amp;P 500 Market Internals Aggregator</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<!-- House font is Bahnschrift (Light), a Windows system font; Barlow is the web fallback for machines without it. -->
<link href="https://fonts.googleapis.com/css2?family=Barlow:wght@300;400;500;600&display=swap" rel="stylesheet">
<meta name="color-scheme" content="light">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
:root{
  --paper:#FFFFFF; --card:#FFFFFF; --ink:#000000; --body:#262626; --muted:#808080; --hair:#D6E3F0;
  --navy:#1F4E79; --yellow:#FFC000; --hl:#FFFF00; --grid:#BDD7EE; --pale:#DEEBF7;
  --grey:#A5A5A5; --green:#70AD47; --blue:#4472C4; --accent:#ED7D31;
  --font:Bahnschrift,"Barlow","Segoe UI",Arial,sans-serif;
  color-scheme:light;
}
*{box-sizing:border-box}
html{background:#FFFFFF;}
body{margin:0;background:#FFFFFF;color:var(--body);font-family:var(--font);font-weight:300;line-height:1.45;-webkit-font-smoothing:antialiased;}
b,strong{font-weight:500;}
.wrap{max-width:1220px;margin:0 auto;padding:36px 26px 0;}
header{border-bottom:2px solid var(--navy);padding-bottom:18px;margin-bottom:26px;}
.kicker{font-size:13px;font-weight:400;letter-spacing:.06em;text-transform:uppercase;color:var(--navy);margin:0 0 14px;}
h1{font-weight:300;font-size:clamp(28px,4.2vw,44px);line-height:1.18;margin:0 0 14px;color:var(--ink);letter-spacing:0;}
h1 .hl{background:var(--hl);padding:0 .14em;-webkit-box-decoration-break:clone;box-decoration-break:clone;}
.sub{font-size:16px;color:#404040;max-width:80ch;margin:0 0 16px;}
.meta{display:flex;flex-wrap:wrap;gap:6px 22px;font-size:12.5px;color:var(--muted);}
.meta b{color:var(--ink);font-weight:400;}
/* Panels: navy header band, message in black, chart title grey + underlined + centred, legend under the chart */
.card{background:#FFFFFF;border:1px solid var(--hair);border-radius:0;padding:0 20px 16px;margin-bottom:22px;min-width:0;}
.eyebrow{background:var(--navy);color:#FFFFFF;font-size:12.5px;font-weight:400;letter-spacing:.02em;margin:0 -20px 14px;padding:7px 20px;display:flex;align-items:center;gap:10px;}
.livebadge{margin-left:auto;font-size:10.5px;font-weight:500;letter-spacing:.05em;background:var(--yellow);color:var(--navy);padding:1px 7px;border-radius:2px;white-space:nowrap;}
.livebadge::before{content:"";display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--navy);margin-right:5px;vertical-align:middle;}
.card h3{font-weight:300;font-size:18px;color:var(--muted);text-align:center;text-decoration:underline;text-decoration-thickness:1px;text-underline-offset:4px;margin:2px 0 10px;letter-spacing:0;}
.deck{font-size:15px;color:var(--ink);margin:0 0 12px;line-height:1.4;}
.deck p{margin:0 0 8px;} .deck p:last-child{margin:0;}
.deck b{font-weight:500;}
.chartbox{position:relative;width:100%;}
.legendrow{display:flex;flex-wrap:wrap;justify-content:center;gap:4px 16px;font-size:12.5px;color:var(--muted);margin:8px 0 4px;}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px;vertical-align:middle;}
.sq{border-radius:0;}
.cap{font-size:11.5px;color:var(--muted);margin:12px 0 0;padding-top:8px;border-top:1px solid #EDEDED;line-height:1.5;}
.cap b{color:var(--navy);font-weight:400;}
.cols{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:22px;}
.cols .card{margin-bottom:22px;}
@media(max-width:900px){.cols{grid-template-columns:1fr;gap:0;}}
/* current-read tiles */
.kpis{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:2px 0 14px;}
@media(max-width:900px){.kpis{grid-template-columns:repeat(2,minmax(0,1fr));}}
.kpi{border:1px solid #D9D9D9;border-radius:0;padding:10px 12px;background:#FFFFFF;min-width:0;}
.kpi.now{border:2px solid var(--navy);padding:9px 11px;}
.kpi .lab{font-size:11.5px;font-weight:400;letter-spacing:.06em;text-transform:uppercase;color:var(--navy);}
.kpi .val{font-size:30px;font-weight:300;color:var(--ink);line-height:1.2;margin-top:3px;font-variant-numeric:tabular-nums;}
.kpi .val small{font-size:14px;color:var(--muted);}
.kpi .note{font-size:12.5px;color:#404040;margin-top:5px;line-height:1.45;}
.pill{display:inline-block;font-size:10.5px;letter-spacing:.02em;padding:2px 7px;border-radius:2px;white-space:nowrap;font-weight:400;vertical-align:middle;}
.pill.number{background:var(--navy);color:#FFFFFF;}
.pill.neu{background:var(--pale);color:var(--navy);}
.pill.none{background:transparent;color:var(--muted);border:1px dashed #BFBFBF;}
.pill.bad{background:#C00000;color:#FFFFFF;}
.pill.good{background:#548235;color:#FFFFFF;}
/* window selector and internal picker */
.bar{display:flex;align-items:center;gap:6px;margin:0 0 14px;position:sticky;top:0;background:#FFFFFF;padding:8px 0;z-index:5;flex-wrap:wrap;}
.bar span{font-size:12.5px;color:var(--navy);text-transform:uppercase;letter-spacing:.06em;font-weight:400;margin-right:6px;}
.bar span.gap{margin-left:18px;}
button.b{font-family:var(--font);font-weight:400;font-size:12px;padding:3px 12px;border:1px solid var(--grid);background:#FFFFFF;color:var(--navy);border-radius:2px;cursor:pointer;}
button.b.on{background:var(--navy);color:#FFFFFF;border-color:var(--navy);}
.picker{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 12px;}
/* tables */
.tblwrap{overflow-x:auto;}
table{width:100%;border-collapse:collapse;font-size:12.5px;}
th{background:var(--navy);color:#FFFFFF;font-size:11.5px;font-weight:400;letter-spacing:.01em;text-align:right;padding:6px 8px;border:0;vertical-align:bottom;}
td{padding:7px 8px;border-bottom:1px solid var(--pale);vertical-align:top;line-height:1.35;text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums;color:var(--body);}
th:first-child,td:first-child{text-align:left;}
th.t{text-align:left;}
td.t{text-align:left;white-space:normal;font-size:13.5px;font-weight:400;color:var(--ink);}
td.t small{display:block;color:var(--muted);font-size:11.5px;font-weight:300;margin-top:2px;line-height:1.35;}
td.t.r{font-size:12.5px;font-weight:300;color:var(--body);}
td.wr{white-space:normal;text-align:left;line-height:1.7;}
#tE td.t,#tB td.t:first-child{min-width:190px;}
tr.grp td{background:var(--pale);color:var(--navy);font-weight:400;font-size:12.5px;text-align:left;}
tr.base td, tr.base td.t{color:var(--muted);font-weight:300;}
tr.pass td:first-child{box-shadow:inset 3px 0 0 var(--navy);}
.mut{color:var(--muted);}
.notes{font-size:13.5px;color:#404040;margin:0;padding-left:20px;}
.notes li{margin-bottom:7px;line-height:1.5;}
.notes b{color:var(--ink);}
.footbar{background:var(--navy);color:#FFFFFF;margin-top:26px;font-size:14px;font-weight:300;}
.footbar .in{max-width:1220px;margin:0 auto;padding:10px 26px;display:flex;justify-content:space-between;gap:16px;flex-wrap:wrap;}
@media(max-width:520px){.wrap{padding:28px 16px 0;}.footbar .in{padding:10px 16px;font-size:12.5px;}}
</style>
</head>
<body>
<div class="wrap">
<header>
  <p class="kicker">Systematic Macro · Market Internals</p>
  <h1><span class="hl">S&amp;P 500 Market Internals Aggregator</span></h1>
  <p class="sub">Six risk-appetite internals — high beta against low beta, cyclicals against defensives, credit spreads, bond-market risk appetite, the VIX term structure and the dollar — each scored risk-on or risk-off against its own trailing average and counted into one score, the same way the Buy Signal and Breadth Thrust Aggregators count signals. The evidence panels test what the score actually predicts, and report where it does not work.</p>
  <div class="meta" id="meta"></div>
</header>

<section class="card">
  <p class="eyebrow">Current read<span class="livebadge">LIVE · YAHOO · FRED</span></p>
  <div class="kpis" id="kpis"></div>
  <div class="deck" id="read"></div>
</section>

<div class="bar"><span>Chart window</span><button class="b" data-r="5Y">5Y</button><button class="b" data-r="10Y">10Y</button><button class="b on" data-r="MAX">Max</button>
  <span class="gap">Internals</span><button class="b on" data-m="all" id="mAll"></button><button class="b" data-m="core" id="mCore"></button></div>

<section class="card">
  <p class="eyebrow">Internals · aggregator</p>
  <p class="deck" id="sub1"></p>
  <h3>Market Internals Aggregator</h3>
  <div class="chartbox" id="cv1" style="height:760px"><canvas id="c1"></canvas></div>
  <div class="legendrow" id="lg1"></div>
  <p class="cap" id="fn1"></p>
</section>

<section class="card">
  <p class="eyebrow">Internals · board</p>
  <p class="deck">Where each internal stands at the latest close, against the trailing average that defines its state.</p>
  <h3>Internals board</h3>
  <div class="tblwrap"><table id="tB"></table></div>
  <p class="cap" id="fnB"></p>
</section>

<section class="card">
  <p class="eyebrow">Inputs · internal detail</p>
  <p class="deck">Each internal against its own trailing average, sampled at week-end over its full history. Shaded bands mark the stretches it reads risk-off.</p>
  <div class="picker" id="picker"></div>
  <h3 id="h3det"></h3>
  <div class="chartbox" style="height:520px"><canvas id="c2"></canvas></div>
  <div class="legendrow" id="lg2"></div>
  <p class="cap" id="fn2"></p>
</section>

<section class="card">
  <p class="eyebrow">Evidence · internal screen</p>
  <p class="deck" id="subE"></p>
  <h3>Which internals carry evidence</h3>
  <div class="tblwrap"><table id="tE"></table></div>
  <p class="cap" id="fnE"></p>
</section>

<div class="cols">
  <section class="card">
    <p class="eyebrow">Evidence · aggregator state</p>
    <p class="deck" id="subS1"></p>
    <h3>What the state predicts</h3>
    <div class="tblwrap"><table id="tS1"></table></div>
    <p class="cap" id="fnS1"></p>
  </section>
  <section class="card">
    <p class="eyebrow">Evidence · era stability</p>
    <p class="deck" id="subEr"></p>
    <h3>Risk signal by era</h3>
    <div class="tblwrap"><table id="tEr"></table></div>
    <p class="cap" id="fnEr"></p>
  </section>
</div>

<section class="card">
  <p class="eyebrow">Evidence · longer history</p>
  <p class="deck">High beta and the VIX curve only exist in recent data, so the six-internal aggregator cannot reach back past 2011. Dropping those internals buys history: the same test on narrower panels, each going back as far as its inputs allow.</p>
  <h3>Does it hold on more history?</h3>
  <div class="tblwrap"><table id="tS2"></table></div>
  <p class="cap" id="fnS2"></p>
</section>

<section class="card">
  <p class="eyebrow">Evidence · non-confirmation</p>
  <p class="deck">The classic use of internals: the index at a new high while the internals lag. Forward returns from those days against every other day near a high.</p>
  <h3>Price high, internals weak</h3>
  <div class="tblwrap"><table id="tD"></table></div>
  <p class="cap" id="fnD"></p>
</section>

<section class="card">
  <p class="eyebrow">State log</p>
  <p class="deck">Every published state change, most recent first, with what the index did next.</p>
  <h3>Recent state changes</h3>
  <div class="tblwrap"><table id="tL"></table></div>
</section>

<section class="card">
  <p class="eyebrow">Method and caveats</p>
  <ul class="notes" id="notes"></ul>
</section>
</div>
<div class="footbar"><div class="in"><span id="fb_date"></span><span>S&amp;P 500 Market Internals Aggregator</span><span>Beta · Sectors · Credit · Volatility · Dollar</span></div></div>

<script>
const P = /*@DATA*/null/*@END*/;
const M = P.meta, N = P.now;
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const MIN = '−';
const sgn = v => (v < 0 ? MIN : '+') + Math.abs(v).toFixed(1);
const pc = (v, d) => v == null ? '–' : (v < 0 ? MIN : '') + Math.abs(v * 100).toFixed(d === undefined ? 1 : d) + '%';
const pc0 = v => v == null ? '–' : Math.round(v * 100) + '%';
const num = v => v == null ? '–' : v.toLocaleString('en-US', {maximumFractionDigits: 0});
const MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const dfmt = s => s ? (+s.slice(8, 10)) + ' ' + MON[+s.slice(5, 7) - 1] + ' ' + s.slice(0, 4) : '–';
const longDate = s => (+s.slice(8,10)) + ' ' + ['January','February','March','April','May','June','July','August','September','October','November','December'][+s.slice(5,7)-1] + ' ' + s.slice(0,4);
const pfmt = p => p == null ? '–' : (p < 0.001 ? '<0.001' : p.toFixed(3));

// Deck palette: navy, yellow, grey, green, blue; orange for overlays. One typeface throughout.
const K = {ink:'#000000', navy:'#1F4E79', yellow:'#FFC000', grey:'#A5A5A5', green:'#70AD47', blue:'#4472C4',
  accent:'#ED7D31', mute:'#808080', grid:'#BDD7EE', pale:'#DEEBF7', hair:'#D6E3F0',
  up:'#548235', dn:'#C00000', spx:'#000000', score:'#1F4E79', scoreFill:'#1F4E79D9',
  bandBad:'#C0000021', bandGood:'#70AD4738', bandSoft:'#C0000016'};
const FONT = "Bahnschrift, 'Barlow', 'Segoe UI', Arial, sans-serif";
Chart.defaults.font.family = FONT; Chart.defaults.font.size = 12; Chart.defaults.font.weight = 300;
Chart.defaults.color = K.mute; Chart.defaults.animation = false;
Chart.defaults.scale.border.display = false;

// family colour, greyed where the internal fails the evidence screen
const FAM = {'Equity risk appetite':K.navy, 'Credit':K.green, 'Volatility':K.blue, 'Dollar':K.yellow};
const legColor = l => l.grade === 'none' ? K.grey : (FAM[l.fam] || K.navy);
const CORE = P.legs.filter(l => l.grade !== 'none').map(l => l.key);
const STN = {2:'Risk-on', 1:'Fragile', 0:'Washout'};
const STNUM = {'Risk-on':2, 'Fragile':1, 'Washout':0};
const STCLS = {2:'good', 1:'neu', 0:'bad'};

// ---------- header ----------
$('meta').innerHTML =
  '<span>Snapshot&nbsp; <b>' + esc(M.built) + '</b></span>' +
  '<span>Data to&nbsp; <b>' + dfmt(M.asof) + ' close</b></span>' +
  '<span>Live&nbsp; <b>Yahoo Finance prices</b> · <b>FRED credit spreads</b></span>' +
  '<span>Rule&nbsp; <b>' + M.ma + '-day average, ' + M.conf_down + '/' + M.conf_up + '-session confirmation</b></span>' +
  '<span>Evidence&nbsp; <b>six internals from ' + M.start.slice(0, 4) + ', narrower panels from ' + M.long_start.slice(0, 4) + '</b></span>' +
  ((M.stale && M.stale.length)
    ? '<span style="color:#C00000">Stale&nbsp; <b style="color:#C00000">' + esc(M.stale.join(', ')) +
      ' did not refresh on the last run</b></span>' : '');
$('fb_date').textContent = longDate(M.asof);

// ---------- current read ----------
(function () {
  const st = P.st_rows.find(r => r.state === STN[N.state]) || {};
  const nOff = N.n_int - N.score;
  const offNames = P.legs.filter(l => N.legs[l.key].on === 0).map(l => l.name);
  const onNames = P.legs.filter(l => N.legs[l.key].on === 1).map(l => l.name);
  const tiles = [
    ['Internals risk-on', N.score + ' <small>of ' + N.n_int + '</small>',
      nOff === 0 ? 'Every internal reads risk-on.' : nOff + ' of ' + N.n_int + ' read risk-off.'],
    ['Aggregator state', '<span style="font-size:24px">' + STN[N.state] + '</span>',
      'Since <b>' + dfmt(N.since) + '</b>, ' + N.days + ' session' + (N.days === 1 ? '' : 's') + '.'],
    ['Typical next month in this state', st.dd1 == null ? '–' : pc(st.dd1),
      'Average deepest fall; volatility averages ' + (st.vol == null ? '–' : st.vol.toFixed(0)) + '%.'],
    ['S&P 500', num(N.px), 'Close on ' + dfmt(M.asof) + '.'],
  ];
  $('kpis').innerHTML = tiles.map((k, i) =>
    '<div class="kpi' + (i < 2 ? ' now' : '') + '"><div class="lab">' + k[0] + '</div><div class="val">' +
    k[1] + '</div><div class="note">' + k[2] + '</div></div>').join('');

  const base = P.st_rows.find(r => r.state === 'All days');
  const wo = P.st_rows.find(r => r.state === 'Washout');
  const ro = P.st_rows.find(r => r.state === 'Risk-on');
  let p1 = '<p><b>' + N.score + ' of ' + N.n_int + ' internals read risk-on, so the aggregator is in its <b>' +
    STN[N.state].toLowerCase() + '</b> state, dated from ' + dfmt(N.since) + '.</b> ' +
    (onNames.length ? 'Risk-on: ' + esc(onNames.join(', ')) + '. ' : '') +
    (offNames.length ? 'Risk-off: ' + esc(offNames.join(', ')) + '.' : '') + '</p>';
  let p2 = '<p>Across ' + M.start.slice(0, 4) + '–' + M.asof.slice(0, 4) + ', the deepest fall over the following month ' +
    'averaged ' + pc(ro.dd1) + ' from risk-on days and ' + pc(wo.dd1) + ' from washout days, against ' + pc(base.dd1) +
    ' on all days. Realised volatility averaged ' + ro.vol.toFixed(0) + '% and ' + wo.vol.toFixed(0) + '% against ' +
    base.vol.toFixed(0) + '%. <b>That risk ordering is what this aggregator is for.</b></p>';
  let p3 = '<p><b>It is not a direction signal.</b> Twelve-month returns do not line up with the score: washout readings ' +
    'cluster at lows, so they are followed by ' + pc(wo['12m']) + ' on average against ' + pc(ro['12m']) +
    ' from risk-on days, and the gap between the two has inverted since 2018. The one return pattern that holds across ' +
    'every panel is that the <b>fragile</b> middle — internals weakening without capitulating — is the weakest state.</p>';
  $('read').innerHTML = p1 + p2 + p3;
})();

// ---------- chart plumbing ----------
const NICE = [10,20,30,50,75,100,150,200,300,400,500,600,700,800,900,1000,1200,1400,1600,1800,
  2000,2400,2800,3200,3600,4000,4500,5000,5500,6000,6500,7000,7500,8000,9000,10000,12000];
function yearTicks(labels) {
  const out = []; let y = null;
  for (let i = 0; i < labels.length; i++) {
    const yy = labels[i].slice(0, 4);
    if (yy !== y) { out.push(i); y = yy; }
  }
  const step = Math.ceil(out.length / 14);
  return out.filter((_, i) => i % step === 0);
}
const GRID = {color:K.grid, drawTicks:false, lineWidth:1};
function xAxis(labels) {
  const yt = yearTicks(labels);
  return {type:'category', grid:{display:false}, border:{display:false},
    ticks:{autoSkip:false, maxRotation:0, color:K.mute, padding:6, font:{size:12},
      callback(v) { return labels[v] ? labels[v].slice(0, 4) : ''; }},
    afterBuildTicks(ax) { ax.ticks = yt.map(i => ({value:i})); }};
}
function priceAxis(px, weight, title) {
  const v = px.filter(x => x != null), lo = Math.min(...v) * .96, hi = Math.max(...v) * 1.04;
  return {type:'logarithmic', position:'left', stack:'s', stackWeight:weight, min:lo, max:hi,
    grid:GRID, border:{display:false},
    title:{display:true, text:title || 'S&P 500 (log)', color:K.mute, font:{size:11}},
    ticks:{color:K.navy, padding:6, font:{size:11.5}, callback:v => num(v)},
    afterBuildTicks(ax) {
      let t = NICE.filter(n => n >= lo * 1.04 && n <= hi);
      if (t.length > 8) t = t.filter((_, k) => k % 2 === (t.length - 1) % 2);
      ax.ticks = t.map(n => ({value:n}));
    }};
}
function panelAxis(o) {
  const base = {type:'linear', position:'left', stack:'s', stackWeight:1, grid:GRID, border:{display:false},
    ticks:{color:K.navy, padding:6, font:{size:11.5}}};
  const out = Object.assign({}, base, o);
  out.ticks = Object.assign({}, base.ticks, o.ticks || {});
  if (out.title) out.title = Object.assign({color:K.mute, display:true, font:{size:11}}, out.title);
  return out;
}
const tipBase = {mode:'index', intersect:false, backgroundColor:K.navy, titleColor:'#FFFFFF',
  bodyColor:'#FFFFFF', footerColor:'#FFFFFF', cornerRadius:0,
  titleFont:{family:FONT, weight:500, size:12.5}, bodyFont:{family:FONT, weight:300, size:12},
  footerFont:{family:FONT, size:12, weight:500}, padding:10, boxWidth:9, boxHeight:9,
  usePointStyle:false, displayColors:true, callbacks:{}};
function legend(id, items) {
  $(id).innerHTML = items.map(it => '<span>' + (it.ln
    ? '<span style="display:inline-block;width:14px;height:' + (it.thin ? 2 : 3) + 'px;background:' + it.c + ';vertical-align:middle;margin-right:6px"></span>'
    : '<span class="dot sq" style="background:' + it.c + (it.band ? ';border:1px solid #D9D9D9;box-sizing:border-box' : '') + '"></span>') + it.t + '</span>').join('');
}
const line = (label, data, color, o) => Object.assign({
  label, data, borderColor:color, backgroundColor:color, borderWidth:1.4, pointRadius:0,
  tension:0, spanGaps:true, fill:false}, o || {});
const area = (label, data, fill, o) => Object.assign({
  label, data, borderColor:fill, backgroundColor:fill, borderWidth:1, pointRadius:0,
  tension:0, stepped:true, fill:'origin'}, o || {});

// runs of equal value, so lanes draw as few rectangles instead of one per session
function runs(a) {
  const out = []; let s = 0;
  for (let i = 1; i <= a.length; i++) {
    if (i === a.length || a[i] !== a[s]) { out.push([s, i - 1, a[s]]); s = i; }
  }
  return out;
}
// shade the whole plot where the state is risk-on or washout
const statePlugin = {
  id: 'statep',
  beforeDatasetsDraw(ch, args, o) {
    if (!o || !o.state) return;
    const {ctx, chartArea:a, scales:{x}} = ch;
    ctx.save();
    for (const [i0, i1, v] of runs(o.state)) {
      if (v !== 2 && v !== 0) continue;
      const xa = x.getPixelForValue(i0), xb = x.getPixelForValue(i1);
      ctx.fillStyle = v === 2 ? K.bandGood : (o.soft ? K.bandSoft : K.bandBad);
      ctx.fillRect(xa, a.top, Math.max(xb - xa, 1), a.bottom - a.top);
    }
    ctx.restore();
  }
};
// the strip of per-internal lanes under the score panel
const lanePlugin = {
  id: 'lanep',
  afterDatasetsDraw(ch, args, o) {
    if (!o || !o.legs || !o.legs.length) return;
    const {ctx, scales:{x}} = ch;
    const ax = ch.scales[o.axis];
    if (!ax) return;
    const top = ax.top, h = ax.bottom - ax.top, n = o.legs.length;
    const slot = h / n, pad = Math.min(3, slot * .18);
    ctx.save();
    ctx.textBaseline = 'middle';
    for (let j = 0; j < n; j++) {
      const l = o.legs[j], y = top + slot * j;
      ctx.fillStyle = K.pale;
      ctx.fillRect(ch.chartArea.left, y + slot * .5 - .5, ch.chartArea.right - ch.chartArea.left, 1);
      const col = legColor(l);
      for (const [i0, i1, v] of runs(l.on)) {
        if (v !== 1) continue;
        const xa = x.getPixelForValue(i0), xb = x.getPixelForValue(i1);
        ctx.fillStyle = col;
        ctx.fillRect(xa, y + pad, Math.max(xb - xa, 1), slot - pad * 2);
      }
      ctx.font = (l.grade === 'none' ? '300 ' : '400 ') + '11.5px ' + FONT;
      ctx.fillStyle = l.grade === 'none' ? K.mute : K.navy;
      ctx.textAlign = 'right';
      ctx.fillText(l.short, ch.chartArea.left - 8, y + slot * .5);
    }
    ctx.restore();
  }
};
Chart.register(statePlugin, lanePlugin);

// ---------- 1. aggregator ----------
let RANGE = 'MAX', MODE = 'all', c1 = null, c2 = null, DETAIL = P.legs[0].key;
P.legs.forEach(l => { l.short = l.name.replace('High beta vs low beta', 'High/low beta')
  .replace('Cyclicals vs defensives', 'Cyclicals/defensives')
  .replace('Bond market risk appetite', 'Bond risk appetite')
  .replace('VIX term structure', 'VIX curve').replace('US dollar', 'Dollar'); });
$('mAll').textContent = 'All ' + P.legs.length;
$('mCore').textContent = 'Screened ' + CORE.length;

function slice() {
  const n = P.dates.length;
  const k = RANGE === '5Y' ? 252 * 5 : RANGE === '10Y' ? 252 * 10 : n;
  return Math.max(0, n - k);
}
function activeLegs() { return MODE === 'all' ? P.legs : P.legs.filter(l => l.grade !== 'none'); }

function drawAgg() {
  const i0 = slice();
  const labels = P.dates.slice(i0), px = P.px.slice(i0);
  const legs = activeLegs();
  const K_ = legs.length;
  const sc = [];
  for (let i = i0; i < P.dates.length; i++) {
    let s = 0, ok = true;
    for (const l of legs) { const v = l.on[i]; if (v == null) { ok = false; break; } s += v; }
    sc.push(ok ? s : null);
  }
  const hi = MODE === 'all' ? M.hi : Math.max(2, K_ - 1);
  // state recomputed for the screened set so the shading matches the score on show
  const stRaw = sc.map(v => v == null ? null : (v >= hi ? 2 : v === 0 ? 0 : 1));
  const st = [];
  let cur = null, prev = null, run = 0;
  for (const s of stRaw) {
    if (s == null) { st.push(null); continue; }
    if (cur == null) { cur = s; prev = s; run = 1; st.push(cur); continue; }
    run = s === prev ? run + 1 : 1; prev = s;
    const need = s < cur ? M.conf_down : M.conf_up;
    if (s !== cur && run >= need) cur = s;
    st.push(cur);
  }
  const legSlice = legs.map(l => Object.assign({}, l, {on:l.on.slice(i0)}));

  const ds = [
    line('S&P 500', px, K.spx, {yAxisID:'yP', borderWidth:1.4}),
    area('Internals risk-on', sc, K.scoreFill, {yAxisID:'yC', borderColor:K.navy, borderWidth:1}),
  ];
  const cfg = {
    type:'line',
    data:{labels, datasets:ds},
    options:{
      responsive:true, maintainAspectRatio:false, animation:false,
      interaction:{mode:'index', intersect:false},
      layout:{padding:{left:118, right:6, top:4}},
      // declared bottom-up: the lane strip sits at the foot, the price panel on top
      scales:{
        x:xAxis(labels),
        yL:panelAxis({id:'yL', stackWeight:1.5, min:0, max:1, grid:{display:false},
          ticks:{display:false}}),
        yC:panelAxis({id:'yC', stackWeight:1.25, min:0, max:K_, title:{text:'Internals risk-on'},
          ticks:{stepSize:1, callback:v => v}}),
        yP:priceAxis(px, 2.1),
      },
      plugins:{
        legend:{display:false},
        statep:{state:st},
        lanep:{legs:legSlice, axis:'yL'},
        tooltip:Object.assign({}, tipBase, {callbacks:{
          title:(it) => dfmt(labels[it[0].dataIndex]),
          label:(it) => it.dataset.yAxisID === 'yP'
            ? 'S&P 500  ' + num(it.parsed.y)
            : 'Risk-on  ' + it.parsed.y + ' of ' + K_,
          afterBody:(it) => {
            const i = it[0].dataIndex;
            const s = st[i];
            const on = legSlice.filter(l => l.on[i] === 1).map(l => l.short);
            const off = legSlice.filter(l => l.on[i] === 0).map(l => l.short);
            const out = [s == null ? '' : 'State  ' + STN[s]];
            if (on.length) out.push('On: ' + on.join(', '));
            if (off.length) out.push('Off: ' + off.join(', '));
            return out;
          }}}),
      }
    }
  };
  if (c1) c1.destroy();
  c1 = new Chart($('c1'), cfg);

  legend('lg1', [{c:K.scoreFill, t:'Internals reading risk-on (count)'}, {c:K.spx, t:'S&amp;P 500', ln:1, thin:1}]
    .concat(Object.keys(FAM).map(f => ({c:FAM[f], t:esc(f)})))
    .concat(MODE === 'all' && CORE.length < P.legs.length ? [{c:K.grey, t:'Fails the evidence screen'}] : [])
    .concat([{c:K.bandGood, t:'Risk-on state', band:1}, {c:K.bandBad, t:'Washout state', band:1}]));

  const nOn = sc[sc.length - 1];
  $('sub1').innerHTML = 'Count of internals reading risk-on out of ' + K_ +
    '. Risk-on when ' + hi + ' or more are on, washout when none are, fragile in between; a deterioration is published after ' +
    M.conf_down + ' sessions and an improvement after ' + M.conf_up +
    '. <b>Latest: ' + nOn + ' of ' + K_ + ' risk-on, ' + STN[st[st.length - 1]].toLowerCase() + '.</b>';
  $('fn1').innerHTML = 'The strip under the count shows which internal is risk-on at each date, coloured by family; ' +
    '<b>labels in grey fail the evidence screen below.</b> Shading marks the published state. ' +
    'Src: Yahoo Finance, FRED, own calculations.';
}

// ---------- 2. internals board ----------
(function () {
  const rows = P.legs.map(l => {
    const nw = N.legs[l.key];
    const sr = P.screen.find(r => r.key === l.key);
    const dirTxt = l.dirn > 0 ? 'above' : 'below';
    const okTxt = nw.on === 1 ? 'risk-on' : 'risk-off';
    const gapTxt = (nw.gap == null ? '–' : (nw.gap > 0 ? '+' : MIN) + Math.abs(nw.gap).toFixed(1) + '%');
    const dec = l.unit === 'index' ? 1 : l.unit === 'pp' ? 2 : 3;
    return '<tr class="' + (nw.on === 1 ? 'pass' : '') + '">' +
      '<td class="t">' + esc(l.name) + '<small>' + esc(l.src) + '</small></td>' +
      '<td>' + (nw.lvl == null ? '–' : nw.lvl.toFixed(dec)) + (l.unit === 'pp' ? 'pp' : '') + '</td>' +
      '<td>' + (nw.ma == null ? '–' : nw.ma.toFixed(dec)) + '</td>' +
      '<td>' + gapTxt + '</td>' +
      '<td><span class="pill ' + (nw.on === 1 ? 'good' : 'bad') + '">' + (nw.on === 1 ? 'Risk-on' : 'Risk-off') + '</span></td>' +
      '<td>' + nw.days + '</td>' +
      '<td class="t r">' + dfmt(nw.since) + '</td>' +
      '<td><span class="pill ' + ({pass:'number', weak:'neu', none:'none'}[sr.grade]) + '">' +
        ({pass:'Passes', weak:'Partial', none:'No edge'}[sr.grade]) + '</span></td></tr>';
  }).join('');
  $('tB').innerHTML = '<thead><tr><th class="t">Internal</th><th>Latest</th><th>' + M.ma + 'd avg</th>' +
    '<th>Gap</th><th>State</th><th>Sessions</th><th class="t">Since</th><th>Screen</th></tr></thead><tbody>' +
    rows + '</tbody>';
  $('fnB').innerHTML = '<b>Gap</b> is how far the internal sits from its ' + M.ma +
    '-day average; the sign is set so a positive gap always means risk-on. ' +
    '<b>Sessions</b> counts how long it has held that side. The screen column is the verdict from the evidence panel below.';
})();

// ---------- 3. internal detail ----------
function drawDetail() {
  const l = P.legs.find(x => x.key === DETAIL);
  const labels = P.wdates, px = P.wpx;
  const dec = l.unit === 'index' ? 1 : l.unit === 'pp' ? 2 : 3;
  const off = l.w_on.map(v => v === 0 ? 1 : null);
  const ds = [
    line('S&P 500', px, K.spx, {yAxisID:'yP', borderWidth:1.1}),
    line(l.name, l.w_lvl, legColor(l), {yAxisID:'yV', borderWidth:1.5}),
    line(M.ma + '-day average', l.w_ma, K.accent, {yAxisID:'yV', borderWidth:1.2, borderDash:[4, 3]}),
  ];
  const cfg = {
    type:'line',
    data:{labels, datasets:ds},
    options:{
      responsive:true, maintainAspectRatio:false, animation:false,
      interaction:{mode:'index', intersect:false},
      // declared bottom-up: the internal sits below, the price panel on top
      scales:{
        x:xAxis(labels),
        yV:panelAxis({id:'yV', stackWeight:1.35,
          title:{text:l.unit === 'pp' ? 'Percentage points' : l.unit === 'index' ? 'Index' : 'Ratio'},
          ticks:{callback:v => v.toFixed(dec)}}),
        yP:priceAxis(px, 1),
      },
      plugins:{
        legend:{display:false},
        statep:{state:l.w_on.map(v => v === 0 ? 0 : null), soft:true},
        tooltip:Object.assign({}, tipBase, {callbacks:{
          title:(it) => dfmt(labels[it[0].dataIndex]),
          label:(it) => it.dataset.yAxisID === 'yP'
            ? 'S&P 500  ' + num(it.parsed.y)
            : it.dataset.label + '  ' + it.parsed.y.toFixed(dec),
          afterBody:(it) => {
            const v = l.w_on[it[0].dataIndex];
            return v == null ? [] : ['Reads  ' + (v === 1 ? 'risk-on' : 'risk-off')];
          }}}),
      }
    }
  };
  if (c2) c2.destroy();
  c2 = new Chart($('c2'), cfg);
  $('h3det').textContent = l.name + ' and the S&P 500';
  legend('lg2', [{c:K.spx, t:'S&amp;P 500', ln:1, thin:1}, {c:legColor(l), t:esc(l.name), ln:1},
    {c:K.accent, t:esc(M.ma + '-day average'), ln:1, thin:1}, {c:K.bandBad, t:'Reads risk-off', band:1}]);
  const sr = P.screen.find(r => r.key === l.key);
  $('fn2').innerHTML = '<b>' + esc(l.name) + ':</b> ' + esc(l.what) +
    ' Risk-on when the ratio sits ' + (l.dirn > 0 ? 'above' : 'below') + ' its ' + M.ma +
    '-day average. History from <b>' + dfmt(sr.first) + '</b>. Source: ' + esc(l.src) + '.';
  [...document.querySelectorAll('#picker button')].forEach(b =>
    b.classList.toggle('on', b.dataset.k === DETAIL));
}
$('picker').innerHTML = P.legs.map(l =>
  '<button class="b" data-k="' + l.key + '">' + esc(l.short) + '</button>').join('');
$('picker').addEventListener('click', e => {
  const b = e.target.closest('button'); if (!b) return;
  DETAIL = b.dataset.k; drawDetail();
});

// ---------- 4. evidence: internal screen ----------
(function () {
  const rows = P.legs.map(l => {
    const r = P.screen.find(x => x.key === l.key);
    return '<tr class="' + (r.grade === 'pass' ? 'pass' : '') + '">' +
      '<td class="t">' + esc(l.name) + '<small>risk-on on ' + num(r.n_on) + ' of ' +
        num(r.n_on + r.n_off) + ' sessions, from ' + dfmt(r.first) + '</small></td>' +
      '<td>' + pc(r.dd_on) + '</td><td>' + pc(r.dd_off) + '</td>' +
      '<td>' + (r.dd_gap == null ? '–' : sgn(r.dd_gap * 100) + 'pp') + '</td>' +
      '<td>' + pfmt(r.p_dd) + '</td>' +
      '<td>' + (r.vol_on == null ? '–' : r.vol_on.toFixed(1)) + '</td>' +
      '<td>' + (r.vol_off == null ? '–' : r.vol_off.toFixed(1)) + '</td>' +
      '<td>' + pfmt(r.p_vol) + '</td>' +
      '<td>' + (r.r12_gap == null ? '–' : sgn(r.r12_gap * 100) + 'pp') + '</td>' +
      '<td>' + pfmt(r.p_r12) + '</td>' +
      '<td><span class="pill ' + ({pass:'number', weak:'neu', none:'none'}[r.grade]) + '">' +
        ({pass:'Passes', weak:'Partial', none:'No edge'}[r.grade]) + '</span></td></tr>';
  }).join('');
  $('tE').innerHTML = '<thead><tr><th class="t">Internal</th>' +
    '<th>Fall, on</th><th>Fall, off</th><th>Gap</th><th>p</th>' +
    '<th>Vol, on</th><th>Vol, off</th><th>p</th>' +
    '<th>+12m gap</th><th>p</th><th>Verdict</th></tr></thead><tbody>' + rows + '</tbody>';
  const fails = P.legs.filter(l => P.screen.find(r => r.key === l.key).grade === 'none').map(l => l.name);
  $('subE').innerHTML = 'Each internal on its own, measured on what it actually forecasts: the <b>deepest fall</b> and the ' +
    '<b>realised volatility</b> of the S&amp;P 500 over the following month, from risk-on days against risk-off days. ' +
    'The last two columns repeat the test on twelve-month returns for contrast. ' +
    (fails.length ? '<b>' + esc(fails.join(' and ')) + ' fails on every measure</b> and is greyed out in the chart above, ' +
      'though it stays in the count.' : '');
  $('fnE').innerHTML = 'Each internal is tested over its own full history, which differs by series, so the columns are not ' +
    'strictly comparable across rows. <b>p</b> is a two-sided circular block bootstrap with 252-session blocks and ' +
    '3,000 draws, which preserves the overlap in forward windows; values below 0.05 are treated as passing. ' +
    'A positive <b>fall gap</b> means drawdowns are that much shallower on risk-on days, which is the result ' +
    'the framework predicts.';
})();

// ---------- 5. evidence: aggregator state ----------
(function () {
  const rows = P.st_rows.map(r => {
    const isBase = r.state === 'All days';
    return '<tr class="' + (isBase ? 'base' : '') + '">' +
      '<td class="t">' + (isBase ? r.state :
        '<span class="pill ' + STCLS[STNUM[r.state]] + '">' + r.state + '</span>') + '</td>' +
      '<td>' + num(r.n) + '</td><td>' + r.pct.toFixed(0) + '%</td>' +
      '<td>' + pc(r.dd1) + '</td><td>' + pc(r.dd3) + '</td>' +
      '<td>' + r.vol.toFixed(1) + '</td>' +
      '<td>' + pc(r['3m']) + '</td><td>' + pc(r['12m']) + '</td><td>' + pc0(r.hit12) + '</td></tr>';
  }).join('');
  $('tS1').innerHTML = '<thead><tr><th class="t">State</th><th>Days</th><th>% time</th>' +
    '<th>Fall, 1m</th><th>Fall, 3m</th><th>Vol</th><th>+3m</th><th>+12m</th><th>Higher</th></tr></thead><tbody>' +
    rows + '</tbody>';
  const p = P.st_p;
  $('subS1').innerHTML = 'Every day in each state, over the full-six window. <b>Fall</b> is the deepest peak-to-trough ' +
    'decline over the next month and three months; <b>Vol</b> is annualised realised volatility over the next month.';
  $('fnS1').innerHTML = 'The risk ordering is monotonic: washout days are followed by the deepest falls and the highest ' +
    'volatility, risk-on days by the shallowest. <b>Washout against all other days: p=' + pfmt(p.p_wo_dd) +
    ' on drawdown.</b> Risk-on against the rest is directionally right but short of significance on this window ' +
    '(p=' + pfmt(p.p_dd) + ' on drawdown, p=' + pfmt(p.p_vol) + ' on volatility); the longer panels alongside are ' +
    'stronger. Twelve-month returns do not separate at all (p=' + pfmt(p.p_r12) + ').';
})();

// ---------- 6. evidence: longer panels ----------
(function () {
  const rows = P.pan_rows.map((r, i) =>
    '<tr class="' + (i === 0 ? 'pass' : '') + '">' +
    '<td class="t">' + esc(r.name) + '<small>' + r.k + ' internals · from ' + dfmt(r.start) +
      ' · ' + num(r.n) + ' sessions</small></td>' +
    '<td>' + pc(r.dd_on) + '</td><td>' + pc(r.dd_fr) + '</td><td>' + pc(r.dd_wo) + '</td>' +
    '<td>' + pfmt(r.p_dd) + '</td>' +
    '<td>' + r.vol_on.toFixed(1) + '</td><td>' + r.vol_fr.toFixed(1) + '</td><td>' + r.vol_wo.toFixed(1) + '</td>' +
    '<td>' + pc(r.r12_on) + '</td><td>' + pc(r.r12_fr) + '</td><td>' + pc(r.r12_wo) + '</td></tr>').join('');
  $('tS2').innerHTML = '<thead><tr><th class="t">Panel</th>' +
    '<th>Fall on</th><th>Fall fr</th><th>Fall wo</th><th>p</th>' +
    '<th>Vol on</th><th>Vol fr</th><th>Vol wo</th>' +
    '<th>+12m on</th><th>+12m fr</th><th>+12m wo</th></tr></thead><tbody>' + rows + '</tbody>';
  const lastP = P.pan_rows[P.pan_rows.length - 1];
  $('fnS2').innerHTML = '<b>on</b> is risk-on, <b>fr</b> fragile, <b>wo</b> washout. The drawdown and volatility ' +
    'ordering survives every panel, and tightens as the sample lengthens: on the longest panel, from ' +
    dfmt(lastP.start) + ', p=' + pfmt(lastP.p_dd) + ' on drawdown and ' + pfmt(lastP.p_vol) + ' on volatility. ' +
    '<b>The twelve-month columns show the one return result that does hold: the fragile middle is the weakest state ' +
    'in every panel</b>, at ' + pc(lastP.r12_fr) + ' against ' + pc(lastP.r12_on) + ' for risk-on on the longest panel. ' +
    'Washout returns beat fragile ones throughout, because washouts cluster at lows.';
})();

// ---------- 7. evidence: era stability ----------
(function () {
  const rows = P.era_rows.map(r =>
    '<tr><td class="t">' + esc(r.era) + '</td>' +
    '<td>' + pc(r.dd_on) + '</td><td>' + pc(r.dd_fr) + '</td><td>' + pc(r.dd_wo) + '</td>' +
    '<td>' + r.vol_on.toFixed(1) + '</td><td>' + r.vol_wo.toFixed(1) + '</td>' +
    '<td>' + pc(r.r12_on) + '</td><td>' + pc(r.r12_wo) + '</td></tr>').join('');
  $('tEr').innerHTML = '<thead><tr><th class="t">Era</th><th>Fall on</th><th>Fall fr</th><th>Fall wo</th>' +
    '<th>Vol on</th><th>Vol wo</th><th>+12m on</th><th>+12m wo</th></tr></thead><tbody>' + rows + '</tbody>';
  const e = P.era_rows, lastE = e[e.length - 1];
  $('subEr').innerHTML = 'The four-internal panel, which reaches back to ' + dfmt(P.pan_rows[2].start) +
    ', split into three eras. This is the test that matters for using the aggregator now.';
  $('fnEr').innerHTML = '<b>The risk signal holds in all three eras:</b> washout days are followed by deeper falls and ' +
    'higher volatility than risk-on days in every one. <b>The return signal does not.</b> Risk-on beat washout by ' +
    sgn((e[0].r12_on - e[0].r12_wo) * 100) + 'pp over twelve months in ' + esc(e[0].era) + ', by ' +
    sgn((e[1].r12_on - e[1].r12_wo) * 100) + 'pp in ' + esc(e[1].era) + ', and has <b>trailed it by ' +
    Math.abs((lastE.r12_on - lastE.r12_wo) * 100).toFixed(1) + 'pp in ' + esc(lastE.era) +
    '</b>. Buying weak internals has worked for roughly a decade. Treat the score as a risk gauge, not a timing tool.';
})();

// ---------- 8. evidence: non-confirmation ----------
(function () {
  const rows = P.div_rows.map((r, i) =>
    '<tr class="' + (i === 3 ? 'base' : '') + '">' +
    '<td class="t' + (i === 3 ? '' : '') + '">' + esc(r.case) + '</td>' +
    '<td>' + num(r.n) + '</td><td>' + pc(r['1m']) + '</td><td>' + pc(r['3m']) + '</td>' +
    '<td>' + pc(r['6m']) + '</td><td>' + pc(r.dd3) + '</td><td>' + pc(r.dd6) + '</td></tr>').join('');
  $('tD').innerHTML = '<thead><tr><th class="t">Case</th><th>Days</th><th>+1m</th><th>+3m</th><th>+6m</th>' +
    '<th>Fall, 3m</th><th>Fall, 6m</th></tr></thead><tbody>' + rows + '</tbody>';
  const weak = P.div_rows[1], strong = P.div_rows[0];
  $('fnD').innerHTML = 'Days with the index within 1% of its 52-week high and no more than two internals risk-on were ' +
    'followed by ' + pc(weak['6m']) + ' over six months, against ' + pc(strong['6m']) +
    ' when five or six were on. The direction matches the textbook reading, but this rests on <b>' + num(weak.n) +
    ' sessions</b> in a handful of episodes, so it is suggestive rather than established. ' +
    'Note too that days well below the high beat both, which is the washout effect again.';
})();

// ---------- 9. state log ----------
(function () {
  const rows = P.log.map(e =>
    '<tr><td class="t r">' + dfmt(e.date) + '</td>' +
    '<td class="t r">' + esc(e.frm) + ' → <b>' + esc(e.to) + '</b></td>' +
    '<td>' + e.score + ' of ' + M.n_int + '</td>' +
    '<td>' + num(e.px) + '</td>' +
    '<td>' + pc(e['1m']) + '</td><td>' + pc(e['3m']) + '</td><td>' + pc(e['12m']) + '</td>' +
    '<td>' + pc(e.dd3) + '</td></tr>').join('');
  $('tL').innerHTML = '<thead><tr><th class="t">Date</th><th class="t">Change</th><th>Score</th>' +
    '<th>S&amp;P 500</th><th>+1m</th><th>+3m</th><th>+12m</th><th>Fall, 3m</th></tr></thead><tbody>' +
    rows + '</tbody>';
})();

// ---------- 10. notes ----------
(function () {
  const sr = k => P.screen.find(r => r.key === k);
  const notes = [
    '<b>The rule.</b> Each internal is risk-on when it sits on the risk-on side of its own ' + M.ma +
      '-day average — above, for the three ratios and the VIX curve; below, for credit spreads and the dollar, ' +
      'where a lower reading is the risk-on one. The score counts how many of the ' + M.n_int +
      ' read risk-on. The state is risk-on at ' + M.hi + ' or more, washout at none, and fragile in between.',
    '<b>Why the confirmation is asymmetric.</b> On the raw score the state changes about once every nine sessions, ' +
      'too often to act on. Requiring ' + M.conf_down + ' sessions to publish a deterioration and ' + M.conf_up +
      ' to publish an improvement cuts that to about once a month without weakening the risk separation, and it keeps ' +
      'the warning prompt while making the all-clear slow. A symmetric five-session rule missed short washouts such as ' +
      'August 2015 entirely.',
    '<b>The window is not tuned.</b> Repeating the whole exercise with 50-, 75-, 150-, 200- and 250-day averages moves ' +
      'the drawdown and volatility figures by a tenth of a point or two and changes no conclusion. The ' + M.ma +
      '-day choice is not doing the work.',
    '<b>What the evidence supports.</b> The score forecasts near-term risk, not direction. Of the ' + M.n_int +
      ' internals, ' + P.screen.filter(r => r.grade === 'pass').length + ' separate next-month drawdown and volatility ' +
      'at p≤0.05 on their own, and the three-state ordering is monotonic in every panel and every era tested.',
    '<b>What it does not support.</b> Nothing here predicts twelve-month returns. The washout state is followed by ' +
      '<i>above</i>-average returns, because washouts happen near lows, and the return gap between risk-on and washout ' +
      'has inverted since 2018. Read the score as a measure of how much the index is likely to move, and in which ' +
      'direction it will hurt, not as a buy or sell call.',
    '<b>The dollar carries no signal here.</b> It is in the count because it belongs to the framework, but it fails ' +
      'every test — p=' + pfmt(sr('dxy').p_dd) + ' on drawdown and ' + pfmt(sr('dxy').p_vol) +
      ' on volatility — at every window length tried. The <b>Screened ' + CORE.length +
      '</b> toggle above the chart drops it. Doing so barely moves the state history, which is itself a reason to ' +
      'doubt it adds information.',
    '<b>Credit spreads use Moody\'s Baa, not high yield.</b> FRED\'s ICE BofA option-adjusted spread series now carry ' +
      'only about three years of history under their licence, too little to score against a ' + M.ma +
      '-day average. BAA10Y, the Moody\'s Baa yield less the 10-year Treasury, runs daily from 1986 and tracks the ' +
      'same risk closely enough for this purpose, with the caveat that it is an investment-grade spread and so ' +
      'moves less violently than high yield in a credit event.',
    '<b>Start dates differ by internal.</b> The aggregator can only run from ' + dfmt(M.start) +
      ', when SPHB and SPLV have ' + M.ma + ' days of history. That window holds no secular bear market, which is ' +
      'why the longer narrower panels matter: they add 2008–09 and 2000–02, and the risk result gets stronger, ' +
      'not weaker.',
    '<b>Total return where it matters.</b> The bond and sector ratios use dividend-adjusted prices, so HYG against IEI ' +
      'is a genuine total-return comparison rather than a price one, which would drift with the coupon difference.',
    '<b>Overlapping windows.</b> Forward returns and drawdowns from consecutive days overlap heavily, so ordinary ' +
      'standard errors would be far too small. Every p-value here is a circular block bootstrap with 252-session ' +
      'blocks and 3,000 draws, resampling the state series rather than the returns.',
    '<b>Same-day data.</b> Each internal is scored on the close of the day it is dated, so the published state is ' +
      'available only after that close. The credit spread is one business day behind the price series, which is how ' +
      'FRED publishes it.',
    '<b>A source quirk worth knowing.</b> Yahoo recomputes its dividend-adjustment factors in single precision on ' +
      'every request, so two downloads minutes apart return adjusted closes that differ in the seventh significant ' +
      'figure. The rebuild is exactly reproducible from a given cache, but a fresh download can move a ratio by one ' +
      'unit in the fourth decimal. That is far too small to change a reading unless an internal is sitting precisely ' +
      'on its average.',
  ];
  $('notes').innerHTML = notes.map(t => '<li>' + t + '</li>').join('');
})();

// ---------- controls ----------
document.querySelector('.bar').addEventListener('click', e => {
  const b = e.target.closest('button.b'); if (!b) return;
  if (b.dataset.r) {
    RANGE = b.dataset.r;
    [...document.querySelectorAll('.bar button[data-r]')].forEach(x => x.classList.toggle('on', x === b));
  } else if (b.dataset.m) {
    MODE = b.dataset.m;
    [...document.querySelectorAll('.bar button[data-m]')].forEach(x => x.classList.toggle('on', x === b));
  }
  drawAgg();
});

drawAgg();
drawDetail();
</script>
</body>
</html>
'''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="sp500_market_internals_aggregator.html")
    ap.add_argument("--cache", default=os.environ.get("INT_CACHE", "cache"))
    ap.add_argument("--refetch", action="store_true")
    a = ap.parse_args()

    global CACHE
    CACHE = a.cache
    raw_path = os.path.join(CACHE, "raw.pkl")
    fresh = os.path.exists(raw_path) and (time.time() - os.path.getmtime(raw_path)) < 20 * 3600
    if a.refetch or not fresh:
        if not FRED_KEY:
            raise SystemExit(
                "FRED_API_KEY is not set. Get a free key at fred.stlouisfed.org and export it "
                "(or add it as a repository secret), then run again."
            )
        print("downloading series ...", flush=True)
        fetch_all()
    else:
        age = (time.time() - os.path.getmtime(raw_path)) / 3600
        print(f"using cached series ({age:.1f}h old); pass --refetch to download again", flush=True)

    P = compute(CACHE)
    # "</" inside the blob could otherwise close the <script> element early
    blob = json.dumps(P, separators=(",", ":")).replace("</", "<\\/")
    html, n = re.subn(r"/\*@DATA\*/.*?/\*@END\*/",
                      lambda m: "/*@DATA*/" + blob + "/*@END*/", TEMPLATE, flags=re.S)
    assert n == 1, f"data marker matched {n} times"
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"wrote {a.out}  {len(html):,} bytes")
    print(f"as of {P['meta']['asof']}  |  {P['now']['score']} of {P['meta']['n_int']} internals risk-on"
          f"  |  {P['now']['state_name']} since {P['now']['since']}")


if __name__ == "__main__":
    main()
