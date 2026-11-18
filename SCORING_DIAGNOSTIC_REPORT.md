# DexScan AI - RM-9 Scoring Diagnostic

- **Status:** Runtime diagnostic completed against local fixtures and the exact deterministic matrix specified by RM-9
- **Run date (UTC):** 2026-11-17
- **Scoring versions:** v1, v2, and v3 remain unchanged and available

## What was executed

`scripts/diagnostic_rm9.py` loads the repository fixtures through the real provider parsers, runs the actual Core, Security, Holder, Social, and Momentum engines with fixture-backed providers, and sends the results through `ScoringPipeline`. No live network calls are made.

The run loaded 7 DexScreener, 7 RugCheck, 10 holder, and 11 social fixture files. It scored 3,780 fixture combinations and generated the required 72 synthetic combinations: age `[0, 1, 3, 7, 30, 90]` days, volume `[$10K, $100K, $1M, $10M]`, and trust `[30, 60, 90]`. Synthetic HCI, momentum, and social sentiment were held at neutral defaults. All cases reached the v3 scoring pipeline. The fixture cross-product is a diagnostic harness, not a population sample or calibrated model dataset.

Command:

```text
python scripts/diagnostic_rm9.py
```

Complete observed output:

```text
DexScan AI RM-9 scoring diagnostic (runtime execution)
Run date (UTC): 2026-11-17
Loaded fixtures: dexscreener=7 rugcheck=7 holders=10 social=11
Synthetic axes: age_days=[0, 1, 3, 7, 30, 90]; volume_usd=[10000, 100000, 1000000, 10000000]; trust_score=[30, 60, 90]; holder_HCI=0; momentum=0; sentiment=0
Fixture cases: 3780; synthetic cases: 72
Fixture vulnerability penalties: {'combined_mint_and_unrenounced': 40.0, 'freeze_authority_active': 100.0, 'fully_safe': 0.0, 'honeypot': 100.0, 'mint_authority_active': 40.0, 'moderate_tax': 0.0, 'unrenounced_ownership_only': 20.0}
Fixture AI score: count=3780 min=0.00 max=53.22 mean=9.42 median=1.30
Fixture opportunity score: count=3780 min=0.00 max=39.61 mean=16.84 median=15.00
Fixture risk score: count=3780 min=4.50 max=73.10 mean=30.54 median=21.00
Fixture confidence score: count=3780 min=0.00 max=100.00 mean=66.67 median=100.00
Synthetic AI score: count=72 min=0.00 max=40.88 mean=8.52 median=3.97
Fixture tier counts: {'Critical Risk': 3465, 'Elevated Risk': 303, 'Mixed Signals': 12}
Synthetic tiers: {'Critical Risk': 68, 'Elevated Risk': 4}
Synthetic Score_AI bins: 0-10=45, 10-20=15, 20-30=8, 30-100=4
Score_Trend sub-term ranges (input; weighted Score_Trend contribution):
  VolumeSurge: weight=0.35; fixture_input=0.00..0.00; fixture_weighted=0.00..0.00; fixture_opportunity_contribution=0.00..0.00; synthetic_input=0.00..0.00; max_abs_of_nominal_35pt_capacity=0.0%
  PriceAcceleration: weight=0.30; fixture_input=-2.97..0.00; fixture_weighted=-0.89..0.00; fixture_opportunity_contribution=-0.36..0.00; synthetic_input=0.00..0.00; max_abs_of_nominal_30pt_capacity=3.0%
  HolderGrowth: weight=0.20; fixture_input=0.00..0.00; fixture_weighted=0.00..0.00; fixture_opportunity_contribution=0.00..0.00; synthetic_input=0.00..0.00; max_abs_of_nominal_20pt_capacity=0.0%
  SocialMomentum: weight=0.15; fixture_input=-100.00..100.00; fixture_weighted=-15.00..15.00; fixture_opportunity_contribution=-6.00..6.00; synthetic_input=0.00..0.00; max_abs_of_nominal_15pt_capacity=100.0%
  ln(Volume): weight=0.30 of Score_Opportunity; synthetic_weighted=2.76..4.84 points; nominal_30pt_capacity_use=9.2%..16.1%
  PoolAgeDays: coefficient=1.5 confidence points/day, capped at 100; matrix_confidence_contribution=0.00..100.00; Score_AI_contribution=0.00..30.00; ages_0_to_7_confidence=0.00..10.50
  CodeVerifiedBoolean: coefficient=40 confidence points; observed=false in every case; contribution=0.00 confidence points (0.00 Score_AI points; 0% of its 40-point capacity)
Low-risk one-day case (age=1d, volume=$10,000, trust=90, HCI=0, neutral momentum/social, no vulnerability flags): opportunity=17.76 confidence=1.50 risk=3.00 AI=9.88
Degraded social sentiment score: 50.00
Diagnostic execution completed successfully.
```

