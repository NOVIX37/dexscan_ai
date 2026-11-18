from __future__ import annotations

import pytest

from scripts.diagnostic_rm9 import (
    fixture_vulnerability_penalties,
    load_fixtures,
    run_fixture_matrix,
    run_synthetic_matrix,
)


def test_fixture_loader_reads_all_four_input_groups() -> None:
    fixtures = load_fixtures()

    assert fixtures.dexscreener
    assert fixtures.rugcheck
    assert fixtures.holders
    assert fixtures.social
    assert any(fixtures.dexscreener.values())


@pytest.mark.asyncio
async def test_rugcheck_fixture_penalties_match_the_executed_security_formula() -> None:
    penalties = await fixture_vulnerability_penalties()

    assert penalties["combined_mint_and_unrenounced"] == 40.0
    assert penalties["moderate_tax"] == 0.0
    assert penalties["fully_safe"] == 0.0
    assert penalties["freeze_authority_active"] == 100.0
    assert penalties["honeypot"] == 100.0
    assert penalties["mint_authority_active"] == 40.0
    assert penalties["unrenounced_ownership_only"] == 20.0


@pytest.mark.asyncio
async def test_fixture_matrix_runs_the_v3_pipeline_for_fixture_combinations() -> None:
    results = await run_fixture_matrix()

    assert len(results) > 0
    assert all(case.scoring.pipeline_version == "v3" for case in results)
    assert all(0.0 <= case.scoring.score_ai <= 100.0 for case in results)
    assert any(case.core.degraded for case in results)
    assert any(case.social.degraded for case in results)


def test_synthetic_matrix_is_finite_and_stays_within_score_bounds() -> None:
    results = run_synthetic_matrix()

    assert len(results) == 72
    assert all(case.scoring.pipeline_version == "v3" for case in results)
    assert all(0.0 <= case.scoring.score_ai <= 100.0 for case in results)
    assert all(0.0 <= case.scoring.score_opportunity <= 100.0 for case in results)
    assert all(0.0 <= case.scoring.score_risk <= 100.0 for case in results)
    assert {case.core.pool_age_days for case in results} == {0.0, 1.0, 3.0, 7.0, 30.0, 90.0}
    assert {case.core.volume_24h for case in results} == {
        10_000.0, 100_000.0, 1_000_000.0, 10_000_000.0
    }
    assert {case.security.trust_score for case in results} == {30.0, 60.0, 90.0}
    assert all(case.holder.hci_pct == 0.0 for case in results)
    assert all(case.momentum.trending_score == 0.0 for case in results)
    assert all(case.social.sentiment_ratio == 0.0 for case in results)


def test_low_risk_one_day_case_scores_near_zero_by_formula() -> None:
    case = next(
        candidate
        for candidate in run_synthetic_matrix()
        if candidate.core.pool_age_days == 1.0
        and candidate.core.volume_24h == 10_000.0
        and candidate.security.trust_score == 90.0
    )

    assert case.scoring.score_risk == pytest.approx(3.0)
    assert case.scoring.score_confidence == pytest.approx(1.5)
    assert case.scoring.score_ai == pytest.approx(9.884171478115)
