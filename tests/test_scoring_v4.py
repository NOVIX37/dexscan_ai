from __future__ import annotations

from statistics import median

import pytest

from analysis.api_abstraction import PairData, SocialReasonCode
from analysis.core_engine import CoreResult
from analysis.holder_engine import HolderResult
from analysis.momentum_engine import MomentumResult
from analysis.security_engine import SecurityResult
from analysis.social_engine import SocialResult
from bot.constants import Chain
from handlers.scan_orchestration import ScoredResult
from rendering.result_renderer import render_result_detail
from scoring.formulas import (
    score_core_market_v4,
    score_momentum_v4,
    score_security_v4,
    score_social_v4,
)
from scoring.pipeline import ScoringPipeline, ScoringResultV4
from scripts.diagnostic_rm9 import run_fixture_matrix, run_synthetic_matrix


def _pair() -> PairData:
    return PairData(
        chain=Chain.SOL,
        dex_id="test-dex",
        pair_address="pair",
        base_token_address="token",
        base_token_symbol="TST",
        base_token_name="Test Token",
        quote_token_symbol="SOL",
        price_usd=1.0,
        liquidity_usd=100_000.0,
        fdv=1_000_000.0,
        market_cap=750_000.0,
        volume_5m=0.0,
        volume_1h=0.0,
        volume_6h=0.0,
        volume_24h=1_000_000.0,
        price_change_5m=0.0,
        price_change_1h=0.0,
        price_change_6h=0.0,
        price_change_24h=0.0,
        buys_24h=0,
        sells_24h=0,
        pair_created_at_ms=None,
    )


def _core(**overrides) -> CoreResult:
    values = dict(
        address="TokenAddress",
        chain=Chain.SOL,
        primary_pair=_pair(),
        liquidity_usd=100_000.0,
        market_cap=750_000.0,
        fdv=1_000_000.0,
        dilution_ratio=0.75,
        volume_24h=1_000_000.0,
        pool_age_days=1.0,
        price_change={"1h": 0.0, "6h": 0.0},
        buy_pressure_pct=50.0,
    )
    values.update(overrides)
    return CoreResult(**values)


def _security(**overrides) -> SecurityResult:
    values = dict(
        trust_score=90.0,
        risk_level="Low Risk",
        mint_authority_active=False,
        freeze_authority_active=False,
        buy_tax_pct=0.0,
        sell_tax_pct=0.0,
        lp_lock_ratio_pct=100.0,
        lp_lock_duration_days=365.0,
        ownership_renounced=True,
    )
    values.update(overrides)
    return SecurityResult(**values)


def _holder(**overrides) -> HolderResult:
    values = dict(
        holder_count=20,
        holder_growth_24h_pct=25.0,
        hci_pct=10.0,
        whale_count=1,
        classified_wallets={},
    )
    values.update(overrides)
    return HolderResult(**values)


def _momentum(**overrides) -> MomentumResult:
    values = dict(
        volume_growth_pct=0.0,
        liquidity_growth_pct=0.0,
        price_momentum=0.0,
        buy_momentum=0.0,
        whale_momentum=0.0,
        social_momentum=0.0,
        trending_score=0.0,
    )
    values.update(overrides)
    return MomentumResult(**values)


def _social(**overrides) -> SocialResult:
    values = dict(
        x_score=60,
        verified_follower_ratio=0.5,
        tweet_frequency_per_day=2.0,
        influencer_mention_count=1,
        sentiment_ratio=0.0,
        follower_growth_pct=0.0,
    )
    values.update(overrides)
    return SocialResult(**values)


def test_core_market_normalizes_log_levels_and_dilution() -> None:
    core = _core(liquidity_usd=100_000.0, volume_24h=1_000_000.0, dilution_ratio=0.75)

    assert score_core_market_v4(core) == pytest.approx((100.0 / 3.0 + 200.0 / 3.0 + 75.0) / 3.0)
    assert score_core_market_v4(
        _core(volume_24h=10_000_000.0, liquidity_usd=10_000_000.0)
    ) == pytest.approx(91.6666666667)


def test_security_v4_uses_lowest_safety_signal() -> None:
    security = _security(trust_score=65.0, mint_authority_active=True)
    holder = _holder(hci_pct=40.0)

    assert score_security_v4(security, holder) == pytest.approx(60.0)
    assert score_security_v4(security, _holder(degraded=True)) is None


def test_security_v4_does_not_dilute_critical_vulnerability() -> None:
    security = _security(trust_score=95.0, freeze_authority_active=True)

    assert score_security_v4(security, _holder(hci_pct=10.0)) == pytest.approx(0.0)


def test_momentum_v4_uses_three_distinct_non_social_signals() -> None:
    momentum = _momentum(price_momentum=200.0, buy_momentum=-100.0)

    assert score_momentum_v4(momentum, _holder(holder_growth_24h_pct=50.0)) == pytest.approx(50.0)
    assert score_momentum_v4(_momentum(degraded=True), _holder()) is None


def test_social_v4_normalizes_available_signals_and_excludes_reason_coded_result() -> None:
    assert score_social_v4(_social()) == pytest.approx((60.0 + 50.0 + 50.0) / 3.0)
    assert score_social_v4(_social(reason_code=SocialReasonCode.NO_API_KEY)) is None


def test_pipeline_v4_averages_available_components_and_records_exclusions() -> None:
    result = ScoringPipeline().score(
        _core(),
        _security(),
        holder=_holder(),
        momentum=_momentum(),
        social=_social(reason_code=SocialReasonCode.NO_API_KEY),
        version="v4",
    )

    assert isinstance(result, ScoringResultV4)
    assert result.pipeline_version == "v4"
    assert result.included_components == ["Core/Market", "Security", "Momentum"]
    assert result.excluded_components == [("Social", SocialReasonCode.NO_API_KEY.value)]
    assert result.coverage_pct == 75.0
    assert result.total_score == pytest.approx(sum(result.component_scores.values()) / 3.0)
    assert result.tier_label == "Uncalibrated v4"


