# Sentiment indicator: Bloomberg ticker diagnostic

Run: 11 Aug 2026 15:28  
**DRY RUN - no Terminal connection, statuses are placeholders.**

## Summary

- Inputs tested: **2**
- Buildable (every role resolved): **0**
- Partial (some roles resolved): **0**
- Blocked (nothing resolved): **0**

A buildable count materially below 21 is expected. The question that matters
is whether the buildable set spans enough distinct clusters for the aggregate
to be meaningful, not whether every HSBC input survives.

## Per-input verdicts

| # | Input | Kind | Verdict | Resolved tickers |
|---|-------|------|---------|------------------|
| 1 | VIX curve (VIX3M / VIX) | derived | **NOT_TESTED** | - |
| 2 | MOVE index | direct | **NOT_TESTED** | - |

## Candidate detail

| Input | Role | Ticker | Field | Status | Points | From | To | Stale (d) | Freq | Detail |
|-------|------|--------|-------|--------|--------|------|----|-----------|------|--------|
| vix_curve | numerator | `VIX3M Index` | `PX_LAST` | NOT_TESTED | 0 | - | - | - | - | Dry run |
| vix_curve | numerator | `VXV Index` | `PX_LAST` | NOT_TESTED | 0 | - | - | - | - | Dry run |
| vix_curve | denominator | `VIX Index` | `PX_LAST` | NOT_TESTED | 0 | - | - | - | - | Dry run |
| move | primary | `MOVE Index` | `PX_LAST` | NOT_TESTED | 0 | - | - | - | - | Dry run |

## What to do with blocked inputs

1. For CFTC series, open `CFTC <GO>` on the Terminal, find the exact security
   for the E-mini S&P non-commercial net position, and paste it into
   `sentiment_tickers.yaml` as the first candidate.
2. For short interest and fund NAV fields, a `FIELD_DENIED` status is an
   entitlement question for your Bloomberg contract, not a code problem.
3. For survey series that do not resolve, AAII and NAAIM publish free weekly
   data that can be loaded outside blpapi into the same cache.
4. Re-run this script after each config change. It is cheap and read-only.
