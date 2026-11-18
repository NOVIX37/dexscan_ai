#!/usr/bin/env python3
"""Execute the current scoring pipeline against repository fixtures and synthetic inputs."""

from __future__ import annotations

import asyncio
import json
import math
import statistics
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from analysis.api_abstraction import (  # noqa: E402
    FundingRecord,
    HolderRecord,
    PairData,
    SecurityReport,
    Tweet,
    XUserProfile,
)
from analysis.core_engine import CoreEngine, CoreResult  # noqa: E402
from analysis.holder_engine import HolderEngine, HolderResult  # noqa: E402
from analysis.momentum_engine import MomentumEngine, MomentumResult  # noqa: E402
from analysis.providers.dexscreener_parser import parse_pairs_response  # noqa: E402
from analysis.providers.rugcheck_parser import parse_report  # noqa: E402
from analysis.providers.solana_rpc_parser import parse_largest_accounts, parse_token_supply  # noqa: E402
from analysis.providers.twitterapi_io_parser import (  # noqa: E402
    parse_followers,
    parse_mentions,
    parse_user_lookup,
)
from analysis.security_engine import SecurityEngine, SecurityResult  # noqa: E402
from analysis.social_engine import SocialEngine, SocialResult  # noqa: E402
from bot.constants import Chain  # noqa: E402
from scoring.formulas import sentiment_score, vulnerability_penalty  # noqa: E402
from scoring.pipeline import ScoringPipeline, ScoringResult  # noqa: E402

FIXTURE_ROOT = REPOSITORY_ROOT / "tests" / "fixtures"
SOLANA_ADDRESS = "So11111111111111111111111111111111111111112"


@dataclass(frozen=True)
class FixtureData:
    dexscreener: dict[str, list[PairData]]
    rugcheck: dict[str, SecurityReport]
    holders: dict[str, dict[str, Any]]
    social: dict[str, dict[str, Any]]


@dataclass(frozen=True)
class ScoredCase:
    name: str
    core: CoreResult
    security: SecurityResult
    holder: HolderResult
    momentum: MomentumResult
    social: SocialResult
    scoring: ScoringResult


class FixtureMarketProvider:
    def __init__(self, pairs: list[PairData]) -> None:
        self._pairs = pairs

    async def get_pairs(self, address: str) -> list[PairData]:
        return self._pairs


class FixtureSecurityProvider:
    def __init__(self, report: SecurityReport) -> None:
        self._report = report

    async def scan(self, address: str, chain: Chain) -> SecurityReport:
        return self._report


class FixtureHolderProvider:
    def __init__(
        self,
        holders: list[HolderRecord],
        funding: list[FundingRecord] | None = None,
    ) -> None:
        self._holders = holders
        self._funding = funding or []

    async def get_holders(self, address: str, chain: Chain) -> list[HolderRecord]:
        return self._holders

    async def get_launch_block_funding(self, address: str, chain: Chain) -> list[FundingRecord]:
        return self._funding


class FixtureSocialProvider:
    def __init__(
        self,
        profile: XUserProfile,
        followers: list[XUserProfile],
        mentions: list[Tweet],
    ) -> None:
        self._profile = profile
        self._followers = followers
        self._mentions = mentions

    async def lookup_user(self, handle: str) -> XUserProfile:
        return self._profile

    async def list_followers(self, handle: str, limit: int) -> list[XUserProfile]:
        return self._followers[:limit]

    async def search_mentions(self, ticker: str) -> list[Tweet]:
        return self._mentions


def _read_directory(name: str) -> dict[str, Any]:
    directory = FIXTURE_ROOT / name
    return {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(directory.glob("*.json"))
    }


def load_fixtures() -> FixtureData:
    raw_dexscreener = _read_directory("dexscreener")
    raw_rugcheck = _read_directory("rugcheck")
    return FixtureData(
        dexscreener={
            name: parse_pairs_response(payload)
            for name, payload in raw_dexscreener.items()
        },
        rugcheck={
            name: parse_report(payload, chain_supported=True)
            for name, payload in raw_rugcheck.items()
        },
        holders=_read_directory("holders"),
        social=_read_directory("social"),
    )