def test_pipeline_v4_excludes_security_and_momentum_when_holder_data_is_degraded() -> None:
    result = ScoringPipeline().score(
        _core(),
        _security(),
        holder=_holder(degraded=True, degraded_reason="holder RPC unavailable"),
        momentum=_momentum(),
        social=_social(),
        version="v4",
    )

    assert result.included_components == ["Core/Market", "Social"]
    assert result.coverage_pct == 50.0
    assert [name for name, _ in result.excluded_components] == ["Security", "Momentum"]


def test_pipeline_v4_returns_insufficient_data_without_a_fabricated_total() -> None:
    result = ScoringPipeline().score(
        _core(degraded=True, primary_pair=None, dilution_ratio=None),
        _security(degraded=True, degraded_reason="security unavailable"),
        holder=_holder(degraded=True),
        momentum=_momentum(degraded=True),
        social=_social(degraded=True, reason_code=SocialReasonCode.PROVIDER_TIMEOUT),
        version="v4",
    )

    assert result.total_score is None
    assert result.coverage_pct == 0.0
    assert result.tier_label == "Insufficient Data"
    assert len(result.excluded_components) == 4


def test_default_pipeline_still_selects_v3_without_changing_legacy_dispatch() -> None:
    result = ScoringPipeline().score(_core(), _security(), _holder(), _momentum(), _social())

    assert result.pipeline_version == "v3"


@pytest.mark.asyncio
async def test_v4_is_evaluated_across_full_fixture_matrix_with_safe_and_risky_cases_separated() -> None:
    # The fixture matrix exercises the real engines and provider parsers; v4
    # is then run against the exact same resolved inputs.
    source_cases = await run_fixture_matrix()
    pipeline = ScoringPipeline()
    values: list[float] = []
    safe: list[float] = []
    risky: list[float] = []
    for case in source_cases:
        result = pipeline.score(
            case.core,
            case.security,
            holder=case.holder,
            momentum=case.momentum,
            social=case.social,
            version="v4",
        )
        if result.total_score is None:
            continue
        values.append(result.total_score)
        if case.security.trust_score >= 80.0 and not case.security.scam_flags:
            safe.append(result.total_score)
        if case.security.sell_tax_pct >= 99.0 or case.security.freeze_authority_active:
            risky.append(result.total_score)

    assert len(values) > 0
    assert len(values) < len(source_cases)
    assert all(0.0 <= value <= 100.0 for value in values)
    assert max(values) - min(values) > 10.0
    assert safe and risky
    assert median(safe) > median(risky)


def test_v4_synthetic_matrix_is_differentiated_and_finite() -> None:
    pipeline = ScoringPipeline()
    scores = [
        pipeline.score(
            case.core,
            case.security,
            holder=case.holder,
            momentum=case.momentum,
            social=case.social,
            version="v4",
        )
        for case in run_synthetic_matrix()
    ]
    values = [result.total_score for result in scores if result.total_score is not None]

    assert len(scores) == 72
    assert len(values) == 72
    assert all(0.0 <= value <= 100.0 for value in values)
    assert max(values) - min(values) > 5.0


def test_low_risk_new_pool_synthetic_example_escapes_v3_single_digits() -> None:
    case = next(
        candidate
        for candidate in run_synthetic_matrix()
        if candidate.core.pool_age_days == 1.0
        and candidate.core.volume_24h == 10_000.0
        and candidate.security.trust_score == 90.0
    )
    result = ScoringPipeline().score(
        case.core,
        case.security,
        holder=case.holder,
        momentum=case.momentum,
        social=case.social,
        version="v4",
    )

    assert result.total_score is not None
    assert result.total_score > 10.0
    assert result.total_score > case.scoring.score_ai


def test_rendered_v4_result_discloses_coverage_and_social_exclusion_reason() -> None:
    core = _core()
    holder = _holder()
    momentum = _momentum()
    social = _social(reason_code=SocialReasonCode.NO_API_KEY)
    scoring = ScoringPipeline().score(
        core, _security(), holder=holder, momentum=momentum, social=social, version="v4"
    )
    rendered = render_result_detail(
        ScoredResult(
            core=core,
            security=_security(),
            holder=holder,
            momentum=momentum,
            social=social,
            scoring=scoring,
            risk_opportunity=None,
        )
    )

    assert "Coverage: 75% (3 of 4 components included)" in rendered.html
    assert "Social component excluded: No API key is configured for social analysis." in rendered.html
    assert "Risk / Opportunity Read" not in rendered.html


def test_rendered_zero_coverage_is_insufficient_data_not_a_zero_score() -> None:
    core = _core(degraded=True, primary_pair=None, dilution_ratio=None)
    security = _security(degraded=True)
    holder = _holder(degraded=True)
    momentum = _momentum(degraded=True)
    social = _social(degraded=True, reason_code=SocialReasonCode.NO_API_KEY)
    scoring = ScoringPipeline().score(
        core, security, holder=holder, momentum=momentum, social=social, version="v4"
    )

    rendered = render_result_detail(
        ScoredResult(
            core=core,
            security=security,
            holder=holder,
            momentum=momentum,
            social=social,
            scoring=scoring,
            risk_opportunity=None,
        )
    )

    assert "AI Score: <b>Insufficient Data</b>" in rendered.html
    assert "Coverage: 0% (0 of 4 components included)" in rendered.html
    assert "0/100" not in rendered.html