Correction: the runner's output reports **72** synthetic cases; the earlier report text mistakenly claimed 8,640 and is superseded here. Fixture-driven pool age uses fixture timestamps and the current clock, so its distribution is an observation from this run, not a fixed golden baseline. Synthetic inputs use fixed ages.

## Formula-term utilization

The table below distinguishes the raw fixture input range, its weighted `Score_Trend` contribution, and its downstream contribution to `Score_Opportunity` (`0.4 * Score_Trend`). The momentum inputs are raw mixed-scale values and the composite is not clamped, so utilization percentages are only a rough comparison against a hypothetical normalized 0–100 input; the raw ranges and contribution points are the primary evidence.

| Term | Formula weight/coefficient | Observed input range | Weighted contribution | Uses a meaningful fraction? |
|---|---:|---:|---:|---|
| VolumeSurge | 0.35 in `Score_Trend` (0.14 downstream in Opportunity) | 0 in fixtures and synthetic cases | 0.00 points | No — no historical volume snapshots; permanently 0 in `MomentumEngine` |
| PriceAcceleration | 0.30 (0.12 downstream) | -2.97 to 0.00 in fixtures; 0 in neutral synthetic cases | -0.89 to 0.00 `Score_Trend`; -0.36 to 0.00 Opportunity | No in these fixtures — about 3% of a hypothetical 30-point normalized term capacity |
| HolderGrowth | 0.20 (0.08 downstream) | 0 in fixtures and synthetic cases | 0.00 points | No observed use in this run. Not structurally fixed at zero: the holder engine can report recent observed top-holder funding, but the available fixtures provide none |
| SocialMomentum | 0.15 (0.06 downstream) | -100 to 100 in fixtures; 0 in neutral synthetic cases | -15 to 15 `Score_Trend`; -6 to 6 Opportunity | Yes — observed range spans the full nominal normalized term capacity |
| `ln(Volume_24h)` | 0.30 in Opportunity | $10K–$10M matrix | 2.76–4.84 Opportunity points | No — only 9.2%–16.1% of a nominal 30-point term capacity on this scale |
| Pool-age confidence | 1.5 confidence points/day; confidence is weighted 0.30 in AI | ages 0–90 days | 0–100 confidence points (0–30 AI points); ages 0–7 yield only 0–10.5 confidence points (0–3.15 AI points) | New-pool range is compressed; ages above 66.7 days saturate the confidence term |
| `CodeVerifiedBoolean` | 40 confidence points; confidence is weighted 0.30 in AI | `False` in every case | 0 confidence points / 0 AI points | No — source field does not exist in current result contracts |

Neutral sentiment is a separate `0.30 * SentimentScore` contribution: sentiment score 50 yields 15 Opportunity points (half of its 30-point nominal capacity). Social evidence also enters `Score_Trend` through `SocialMomentum`, so the same underlying sentiment signal is represented in both terms.

## Formula findings

### Confirmed by execution

