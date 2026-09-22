# LFS point-in-time backtest

This directory is intentionally **not connected to the public UI yet**.

The backtest must answer one question: if the LFS score had been computed with only information publicly available at the historical ranking date, did higher-ranked stocks subsequently experience higher stock-price returns?

## Locked methodology

- Market: Korea first (KOSPI + KOSDAQ). US is not published until a survivorship-safe historical universe is validated.
- Rebalance / score date: first trading day on or after June 1 of each year.
- Point-in-time financials: prior fiscal-year annual report plus current-year Q1 report when it had been filed by the score date. Every filing receipt date is checked against the score date. A later filing is rejected.
- Long-history inputs: only financial periods available through OpenDART at that time are used. Earlier cohorts can therefore have shorter history; history length is recorded.
- Universe/prices: FinanceData/marcap, a daily KRX-derived dataset covering 1995-present, is used to reconstruct the actual securities, names, closes, shares and market caps that existed on each historical date. This avoids using only today's listed stocks. Preferred shares are not independently ranked; one corporation gets one common-stock candidate.
- Ranking: descending LFS score, deterministic ticker tie-break. Store ranks 1 through 10 for each cohort.
- Returns: stock **price** return, because the research question is whether price rises. Entry is the first trading close on/after the score date. End is the first trading close on/after the 1/3/5/10-year anniversary. If the security ceased trading before the target, the final available close is retained and flagged rather than silently dropping the observation.
- Benchmarks: KOSPI or KOSDAQ price index according to the security's market; raw and benchmark-excess returns are both retained.
- Horizons: 1y, 3y, 5y, 10y.
- Statistics: per-rank mean/median return, positive-return hit rate, excess return, 95% interval, equal-weight top-10 cohort return, and score/forward-return Spearman IC.
- Validation gates before UI publication:
  1. no look-ahead violations;
  2. point-in-time universe mapping coverage >= 95%;
  3. financial-score coverage >= 80% of the selected liquid universe;
  4. no rank with silently missing delisted names;
  5. deterministic rerun;
  6. manual spot-check of at least 10 historical company snapshots against original filings;
  7. results clearly label small sample sizes, especially 10-year horizons.

A 10-year forward test available in 2026 necessarily has very few independent annual cohorts. It is reported, but it must not be interpreted as statistically strong evidence.

OpenDART full financial statements are officially provided for business years from 2015 onward, so early backtest cohorts have less history than recent cohorts. The backtest records this rather than using future history to fill the past.

The UI remains disabled until the validation gates pass on the full run. This is deliberate: a partial or survivorship-biased backtest is not published as evidence.
