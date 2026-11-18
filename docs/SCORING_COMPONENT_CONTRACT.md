# Scoring Component Contract

**Status:** Owner-approved for opt-in RM-11 implementation, including the
revised conservative Security sub-score below.

**Version:** 1.1
**Scope:** Additive scoring version only. Existing v1/v2/v3 formulas,
dispatch, tiers, and production default remain unchanged.

## Evidence and owner direction

The RM-9 diagnostic executed the current v3 pipeline across 3,780 fixture
combinations and the specified 72-case synthetic matrix. It found a
compressed score distribution and confirmed that missing Social data
currently maps to neutral sentiment. This is diagnostic evidence, not
calibration data; it does not justify changing the existing formula or
setting replacement tier boundaries.

The owner explicitly selected the **Hybrid** component boundary:
**Core/Market, Security, Momentum, and Social**. Holder concentration is
part of Security as a safety adjustment, not an independent positive
opportunity axis. Core/Market and Momentum must have distinct
responsibilities to avoid double-counting.

The owner approved a common polarity of **100 = strongest/safer and
0 = weakest/riskier**. This follows the existing `SecurityReport` and
`SecurityResult.trust_score` convention, which is already 0–100 with
higher values meaning safer.

## Component contract

All available components are normalized to 0–100 in the common positive
direction and have equal weight in the v4 total:

`total = sum(included component scores) / number of included components`

This is the specified average, not the existing penalty-subtraction
formula. The component fields below identify inputs and their
responsibilities; the numeric mappings are implemented in RM-11.

| Component | Candidate fields and responsibility | Exclusion rule |
|---|---|---|
| **Core/Market** | `CoreResult.liquidity_usd`, `volume_24h`, and `market_cap` describe current market liquidity, activity level, and scale. `dilution_ratio` describes the `market_cap`/`fdv` relationship; do not score `fdv` and `dilution_ratio` as independent signals without demonstrating they do not duplicate valuation evidence. `primary_pair` identifies the selected market record; it is not itself a score. Keep price-change windows and buy pressure out of this component; those are Momentum. Keep `pool_age_days` out of the opportunity score so young tokens are not automatically treated as weak market opportunities. | Exclude if `CoreResult.degraded` or `primary_pair is None`. |
| **Security** | Use `SecurityResult.trust_score`, the existing `vulnerability_penalty(security)` priority ladder, and Holder `hci_pct` as safety signals. The component takes the weakest normalized signal rather than adding penalties, so vulnerability evidence represented in trust is not stacked again. Active authorities, tax, LP lock, ownership, scam flags, and provider notes remain explanatory except as already encoded by the named vulnerability ladder. Holder concentration is a safety assessment, never a positive opportunity component. | Exclude the combined Security component if either `SecurityResult.degraded` or `HolderResult.degraded`; do not treat unavailable holder concentration as zero risk. |
| **Momentum** | Use time/flow fields: `MomentumResult.price_momentum` and `buy_momentum`; where real data becomes available, `volume_growth_pct`, `liquidity_growth_pct`, and `whale_momentum`; and `HolderResult.holder_growth_24h_pct` as a change/flow signal. Keep static market levels assigned to Core/Market. Do not use `MomentumResult.trending_score` as a v4 input because it combines price, holder growth, and `social_momentum`, creating overlap with the other components. Do not separately add `SocialResult.sentiment_ratio` through `social_momentum`. | Exclude if required Core or Holder input is degraded, or the Momentum result is degraded. Terms that are currently permanent zeroes in the implementation (`volume_growth_pct`, `liquidity_growth_pct`, `whale_momentum`) must not be presented as measured evidence; keep them out until a real source exists. |
| **Social** | `SocialResult.x_score` is already a composite of account verification, age, follower count, and follower/following ratio; do not add those same account-level features again. `verified_follower_ratio` is a sampled-audience measure. `tweet_frequency_per_day`, `influencer_mention_count`, and `sentiment_ratio` are all derived from mentions and must not be blindly treated as independent evidence. Do not use `follower_growth_pct` as measured while it is a documented permanent zero. `reason_code` provides a machine-readable exclusion reason. | Exclude if `SocialResult.degraded` or `reason_code` is non-null, including insufficient-data and partial-secondary-failure statuses; do not replace unavailable social evidence with a neutral component score. |

`holder_count`, `holder_count_is_estimate`, `classified_wallets`,
`ambiguous_chain_candidates`, and provider/free-text notes remain
explanatory or coverage metadata unless a later, separately approved
contract assigns them a scoring role. They are not positive components
under this contract.

## Implemented numeric normalization

These are deterministic, inspectable starting rules, not empirically
calibrated thresholds. The $10K–$10M market anchors reuse the volume
range in RM-9's synthetic matrix; that matrix is explicitly not a
population sample. No new tier boundaries are set here.

| Component | Sub-score mapping | Component score |
|---|---|---|
| **Core/Market** | For `liquidity_usd` and `volume_24h`, map values at/below $10K to 0 and values at/above $10M to 100; use linear interpolation in `log10(value)` between those anchors. Map `dilution_ratio` (`market_cap / fdv`) to `clamp(0, 100, ratio * 100)`. | Arithmetic mean of the three sub-scores. Exclude if the result is degraded, no primary pair exists, or `dilution_ratio` is unavailable. |
| **Security** | Take `min(clamp(trust_score), 100 - clamp(hci_pct), 100 - vulnerability_penalty(security))`; every term is 0–100 with higher = safer. This replaces the blended `100 - score_risk_v2(...)` mapping. | The minimum safety sub-score; degraded Security or Holder data excludes this component. |
| **Momentum** | Map `price_momentum` and `buy_momentum` from their signed range [-100, 100] to [0, 100] using `(clamp(-100, 100, value) + 100) / 2`. Use `holder_growth_24h_pct` directly on its existing 0–100 scale. | Arithmetic mean of those three sub-scores. Exclude until Core/Holder inputs and the Momentum result are available and non-degraded. Leave documented constant-zero growth/whale terms out. |
| **Social** | Use `x_score` directly (0–100); map `verified_follower_ratio` (0–1) to 0–100; map `sentiment_ratio` (-1–1) with `(ratio + 1) * 50`. | Arithmetic mean of those three sub-scores. Keep tweet-frequency and influencer-count values explanatory only until a reference scale is agreed; do not let unbounded counts dominate by raw magnitude. Exclude when the approved Social availability rule applies. |