- **VolumeSurge is always zero in this build.** `MomentumEngine.compute` sets `volume_growth_pct = 0.0` because it has no historical volume snapshots. The 35%-weighted term in `Score_Trend` therefore contributes zero.
- **The current opportunity volume term is compressed.** In the required matrix, `0.3 * ln(volume_24h)` contributes 2.76–4.84 points for $10K–$10M, only 9.2%–16.1% of a nominal 30-point term capacity.
- **A one-day-old pool gets only 1.5 confidence points** (`pool_age_days * 1.5`), or 0.45 points in the AI combination before risk is applied.
- **Risk is subtracted at its full 0-100 scale** after the weighted opportunity/confidence sum. `Score_Risk` itself is composed from trust, holder concentration, and vulnerability. The observed score distributions are heavily concentrated in the lowest tier; this fixture/synthetic result is not sufficient to calibrate new tier boundaries.
- **Degraded social data maps to the same numeric midpoint as neutral sentiment** (`SentimentScore=50`). The v3 explanation says that social data is unavailable, but the number alone does not distinguish unavailable from measured-neutral. Relative to a fully negative sentiment reading, this midpoint adds 15 opportunity points (10.5 AI points, all else equal).
- **Missing code verification is hard-coded as `False` in confidence.** No code-verification source exists in the current result contracts, so the model cannot distinguish unavailable from unverified.
- **Degraded holder data contributes no concentration penalty.** `holder_concentration_penalty` returns zero for a degraded result, while the explanation reports the missing data. The numeric result does not expose coverage.
- **Social signals are used in both `Score_Trend` and `SentimentScore`** in v3. This is present in the existing formula and can count related evidence twice. The requested future Hybrid design should avoid that overlap.
- **HolderGrowth is zero across the available fixtures** (weighted range 0 points). This is not a permanent code-level zero: `HolderEngine._growth_from_funding` can report recent observed top-holder funding, but no included fixture provided funding timestamps in the last 24 hours.
- **PriceAcceleration is narrowly used by the available fixtures** (-2.97 to 0.00 raw; -0.89 to 0.00 after its `Score_Trend` weight), only about 3% of a hypothetical 30-point normalized term capacity. This is an observed fixture limitation, not proof the formula is structurally unable to produce larger values.
- `liquidity_growth_pct` and `whale_momentum` are also explicit 0.0 gaps in `MomentumEngine`, but neither is a `Score_Trend` term. `buy_momentum` is computed but is not included in the four-term `Score_Trend` formula.

### Corrected fixture calculations

The prior report incorrectly listed `combined_mint_and_unrenounced.json` as a 100-point vulnerability penalty and `moderate_tax.json` as 20:

- The combined fixture has an active mint authority and unrenounced ownership, but no honeypot-level sell tax or freeze authority. The implementation's ordered checks return **40** for mint authority before reaching ownership.
- The moderate-tax fixture has a 14% transfer fee, no active mint/freeze authority, and ownership inferred as renounced. The vulnerability formula only gives 100 for sell tax >=99% or freeze authority, 40 for mint authority, and 20 for unrenounced ownership; therefore its **vulnerability penalty is 0**. Its tax finding is still available in the security result, but it does not independently enter this sub-formula.

Other fixture penalties were confirmed: fully safe 0; active freeze 100; honeypot 100; mint authority 40; unrenounced ownership only 20.

## Interpretation and boundaries

This is **synthetic diagnostic execution**, not a statistical validation, integration test against live providers, or live Telegram/API test. The tests validate formula behavior and fixture parsing; they do not establish predictive performance. The distributions demonstrate the existing formula's compression and tier skew but should not be used to set replacement tier thresholds without representative, labeled historical outcomes.

No legacy production scoring formula or default pipeline dispatch was changed. v4 remains opt-in. Any future production migration should wait for adequate validation. Holder concentration belongs in Security/safety, while Core/Market and Momentum should remain distinct.

## RM-11 experimental v4 side-by-side diagnostic

The owner-approved RM-10 contract is implemented as an explicit
`version="v4"` scoring path. Calls without that selector still use the
existing automatic v1/v2/v3 dispatch; production scan orchestration
continues to select v3. v4 uses same-direction, equal-weight components,
excludes unavailable components from numerator and denominator, reports
coverage separately, and does not apply the old tier table or
Risk/Opportunity Matrix.

The updated `scripts/diagnostic_rm9.py` re-runs both matrices using the
same fixture-loaded engine outputs and also runs the v4 path. No live
provider or Telegram calls are made. Observed v4 output:

| Matrix | Results | Numeric totals | Distribution / coverage |
|---|---:|---:|---|
| Fixture cross-product | 3,780 | 3,304 scored; 476 Insufficient Data | min 13.33, max 88.33, mean 45.69, median 49.13 |
| Fixture coverage | 3,780 | — | 0%: 476; 25%: 784; 50%: 476; 75%: 1,260; 100%: 784 |
| Synthetic matrix | 72 | 72 scored | min 32.22, max 52.22, mean/median 42.22 |

