from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from statistics import mean, median
from typing import Iterable, Sequence


HORIZONS = (1, 3, 5, 10)


@dataclass(frozen=True)
class Observation:
    cohort_year: int
    asof_date: str
    rank: int
    ticker: str
    name: str
    score: float
    horizon_years: int
    start_price: float
    end_price: float
    price_return: float
    benchmark_return: float | None = None
    terminal_source: str = "target_date"

    @property
    def excess_return(self) -> float | None:
        if self.benchmark_return is None:
            return None
        return self.price_return - self.benchmark_return


def parse_date(value: str | date | datetime) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(str(value)[:10]).date()


def assert_point_in_time(filing_date: str | date | datetime | None, asof_date: str | date | datetime) -> None:
    """Raise if a filing used by a score was not publicly available by the score date."""
    if filing_date is None:
        raise ValueError("filing date is missing; point-in-time validity cannot be proven")
    filing = parse_date(filing_date)
    asof = parse_date(asof_date)
    if filing > asof:
        raise ValueError(f"look-ahead detected: filing {filing} is after score date {asof}")


def simple_return(start_price: float, end_price: float) -> float:
    if start_price is None or end_price is None or start_price <= 0:
        raise ValueError("prices must be positive")
    return end_price / start_price - 1.0


def rank_scores(rows: Sequence[dict], top_n: int = 10) -> list[dict]:
    """
    Deterministic descending rank. Ties are broken by ticker so reruns are reproducible.
    Rows with non-finite scores are excluded.
    """
    clean = []
    for row in rows:
        try:
            score = float(row["score"])
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(score):
            continue
        x = dict(row)
        x["score"] = score
        clean.append(x)

    clean.sort(key=lambda r: (-r["score"], str(r.get("ticker", ""))))
    out = []
    for i, row in enumerate(clean[:top_n], start=1):
        x = dict(row)
        x["rank"] = i
        out.append(x)
    return out


def _sample_std(values: Sequence[float]) -> float | None:
    if len(values) < 2:
        return None
    m = mean(values)
    return math.sqrt(sum((x - m) ** 2 for x in values) / (len(values) - 1))


def _normal_ci(values: Sequence[float], z: float = 1.96) -> tuple[float | None, float | None]:
    if len(values) < 2:
        return None, None
    sd = _sample_std(values)
    if sd is None:
        return None, None
    se = sd / math.sqrt(len(values))
    m = mean(values)
    return m - z * se, m + z * se


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    mx, my = mean(xs), mean(ys)
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 0 or vy <= 0:
        return None
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return cov / math.sqrt(vx * vy)


def _ranks(values: Sequence[float]) -> list[float]:
    indexed = sorted(enumerate(values), key=lambda x: x[1])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(indexed):
        j = i + 1
        while j < len(indexed) and indexed[j][1] == indexed[i][1]:
            j += 1
        avg_rank = (i + 1 + j) / 2.0
        for k in range(i, j):
            ranks[indexed[k][0]] = avg_rank
        i = j
    return ranks


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    return _pearson(_ranks(xs), _ranks(ys))


def summarize_rank_observations(observations: Iterable[Observation]) -> dict:
    obs = list(observations)
    by_horizon_rank: dict[tuple[int, int], list[Observation]] = defaultdict(list)
    by_horizon_cohort: dict[tuple[int, int], list[Observation]] = defaultdict(list)

    for o in obs:
        by_horizon_rank[(o.horizon_years, o.rank)].append(o)
        by_horizon_cohort[(o.horizon_years, o.cohort_year)].append(o)

    rank_rows = []
    for horizon in HORIZONS:
        for rank in range(1, 11):
            group = by_horizon_rank.get((horizon, rank), [])
            returns = [x.price_return for x in group]
            excess = [x.excess_return for x in group if x.excess_return is not None]
            lo, hi = _normal_ci(returns)
            rank_rows.append(
                {
                    "horizon_years": horizon,
                    "rank": rank,
                    "n": len(group),
                    "mean_return": mean(returns) if returns else None,
                    "median_return": median(returns) if returns else None,
                    "hit_rate_positive": (
                        sum(r > 0 for r in returns) / len(returns) if returns else None
                    ),
                    "mean_excess_return": mean(excess) if excess else None,
                    "ci95_low": lo,
                    "ci95_high": hi,
                }
            )

    horizon_rows = []
    for horizon in HORIZONS:
        cohort_keys = sorted(
            k for k in by_horizon_cohort if k[0] == horizon
        )
        portfolio_returns = []
        portfolio_excess = []
        score_ic = []
        for _, cohort_year in cohort_keys:
            group = by_horizon_cohort[(horizon, cohort_year)]
            if not group:
                continue
            portfolio_returns.append(mean(x.price_return for x in group))
            ex = [x.excess_return for x in group if x.excess_return is not None]
            if ex:
                portfolio_excess.append(mean(ex))
            # Score/forward-return IC across the 10 names in this cohort.
            scores = [x.score for x in group]
            fwd = [x.price_return for x in group]
            ic = spearman(scores, fwd)
            if ic is not None:
                score_ic.append(ic)

        lo, hi = _normal_ci(portfolio_returns)
        horizon_rows.append(
            {
                "horizon_years": horizon,
                "cohorts": len(portfolio_returns),
                "top10_mean_return": mean(portfolio_returns) if portfolio_returns else None,
                "top10_median_return": median(portfolio_returns) if portfolio_returns else None,
                "top10_mean_excess_return": mean(portfolio_excess) if portfolio_excess else None,
                "mean_score_return_spearman": mean(score_ic) if score_ic else None,
                "ci95_low": lo,
                "ci95_high": hi,
                "statistical_power": (
                    "very_low" if len(portfolio_returns) < 3
                    else "low" if len(portfolio_returns) < 6
                    else "moderate" if len(portfolio_returns) < 12
                    else "higher"
                ),
            }
        )

    return {"rank_summary": rank_rows, "horizon_summary": horizon_rows}