def _holder_records(raw: dict[str, Any], total_supply: float) -> list[HolderRecord]:
    if total_supply <= 0:
        raise ValueError("Holder fixture supply must be positive.")
    return [
        HolderRecord(
            wallet_address=balance.token_account_address,
            token_account_address=balance.token_account_address,
            balance=balance.ui_amount,
            pct_of_supply=balance.ui_amount / total_supply * 100.0,
        )
        for balance in parse_largest_accounts(raw)
    ]


def _holder_cases(fixtures: FixtureData) -> list[tuple[str, FixtureHolderProvider]]:
    supply = parse_token_supply(fixtures.holders["token_supply_standard"])
    cases = [
        (
            name,
            FixtureHolderProvider(_holder_records(raw, supply)),
        )
        for name, raw in sorted(fixtures.holders.items())
        if name.startswith("largest_accounts_")
    ]
    return cases


def _social_cases(fixtures: FixtureData) -> list[tuple[str, FixtureSocialProvider]]:
    profiles = {
        name: parse_user_lookup(raw, requested_handle=name)
        for name, raw in sorted(fixtures.social.items())
        if name.startswith("user_info_")
    }
    followers = {
        name: parse_followers(raw)
        for name, raw in sorted(fixtures.social.items())
        if name.startswith("followers_")
    }
    mentions = {
        name: parse_mentions(raw)
        for name, raw in sorted(fixtures.social.items())
        if name.startswith("mentions_")
    }

    cases = [
        (
            f"{profile_name}+{follower_name}+{mention_name}",
            FixtureSocialProvider(profile, follower_set, mention_set),
        )
        for profile_name, profile in profiles.items()
        for follower_name, follower_set in followers.items()
        for mention_name, mention_set in mentions.items()
    ]
    return cases


async def _core_cases(fixtures: FixtureData) -> list[tuple[str, CoreResult]]:
    cases: list[tuple[str, CoreResult]] = []
    empty_result = await CoreEngine(FixtureMarketProvider([])).analyze(SOLANA_ADDRESS)
    cases.append(("empty_result", empty_result))

    for name, pairs in sorted(fixtures.dexscreener.items()):
        solana_pairs = [pair for pair in pairs if pair.chain is Chain.SOL]
        if not solana_pairs:
            continue
        core = await CoreEngine(FixtureMarketProvider(solana_pairs)).analyze(
            SOLANA_ADDRESS,
            chain_hint=Chain.SOL,
        )
        cases.append((name, core))
    return cases


async def run_fixture_matrix(fixtures: FixtureData | None = None) -> list[ScoredCase]:
    loaded = fixtures or load_fixtures()
    core_cases = await _core_cases(loaded)
    security_cases = sorted(loaded.rugcheck.items())
    holder_cases = _holder_cases(loaded)
    social_cases = _social_cases(loaded)
    scores: list[ScoredCase] = []
    pipeline = ScoringPipeline()
    momentum_engine = MomentumEngine()

    for core_name, core in core_cases:
        for security_name, report in security_cases:
            security = await SecurityEngine(FixtureSecurityProvider(report)).analyze(core)
            for holder_name, provider in holder_cases:
                holder = await HolderEngine(provider).analyze(core)
                for social_name, social_provider in social_cases:
                    social = await SocialEngine(social_provider).analyze("SAMPLE")
                    momentum = momentum_engine.compute(core, holder, social=social)
                    result = pipeline.score(
                        core,
                        security,
                        holder=holder,
                        momentum=momentum,
                        social=social,
                    )
                    scores.append(
                        ScoredCase(
                            name="+".join(
                                (core_name, security_name, holder_name, social_name)
                            ),
                            core=core,
                            security=security,
                            holder=holder,
                            momentum=momentum,
                            social=social,
                            scoring=result,
                        )
                    )
    return scores


async def fixture_vulnerability_penalties(
    fixtures: FixtureData | None = None,
) -> dict[str, float]:
    loaded = fixtures or load_fixtures()
    probe_core = _synthetic_core(age_days=30.0, volume_24h=50_000.0)
    penalties: dict[str, float] = {}
    for name, report in sorted(loaded.rugcheck.items()):
        result = await SecurityEngine(FixtureSecurityProvider(report)).analyze(probe_core)
        penalties[name] = vulnerability_penalty(result)
    return penalties


def _synthetic_core(age_days: float, volume_24h: float) -> CoreResult:
    return CoreResult(
        address=SOLANA_ADDRESS,
        chain=Chain.SOL,
        primary_pair=None,
        liquidity_usd=100_000.0,
        market_cap=500_000.0,
        fdv=1_000_000.0,
        dilution_ratio=0.5,
        volume_24h=volume_24h,
        pool_age_days=age_days,
        price_change={},
        buy_pressure_pct=50.0,
    )