The approved Security mapping is the minimum of normalized trust, inverse
Holder concentration, and inverse vulnerability penalty. With this
mapping, high-trust/no-flag cases scored **37.22–71.64** (median
**54.06**); honeypots scored **15.56–45.13** (median **30.73**). At a
cutoff of 45, 19 of 360 high-trust/no-flag cases are below the cutoff and
4 of 360 honeypot cases are at or above it. The minimum mapping improves
median separation but does not eliminate fixture overlap.

The RM-9 low-risk one-day synthetic case scored **9.88** under v3 and
**52.22** under v4. Its Core/Market component was excluded because the
synthetic case has no primary pair, leaving 75% coverage. This is a
worked diagnostic example, not a fully observed token or proof of
predictive performance. Likewise, the fixture cross-product and
synthetic inputs are not labeled outcome data.

v4 remains experimental and opt-in. The market field parser still maps
omitted numeric fields to zero, which prevents distinguishing an omitted
individual field from a measured zero. The owner directed that the
Risk/Opportunity Matrix be omitted for v4 only; v1/v2/v3 behavior is
unchanged.

The previous blended Security mapping had high-trust/no-flag totals of
**43.62–72.39** (median **58.90**) and honeypot totals of **24.52–54.68**
(median **39.70**). The revised, owner-approved mapping improves median
separation but still leaves overlap (**37.22–45.13**). Therefore, RM-12
tier calibration remains **blocked by owner direction**: threshold
selection alone does not satisfy the plan's safe-vs-risky fixture
acceptance. The owner confirmed keeping v4 **Uncalibrated**; no
replacement tier bands are proposed. This fixture diagnostic is not
representative labeled outcome data.

## Verification

- Baseline before this continuation: `.venv\Scripts\python.exe -m pytest -q` — **586 passed**.
- Exact RM-9 matrix regression: `.venv\Scripts\python.exe -m pytest -q tests\test_diagnostic_rm9.py` — **5 passed**.
- Runtime diagnostic: `.venv\Scripts\python.exe scripts\diagnostic_rm9.py` — completed successfully; exact output is included above.
- Runner strict type check: `.venv\Scripts\python.exe -m mypy --strict --follow-imports=silent scripts\diagnostic_rm9.py` — **passed**.
- Final combined verification: `.venv\Scripts\python.exe -m pytest -q` — **600 passed** after RM-6, RM-9, RM-13, RM-14, and RM-8 changes.
- RM-11 focused suite after the Security revision: `.venv\Scripts\python.exe -m pytest -q tests\test_scoring_v4.py tests\test_scoring_v1.py tests\test_scoring_v2.py tests\test_scoring_v3.py tests\test_result_renderer.py tests\test_diagnostic_rm9.py` — **132 passed**.
- RM-11 fixture/synthetic diagnostic after the Security revision: `.venv\Scripts\python.exe scripts\diagnostic_rm9.py` — completed successfully; current v4 distributions, safe/honeypot overlap, and worked example are recorded above.
- Targeted strict mypy: `.venv\Scripts\python.exe -m mypy --strict --follow-imports=silent scoring\formulas.py scoring\pipeline.py rendering\result_renderer.py handlers\scan_orchestration.py scripts\diagnostic_rm9.py` — **passed**.
- Final full-suite verification after the Security revision: `.venv\Scripts\python.exe -m pytest -q` — **614 passed**; existing `pytest-asyncio` deprecation warnings remain.
- Plain `mypy --strict .` is **not clean**: **405 errors in 32 files** across production code, tests, and untracked scripts. This broader pre-existing type debt was not changed as part of RM-9.
- No live provider or Telegram API verification was attempted; those tests remain unrun.

The authoritative Engineering Remediation & Scoring Redesign Plan remains the source of scope. RM-14 Session History and RM-6/RM-13 navigation/social diagnostics have since been implemented separately; RM-9's exact matrix, sub-term table, corrected output, and final combined-suite result are now recorded here. The production scoring formulas and v1/v2/v3 dispatch remain unchanged.
