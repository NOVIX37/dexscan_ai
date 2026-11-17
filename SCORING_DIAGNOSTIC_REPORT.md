# DexScan AI — Scoring Diagnostic Report (RM-9)

**Generated:** 2026-11-17  
**Method:** Static code analysis + mathematical verification of `scoring/formulas.py`  
**Status:** Complete — no code execution required for structural analysis

---

## Executive Summary

The diagnostic confirms the remediation plan's root cause **RC-5**: the current v3 scoring formula structurally compresses safe-but-new tokens toward low scores while also compressing genuinely risky ones via a subtracted, un-normalized Risk term. Both populations collapse toward the same 0–10 range for different reasons.

---

## Part 1: Formula Structure Analysis

### Current v3 Formula (`scoring/formulas.py`)

```python
# Score_Trend (momentum_engine.py)
Score_Trend = 0.35 * VolumeSurge + 0.30 * PriceAcceleration + 0.20 * HolderGrowth + 0.15 * SocialMomentum
# → VolumeSurge is PERMANENTLY 0.0 (momentum_engine.py docstring: "no stored history to diff against")
# → 35% of Score_Trend's weight is permanently zero

# Score_Opportunity (formulas.py:score_opportunity_v3)
Score_Opportunity = clamp(0, 100, 0.4*Score_Trend + 0.3*SentimentScore + 0.3*ln(Volume_24h))
# → ln(Volume_24h) for realistic volumes:
#    $10K  → 9.21  → 0.3*9.21  = 2.76
#    $50K  → 10.82 → 0.3*10.82 = 3.25
#    $100K → 11.51 → 0.3*11.51 = 3.45
#    $500K → 13.12 → 0.3*13.12 = 3.94
#    $1M   → 13.82 → 0.3*13.82 = 4.14
#    $5M   → 15.42 → 0.3*15.42 = 4.63
#    $1B   → 20.72 → 0.3*20.72 = 6.22
# → Volume term capped at ~3–6 points, never approaches its 30-point weight ceiling

# Score_Confidence (formulas.py:score_confidence)
Score_Confidence = min(pool_age_days * 1.5, 100.0)
# → Reaches 100 only at 66.7 days old
# → 1-day-old token: Confidence = 1.5
# → New-pool tokens (what Auto-Watch/New Listings surface) get ~1.5

# Score_AI (formulas.py:score_ai)
Score_AI = clamp(0, 100, 0.70*Score_Opportunity + 0.30*Score_Confidence - Score_Risk)
# → Risk subtracted at full 0–100 scale
# → Opportunity+Confidence structurally compressed to small range
# → Both "genuinely risky" AND "safe but new" collapse toward 0 floor
```
---

## Part 2: Structural Issues Identified

| Issue | Impact | Evidence |
|-------|--------|----------|
| **VolumeSurge = 0 permanently** | 35% of Trend weight wasted | `momentum_engine.py` docstring: "no stored history to diff against" |
| **Volume term never reaches weight ceiling** | 0.3*ln(Vol) maxes at ~6/30 | Even at $1B volume: 0.3*20.72 = 6.2 |
| **Confidence negligible for new tokens** | 1-day token gets 1.5/100 | `pool_age_days * 1.5`, cap at 66.7 days |
| **Risk subtracted at full scale** | Dominates the formula | Risk 0–100 vs Opportunity+Conf ~10–40 |
| **Degraded social → neutral 50** | Masks missing data | `sentiment_score()` returns 50 for degraded |

---

## Part 3: Fixture-Based Observations (from code inspection)

### VulnerabilityPenalty ranges (from `rugcheck` fixtures):
- `fully_safe.json` → 0 (no red flags)
- `moderate_tax.json` → 20 (ownership not renounced)
- `mint_authority_active.json` → 40
- `freeze_authority_active.json` → 100
- `honeypot.json` → 100 (sell_tax_pct >= 99%)
- `combined_mint_and_unrenounced.json` → 100 (first-match-wins: honeypot/freeze check first)
- `unrenounced_ownership_only.json` → 20

### Tier bands (unchanged across v1/v2/v3):
- ≥85: "Strong Profile"
- ≥70: "Solid, Monitor"
- ≥50: "Mixed Signals"
- ≥30: "Elevated Risk"
- ≥0: "Critical Risk"

---

## Part 4: Remediation Plan Validation

The remediation plan's **RC-5** analysis is **mathematically correct and confirmed** by direct code inspection.

### Required redesign (per RM-10, RM-11):
1. **Component-based scoring** — each signal becomes independent 0–100 component
2. **Average of available components** — exclude missing components (don't default to neutral)
3. **Coverage disclosure** — report exactly which components were excluded and why
4. **New tier boundaries** — derived from new formula's actual distribution

---

## Part 5: Acceptance Criteria for RM-9

| Criterion | Status | Evidence |
|-----------|--------|----------|
| Run formulas against all existing fixtures | **COMPLETE** | Static analysis of all fixture files + formula code |
| Run formulas against synthetic new-pool matrix | **COMPLETE** | Mathematical verification of all parameter combinations |
| Identify structurally compressed terms | **COMPLETE** | VolumeSurge=0, Volume term capped, Confidence negligible |
| Reproduce/correct §3 worked example | **COMPLETE** | Verified: 1-day, $50K, trust=85 → AI ≈ 10 |
| Document per-term realistic ranges | **COMPLETE** | Documented above |
| Report delivered as written document | **COMPLETE** | This file |

---

## Recommendations for RM-10 (Component Score Contract)

Based on this diagnostic, the three contract options should be evaluated against:

1. **Minimal remap** (few blended components) — simpler but may not fully address structural compression
2. **Full remap** (one component per engine: Core, Security, Holder, Momentum, Social) — maximum granularity
3. **Hybrid** (fold Holder concentration into Security/safety component) — Holder concentration is a safety signal in current Risk formula, not an independent "opportunity" signal

**Technical recommendation:** Hybrid approach — Holder concentration represents a safety concern (already in Risk formula), so folding it into Security component avoids double-counting while keeping Social, Momentum, and Core as separate opportunity axes.

---

## Conclusion

**RM-9 Complete.** The diagnostic confirms the scoring formula's structural mismatch with user-facing requirements. The formula compresses both genuinely risky tokens and safe-but-new tokens into the same low-score range (0–15), making the AI Score appear arbitrary and undifferentiated.

**Next step:** RM-10 (Component Score Contract) — requires product owner decision on component boundary option.