def _synthetic_security(trust_score: float) -> SecurityResult:
    return SecurityResult(
        trust_score=trust_score,
        risk_level="synthetic",
        mint_authority_active=trust_score <= 50.0,
        freeze_authority_active=trust_score <= 20.0,
        buy_tax_pct=0.0,
        sell_tax_pct=99.0 if trust_score <= 20.0 else 0.0,
        lp_lock_ratio_pct=100.0,
        lp_lock_duration_days=365.0,
        ownership_renounced=trust_score >= 80.0,
    )


def _synthetic_holder(hci_pct: float, degraded: bool) -> HolderResult:
    return HolderResult(
        holder_count=20,
        holder_growth_24h_pct=0.0,
        hci_pct=hci_pct,
        whale_count=0,
        degraded=degraded,
        degraded_reason="synthetic missing holder data" if degraded else None,
    )


def _synthetic_momentum(trending_score: float) -> MomentumResult:
    return MomentumResult(
        volume_growth_pct=0.0,
        liquidity_growth_pct=0.0,
        price_momentum=trending_score,
        buy_momentum=0.0,
        whale_momentum=0.0,
        social_momentum=0.0,
        trending_score=trending_score,
    )


def _synthetic_social(sentiment_ratio: float, degraded: bool) -> SocialResult:
    return SocialResult(
        x_score=50,
        verified_follower_ratio=0.0,
        tweet_frequency_per_day=0.0,
        influencer_mention_count=0,
        sentiment_ratio=sentiment_ratio,
        follower_growth_pct=0.0,
        degraded=degraded,
        degraded_reason="synthetic missing social data" if degraded else None,
    )


def run_synthetic_matrix() -> list[ScoredCase]:
    """Run the exact age/volume/trust grid specified by the RM-9 plan."""
    pipeline = ScoringPipeline()
    results: list[ScoredCase] = []
    for age in (0.0, 1.0, 3.0, 7.0, 30.0, 90.0):
        for volume in (10_000.0, 100_000.0, 1_000_000.0, 10_000_000.0):
            for trust in (30.0, 60.0, 90.0):
                core = _synthetic_core(age, volume)
                security = _synthetic_security(trust)
                holder = _synthetic_holder(hci_pct=0.0, degraded=False)
                momentum = _synthetic_momentum(trending_score=0.0)
                social = _synthetic_social(sentiment_ratio=0.0, degraded=False)
                results.append(
                    ScoredCase(
                        name=f"age={age:g},volume={volume:g},trust={trust:g}",
                        core=core,
                        security=security,
                        holder=holder,
                        momentum=momentum,
                        social=social,
                        scoring=pipeline.score(
                            core,
                            security,
                            holder=holder,
                            momentum=momentum,
                            social=social,
                        ),
                    )
                )
    return results


def _distribution(label: str, values: list[float]) -> str:
    if not values:
        raise ValueError(f"No values were generated for {label}.")
    return (
        f"{label}: count={len(values)} min={min(values):.2f} "
        f"max={max(values):.2f} mean={statistics.fmean(values):.2f} "
        f"median={statistics.median(values):.2f}"
    )


def _format_range(values: list[float]) -> str:
    if not values:
        raise ValueError("Cannot report the range of an empty value set.")
    return f"{min(values):.2f}..{max(values):.2f}"


