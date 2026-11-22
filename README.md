# S&P 500 Market Internals Aggregator

A self-contained HTML dashboard that scores six risk-appetite internals — high beta against
low beta, cyclicals against defensives, credit spreads, bond-market risk appetite, the VIX
term structure and the dollar — risk-on or risk-off against their own 100-day averages, and
counts them into one 0–6 score.

GitHub Actions rebuilds it after every US close and publishes it to GitHub Pages.

---

## Setup

You need a free FRED API key and about ten minutes.

**1. Get a FRED API key.**
Request one at <https://fredaccount.stlouisfed.org/apikeys>. It arrives immediately.

**2. Create the repository and push these files.**

```bash
git init
git add .
git commit -m "Market Internals Aggregator"
git branch -M main
git remote add origin https://github.com/<you>/market-internals-aggregator.git
git push -u origin main
```

Make sure the `.github` folder goes up — `git status` should list
`.github/workflows/refresh.yml`. Some tools hide dotfiles.

GitHub Pages is free on public repositories. On a private one it needs a paid plan.

**3. Add the key as a secret.**
**Settings → Secrets and variables → Actions → New repository secret**.
Name it exactly `FRED_API_KEY` and paste the key as the value.
The key is not in any file here, and `check.py` fails the build if one ever ends up in the page.

**4. Turn on Pages.**
**Settings → Pages → Build and deployment → Source: GitHub Actions.**
Not "Deploy from a branch".

**5. Allow the workflow to run.**
**Settings → Actions → General → Workflow permissions: Read and write permissions.**

**6. Run it once by hand.**
**Actions → Refresh Market Internals Aggregator → Run workflow.**
It downloads about 40 years of daily history for sixteen series. In testing that took
under a minute, but Yahoo throttles shared addresses unpredictably, so allow up to the
job's 30-minute limit on a bad night. Later runs restore the cache and are no slower.

When it finishes, the page is at `https://<you>.github.io/market-internals-aggregator/`.
The deploy job prints the URL.

After that it runs itself at **22:40 UTC, Monday to Friday** — about 9:40am the next
morning in Melbourne, 8:40am during southern winter. Change the `cron:` line in
`.github/workflows/refresh.yml` to move it.

---

## What runs

| File | Does |
| --- | --- |
| `rebuild.py` | Downloads every series, scores the internals, runs the evidence tests, writes the page. Standalone — it embeds the HTML template. |
| `check.py` | Inspects the built page and exits non-zero if it looks wrong. The workflow publishes only if this passes. |
| `.github/workflows/refresh.yml` | Schedule, cache, build, check, deploy. |
| `requirements.txt` | `numpy` and `yfinance`. |

Data comes from Yahoo Finance (SPHB, SPLV, XLY, XLP, HYG, IEI, LQD, IEF, `^VIX`, `^VIX3M`,
`DX-Y.NYB`, `^GSPC`) and FRED (BAA10Y, AAA10Y, BAMLH0A0HYM2, DGS10).

### Running it locally

```bash
pip install -r requirements.txt
export FRED_API_KEY=your_key
python3 rebuild.py                 # -> sp500_market_internals_aggregator.html
python3 check.py sp500_market_internals_aggregator.html
```

`rebuild.py` skips the download if the cache is less than a day old. Pass `--refetch` to
force it, `--out PATH` to write elsewhere, `--cache DIR` to put the cache elsewhere.

---

## How it survives a bad night

**Yahoo throttles shared IPs.** GitHub's runners share addresses with everyone else, so a
429 is a question of when, not if. Each download retries six times with waits that grow to
five minutes. If a series still fails, the run **keeps yesterday's copy of that series** and
carries on, rather than throwing away the whole build. The page then shows a red
`Stale` note in its header line naming what did not refresh, and `check.py` prints a warning.

The run only fails outright if a series the score cannot be computed without has no cached
copy either.

**The cache is a convenience, not a dependency.** It lives in the Actions cache, keyed per
run with a `series-` prefix fallback, so each run restores the most recent one. GitHub
evicts caches after seven days without a hit and ten days of inactivity would clear it
entirely; a daily schedule keeps it warm, and if it does vanish the next run simply
downloads everything again.

**`check.py` is the gate.** It refuses to publish if the payload is missing or unparseable,
the file is suspiciously small, an internal has been added or dropped, the series lengths
disagree, dates are out of order or duplicated, the score or state is out of range, the text
contains `undefined` or `NaN`, an API key has leaked into the page, or **the latest close is
more than five days old**. That last one matters most: it is what stops a quietly broken
feed from replacing a good page with stale numbers that still look plausible. Raise the
limit with `--max-age N` if you ever need to publish over a long market closure.

A failed check leaves the previous page live and marks the run red in the Actions tab.

---

## Changing the internals

The six are defined in the `SPEC` list near the top of `rebuild.py`. Each entry is either a
ratio of two Yahoo series (`num` / `den`) or a FRED level (`lvl`), plus `dirn`: `+1` when a
higher reading is risk-on, `-1` when a lower one is.

If you add or remove one, update **both** `EXPECTED_INTERNALS` and `EXPECTED_KEYS` in
`check.py`, or every run will be blocked from publishing.

Two settings sit beside `SPEC`:

- `MA = 100` — the trailing window each internal is measured against. Tested at 50, 75,
  150, 200 and 250; the conclusions do not move.
- `CONF_DOWN = 2`, `CONF_UP = 5` — sessions needed to confirm a state change. Asymmetric on
  purpose: prompt warnings, slow all-clears.

---

## What the dashboard is for

It forecasts **risk, not direction**. The three-state ordering of next-month drawdown and
volatility is monotonic in every panel and era tested. Twelve-month returns do not line up
with the score at all, and the gap between risk-on and washout has inverted since 2018. The
page says so in its own evidence panels; read those before acting on the score.

The dollar is in the count because it belongs to the framework, but it fails the evidence
screen on every measure and is greyed out in the chart.
