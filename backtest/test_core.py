from backtest.core import (
    Observation,
    assert_point_in_time,
    rank_scores,
    simple_return,
    summarize_rank_observations,
)


def test_rank_scores_is_descending_and_deterministic():
    rows = [
        {"ticker": "B", "score": 80},
        {"ticker": "A", "score": 80},
        {"ticker": "C", "score": 70},
    ]
    ranked = rank_scores(rows, top_n=3)
    assert [x["ticker"] for x in ranked] == ["A", "B", "C"]
    assert [x["rank"] for x in ranked] == [1, 2, 3]


def test_no_lookahead_guard():
    assert_point_in_time("2020-03-30", "2020-06-01")
    try:
        assert_point_in_time("2020-06-02", "2020-06-01")
    except ValueError as e:
        assert "look-ahead" in str(e)
    else:
        raise AssertionError("look-ahead was not detected")


def test_simple_return():
    assert abs(simple_return(100, 120) - 0.2) < 1e-12


def test_summary_has_rank_and_horizon_outputs():
    obs = []
    for cohort in (2020, 2021, 2022):
        for rank in range(1, 11):
            obs.append(
                Observation(
                    cohort_year=cohort,
                    asof_date=f"{cohort}-06-01",
                    rank=rank,
                    ticker=f"T{rank}",
                    name=f"N{rank}",
                    score=100-rank,
                    horizon_years=1,
                    start_price=100,
                    end_price=100 + (11-rank),
                    price_return=(11-rank)/100,
                    benchmark_return=0.02,
                )
            )
    out = summarize_rank_observations(obs)
    r1 = next(x for x in out["rank_summary"] if x["horizon_years"] == 1 and x["rank"] == 1)
    assert r1["n"] == 3
    assert r1["mean_return"] > 0
    h1 = next(x for x in out["horizon_summary"] if x["horizon_years"] == 1)
    assert h1["cohorts"] == 3
    assert h1["mean_score_return_spearman"] > 0.9