def _print_term_ranges(fixture_cases: list[ScoredCase], synthetic_cases: list[ScoredCase]) -> None:
    terms: tuple[tuple[str, float, Callable[[ScoredCase], float]], ...] = (
        ("VolumeSurge", 0.35, lambda case: case.momentum.volume_growth_pct),
        ("PriceAcceleration", 0.30, lambda case: case.momentum.price_momentum),
        ("HolderGrowth", 0.20, lambda case: case.holder.holder_growth_24h_pct),
        ("SocialMomentum", 0.15, lambda case: case.momentum.social_momentum),
    )
    print("Score_Trend sub-term ranges (input; weighted Score_Trend contribution):")
    for name, weight, value_of in terms:
        fixture_values = [value_of(case) for case in fixture_cases]
        synthetic_values = [value_of(case) for case in synthetic_cases]
        weighted_values = [weight * value for value in fixture_values]
        opportunity_values = [0.4 * value for value in weighted_values]
        nominal_capacity = weight * 100.0
        utilization = max(abs(value) for value in weighted_values) / nominal_capacity * 100.0
        print(
            f"  {name}: weight={weight:.2f}; fixture_input={_format_range(fixture_values)}; "
            f"fixture_weighted={_format_range(weighted_values)}; "
            f"fixture_opportunity_contribution={_format_range(opportunity_values)}; "
            f"synthetic_input={_format_range(synthetic_values)}; "
            f"max_abs_of_nominal_{nominal_capacity:.0f}pt_capacity={utilization:.1f}%"
        )

    volume_contributions = [0.3 * math.log(volume) for volume in (10_000.0, 100_000.0, 1_000_000.0, 10_000_000.0)]
    print(
        "  ln(Volume): weight=0.30 of Score_Opportunity; "
        f"synthetic_weighted={_format_range(volume_contributions)} points; "
        f"nominal_30pt_capacity_use={min(volume_contributions) / 30.0 * 100.0:.1f}%.."
        f"{max(volume_contributions) / 30.0 * 100.0:.1f}%"
    )

    age_confidence = [min(100.0, age * 1.5) for age in (0.0, 1.0, 3.0, 7.0, 30.0, 90.0)]
    age_ai_contribution = [0.3 * value for value in age_confidence]
    print(
        "  PoolAgeDays: coefficient=1.5 confidence points/day, capped at 100; "
        f"matrix_confidence_contribution={_format_range(age_confidence)}; "
        f"Score_AI_contribution={_format_range(age_ai_contribution)}; "
        f"ages_0_to_7_confidence={_format_range(age_confidence[:4])}"
    )
    print(
        "  CodeVerifiedBoolean: coefficient=40 confidence points; "
        "observed=false in every case; contribution=0.00 confidence points "
        "(0.00 Score_AI points; 0% of its 40-point capacity)"
    )


def _low_risk_one_day_case(synthetic_cases: list[ScoredCase]) -> ScoredCase:
    case = next(
        (
            candidate
            for candidate in synthetic_cases
            if candidate.core.pool_age_days == 1.0
            and candidate.core.volume_24h == 10_000.0
            and candidate.security.trust_score == 90.0
        ),
        None,
    )
    if case is None:
        raise RuntimeError("The exact low-risk one-day synthetic case is missing.")
    return case