Within each component, the listed sub-scores have equal weight. The four
component scores then have equal weight in the v4 total. These mappings
preserve the positive direction and avoid counting derived or overlapping
fields twice. The current DexScreener parser maps omitted numeric market
fields to `0.0`, so v4 cannot distinguish an omitted individual market
field from a measured zero with today's `CoreResult` contract; RM-11
must preserve that documented limitation rather than claim field-level
presence detection.

Until RM-12 tier calibration is complete, a numeric v4 total is labeled
**Uncalibrated v4** rather than assigned a v1/v2/v3 tier. The owner
directed that the Risk/Opportunity Matrix be omitted for v4 only; the
existing versions keep their tiers and matrix behavior. The
zero-component result uses `total_score=None` and the status
**Insufficient Data**.

### Input overlap and data limits

- Core/Market owns **levels** (liquidity, volume, valuation); Momentum
  owns **changes and flow** (price acceleration, buy pressure, and
  holder growth). Although some inputs share a provider or result
  object, no same field should contribute to both components.
- Social sentiment is represented once in Social. The existing
  `MomentumResult.social_momentum` is excluded from the v4 Momentum
  calculation.
- The current `MomentumResult.trending_score` includes a social term and
  a holder-growth term. It must not be copied wholesale into v4.
- `SecurityResult.trust_score` summarizes provider security data that can
  overlap with its structured flags; those flags are not additional
  penalties by default. `SocialResult.x_score` is itself composite, and
  mention-derived social fields also overlap. RM-11 must document any
  chosen sub-signal weights and why their evidence is not double-counted.
- The RM-9 diagnostic confirmed that several current Momentum fields
  have no real backing data and remain constant zeroes. They are omitted
  until the project can measure them; zero is not proof of a measured
  neutral signal.

## Missing data, coverage, and confidence

1. A component enters the average only when its required inputs are
   available and pass the component's engine-status rule above.
2. Excluded components contribute neither a score nor a denominator
   entry. Record each as `(component_name, reason_code)` in the coverage
   manifest; use `SocialReasonCode` for Social causes and stable
   component-specific codes for Core/Market, Security, and Momentum.
   Never parse human-readable prose to infer the reason.
3. With the four equal-weight components, coverage is
   `included_component_count / 4 * 100`, in the same 0–100, higher-is-
   more-complete direction. Report coverage separately; do not multiply
   or subtract it from the total.
4. Proposed v4 `score_confidence` is the same coverage percentage,
   presented as **Coverage** rather than as certainty that the token is
   safe. Do not reuse the existing age-based confidence term in the v4
   aggregate; RM-9 showed that it gives very young pools little
   confidence independently of their measured market/security evidence.
5. If no component is available, return an explicit **Insufficient
   Data** result with `total_score=None` and no normal tier label. Do not
   manufacture a zero score or divide by zero.
6. The owner directed that the Risk/Opportunity Matrix be omitted for
   v4 only because v4 has no separate axes. Existing versions keep their
   current matrix behavior. This decision does not define or recalibrate
   v4 tiers.

## Component-boundary options and trade-offs

| Option | Component mapping | Signal not separately represented | Assessment |
|---|---|---|---|
| **1 — Minimal remap** | Discovery (Core + Momentum), Security (including Holder safety), Social | Core/Market and Momentum are blended, reducing visibility into market quality versus short-term changes. | Lowest component count, but conflicts with the owner's explicit request to keep Core/Market and Momentum distinct. |
| **2 — Full remap** | Core, Security, Holder, Momentum, Social | No engine is omitted, but Holder becomes an independent positive/averaged axis despite being a safety signal; Core and Momentum risk counting the same market activity twice. | Most granular, but conflicts with the owner's explicit Holder/Security boundary and overlap constraint. |
| **3 — Hybrid (owner-selected direction)** | Core/Market, Security (with Holder safety inputs), Momentum, Social | Holder concentration is not an independent opportunity axis; constant-zero Momentum fields and permanent-zero Social follower growth are not treated as measured signals. | Recommended and aligned with the owner's stated architecture. |

## Owner decisions recorded

- [x] **100 = strongest/safer; 0 = weakest/riskier** for all v4
  components and the total.
- [x] Exclude Social when its `reason_code` is non-null and exclude
  Security when Holder data is degraded; do not substitute a neutral
  score for unavailable components.
- [x] Report coverage separately as v4 `score_confidence`; return
  **Insufficient Data** without a numeric score or normal tier when no
  component is available.
- [x] Confirm the numeric sub-score mappings in **Proposed numeric
  normalization**, including the fixed market anchors and count-field
  exclusions.
- [x] Confirm the revised conservative Security sub-score: the minimum
  of trust, inverse HCI, and inverse vulnerability penalty.

The Hybrid boundary and revised Security mapping follow the owner's
explicit direction. This approval authorizes opt-in RM-11 behavior only;
it does not authorize v4 production dispatch, tier changes, or changes
to v1/v2/v3.
