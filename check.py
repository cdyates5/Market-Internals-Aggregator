#!/usr/bin/env python3
"""Sanity-check a built dashboard before it is published.

Exits non-zero if anything looks wrong, which stops the workflow from replacing a
good live page with a broken or silently stale one.

    python3 check.py site/index.html
    python3 check.py site/index.html --max-age 6     # allow a longer data gap

Update EXPECTED_INTERNALS if internals are ever added to or removed from rebuild.py.
"""
import argparse
import datetime as dt
import json
import re
import sys

EXPECTED_INTERNALS = 6          # keep in step with SPEC in rebuild.py
EXPECTED_KEYS = ["beta", "cycdef", "credit", "bondra", "vixts", "dxy"]
MIN_BYTES = 200_000             # a real build is ~480 KB; far less means the payload is missing
MAX_DATA_AGE_DAYS = 5           # calendar days between the latest close and today
MIN_SESSIONS = 3000             # the full-six window is ~3,800 sessions and only grows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--max-age", type=int, default=MAX_DATA_AGE_DAYS)
    a = ap.parse_args()

    errors, warnings = [], []
    try:
        html = open(a.path, encoding="utf-8").read()
    except OSError as e:
        print(f"FAIL: cannot read {a.path}: {e}")
        return 1

    if len(html) < MIN_BYTES:
        errors.append(f"file is only {len(html):,} bytes, expected at least {MIN_BYTES:,}")

    m = re.search(r"/\*@DATA\*/(.*?)/\*@END\*/", html, re.S)
    if not m:
        print("FAIL: no payload found between the data markers")
        return 1
    try:
        P = json.loads(m.group(1).replace("<\\/", "</"))
    except json.JSONDecodeError as e:
        print(f"FAIL: payload is not valid JSON: {e}")
        return 1

    if "null" == m.group(1).strip():
        print("FAIL: template was published without its data")
        return 1

    M, N = P.get("meta", {}), P.get("now", {})

    # ---- structure ----
    for key in ("meta", "now", "dates", "px", "score", "state", "legs", "screen",
                "st_rows", "pan_rows", "era_rows", "div_rows", "log", "wdates", "wpx"):
        if not P.get(key):
            errors.append(f"payload section '{key}' is missing or empty")

    legs = P.get("legs", [])
    if len(legs) != EXPECTED_INTERNALS:
        errors.append(f"expected {EXPECTED_INTERNALS} internals, found {len(legs)}")
    got = [l.get("key") for l in legs]
    if got and got != EXPECTED_KEYS:
        errors.append(f"internals changed: expected {EXPECTED_KEYS}, found {got}")
    if M.get("n_int") != EXPECTED_INTERNALS:
        errors.append(f"meta.n_int is {M.get('n_int')}, expected {EXPECTED_INTERNALS}")

    # ---- series line up ----
    n = len(P.get("dates", []))
    if n < MIN_SESSIONS:
        errors.append(f"only {n} sessions in the aggregator window, expected at least {MIN_SESSIONS}")
    for key in ("px", "score", "state"):
        if len(P.get(key, [])) != n:
            errors.append(f"series '{key}' has {len(P.get(key, []))} points against {n} dates")
    for l in legs:
        if len(l.get("on", [])) != n:
            errors.append(f"lane '{l.get('key')}' has {len(l.get('on', []))} points against {n} dates")
        if len(l.get("w_lvl", [])) != len(P.get("wdates", [])):
            errors.append(f"weekly series for '{l.get('key')}' does not match the weekly dates")

    d = P.get("dates", [])
    if d != sorted(d):
        errors.append("dates are not in ascending order")
    if len(set(d)) != len(d):
        errors.append("dates contain duplicates")

    # ---- values in range ----
    sc = [v for v in P.get("score", []) if v is not None]
    if sc and (min(sc) < 0 or max(sc) > EXPECTED_INTERNALS):
        errors.append(f"score out of range: {min(sc)} to {max(sc)}")
    st = {v for v in P.get("state", []) if v is not None}
    if st - {0, 1, 2}:
        errors.append(f"unexpected state values: {sorted(st - {0, 1, 2})}")
    if not isinstance(N.get("score"), int) or not 0 <= N["score"] <= EXPECTED_INTERNALS:
        errors.append(f"current score is {N.get('score')!r}")
    if N.get("state_name") not in ("Risk-on", "Fragile", "Washout"):
        errors.append(f"current state is {N.get('state_name')!r}")
    px = [v for v in P.get("px", []) if v is not None]
    if px and (min(px) <= 0 or max(px) > 100_000):
        errors.append(f"S&P 500 series out of range: {min(px)} to {max(px)}")

    # ---- freshness ----
    asof = M.get("asof")
    if not asof:
        errors.append("meta.asof is missing")
    else:
        age = (dt.date.today() - dt.date.fromisoformat(asof)).days
        if age > a.max_age:
            errors.append(f"data is {age} days old (latest close {asof}), limit is {a.max_age}")
        elif age > 2:
            warnings.append(f"data is {age} days old (latest close {asof})")
    if M.get("stale"):
        warnings.append("series running on cached values: " + ", ".join(M["stale"]))

    # ---- rendered text ----
    body = re.sub(r"<script.*?</script>", "", html, flags=re.S)
    body = re.sub(r"<[^>]+>", " ", body)
    for bad in ("undefined", "NaN", "[object Object]"):
        if bad in body:
            errors.append(f"the page text contains '{bad}'")
    if "/*@DATA*/" not in html or "/*@END*/" not in html:
        errors.append("data markers are missing from the output")
    if re.search(r"api_key=[0-9a-f]{32}", html):
        errors.append("an API key is embedded in the page")

    # ---- report ----
    for w in warnings:
        print(f"WARN: {w}")
    if errors:
        print(f"FAIL: {len(errors)} problem(s) with {a.path}")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f'{{"ok": true, "file": "{a.path}", "bytes": {len(html)}, "asof": "{asof}", '
          f'"sessions": {n}, "score": {N.get("score")}, "state": "{N.get("state_name")}", '
          f'"warnings": {len(warnings)}}}')
    print(f"PASS :: {asof} :: {N.get('score')} of {EXPECTED_INTERNALS} risk-on :: {N.get('state_name')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