async def _run_diagnostic() -> None:
    fixtures = load_fixtures()
    fixture_scores = await run_fixture_matrix(fixtures)
    synthetic_scores = run_synthetic_matrix()
    pipeline = ScoringPipeline()
    fixture_v4 = [
        pipeline.score(
            case.core,
            case.security,
            holder=case.holder,
            momentum=case.momentum,
            social=case.social,
            version="v4",
        )
        for case in fixture_scores
    ]
    synthetic_v4 = [
        pipeline.score(
            case.core,
            case.security,
            holder=case.holder,
            momentum=case.momentum,
            social=case.social,
            version="v4",
        )
        for case in synthetic_scores
    ]

    if not fixture_scores:
        raise RuntimeError("The fixture matrix produced no scoring results.")
    if not synthetic_scores:
        raise RuntimeError("The synthetic matrix produced no scoring results.")

    print("DexScan AI RM-9 scoring diagnostic (runtime execution)")
    print(f"Run date (UTC): {datetime.now(timezone.utc).date().isoformat()}")
    print(
        "Loaded fixtures: "
        f"dexscreener={len(fixtures.dexscreener)} "
        f"rugcheck={len(fixtures.rugcheck)} "
        f"holders={len(fixtures.holders)} "
        f"social={len(fixtures.social)}"
    )
    print(
        "Synthetic axes: "
        "age_days=[0, 1, 3, 7, 30, 90]; "
        "volume_usd=[10000, 100000, 1000000, 10000000]; "
        "trust_score=[30, 60, 90]; holder_HCI=0; momentum=0; sentiment=0"
    )
    print(
        "Fixture cases: "
        f"{len(fixture_scores)}; synthetic cases: {len(synthetic_scores)}"
    )

    penalties = await fixture_vulnerability_penalties(fixtures)
    print(f"Fixture vulnerability penalties: {penalties}")

    fixture_ai = [case.scoring.score_ai for case in fixture_scores]
    fixture_opportunity = [case.scoring.score_opportunity for case in fixture_scores]
    fixture_risk = [case.scoring.score_risk for case in fixture_scores]
    fixture_confidence = [case.scoring.score_confidence for case in fixture_scores]
    synthetic_ai = [case.scoring.score_ai for case in synthetic_scores]
    print(_distribution("Fixture AI score", fixture_ai))
    print(_distribution("Fixture opportunity score", fixture_opportunity))
    print(_distribution("Fixture risk score", fixture_risk))
    print(_distribution("Fixture confidence score", fixture_confidence))
    print(_distribution("Synthetic AI score", synthetic_ai))
    print(f"Fixture tier counts: {dict(Counter(case.scoring.tier_label for case in fixture_scores))}")
    print(
        f"Synthetic tier counts: "
        f"{dict(Counter(case.scoring.tier_label for case in synthetic_scores))}"
    )
    print(
        "Synthetic Score_AI bins: "
        f"0-10={sum(score < 10.0 for score in synthetic_ai)}, "
        f"10-20={sum(10.0 <= score < 20.0 for score in synthetic_ai)}, "
        f"20-30={sum(20.0 <= score < 30.0 for score in synthetic_ai)}, "
        f"30-100={sum(score >= 30.0 for score in synthetic_ai)}"
    )

    _print_term_ranges(fixture_scores, synthetic_scores)
    example = _low_risk_one_day_case(synthetic_scores)
    print(
        "Low-risk one-day case (age=1d, volume=$10,000, trust=90, HCI=0, "
        "neutral momentum/social, no vulnerability flags): "
        f"opportunity={example.scoring.score_opportunity:.2f} "
        f"confidence={example.scoring.score_confidence:.2f} "
        f"risk={example.scoring.score_risk:.2f} AI={example.scoring.score_ai:.2f}"
    )
    print(
        "Degraded social sentiment score: "
        f"{sentiment_score(_synthetic_social(-0.9, degraded=True)):.2f}"
    )
    fixture_v4_values = [result.total_score for result in fixture_v4 if result.total_score is not None]
    synthetic_v4_values = [result.total_score for result in synthetic_v4 if result.total_score is not None]
    print("\nExperimental v4 side-by-side diagnostic (not production dispatch)")
    print(
        _distribution("Fixture v4 total", fixture_v4_values)
        + f"; insufficient={len(fixture_v4) - len(fixture_v4_values)}"
    )
    print(
        "Fixture v4 coverage counts: "
        f"{dict(Counter(result.coverage_pct for result in fixture_v4))}"
    )
    print(_distribution("Synthetic v4 total", synthetic_v4_values))
    safe_values = [
        result.total_score
        for case, result in zip(fixture_scores, fixture_v4)
        if result.total_score is not None
        and case.security.trust_score >= 80.0
        and not case.security.scam_flags
    ]
    honeypot_values = [
        result.total_score
        for case, result in zip(fixture_scores, fixture_v4)
        if result.total_score is not None and case.security.sell_tax_pct >= 99.0
    ]
    if not safe_values or not honeypot_values:
        raise RuntimeError("The fixture matrix did not produce both safe and honeypot v4 cases.")
    print(
        "Fixture v4 high-trust/no-flag: "
        f"count={len(safe_values)} range={min(safe_values):.2f}..{max(safe_values):.2f} "
        f"median={statistics.median(safe_values):.2f}"
    )
    print(
        "Fixture v4 honeypot (sell tax >=99%): "
        f"count={len(honeypot_values)} range={min(honeypot_values):.2f}..{max(honeypot_values):.2f} "
        f"median={statistics.median(honeypot_values):.2f}"
    )
    print(
        "Fixture v4 overlap at cutoff 45: "
        f"high-trust/no-flag below={sum(value < 45.0 for value in safe_values)}; "
        f"honeypot at/above={sum(value >= 45.0 for value in honeypot_values)}"
    )
    v4_example = synthetic_v4[synthetic_scores.index(example)]
    print(
        "Low-risk one-day case v4: "
        f"score={v4_example.total_score:.2f} "
        f"coverage={v4_example.coverage_pct:.0f}% "
        f"excluded={v4_example.excluded_components}"
        if v4_example.total_score is not None
        else "Low-risk one-day case v4: Insufficient Data"
    )
    print("Diagnostic execution completed successfully.")


def main() -> None:
    asyncio.run(_run_diagnostic())


if __name__ == "__main__":
    main()
