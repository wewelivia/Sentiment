# Sentiment indicator tab: Bloomberg feasibility

Assessment for adding an HSBC-style sentiment indicator as a third tab alongside the House View and View Challenges dashboards, using the existing blpapi connection.

Prepared 11 August 2026.

---

## Bottom line

Yes, and it is a considerably easier data problem than the economic calendar work you have just finished. The reason is that the calendar needed Bloomberg to tell you about *events* (survey medians, scheduled publish dates, actual-versus-consensus), which is where `ACTUAL_RELEASE`, `ECO_FUTURE_RELEASE_DATE_LIST` and the period-end-versus-publish-date remapping all caused trouble. The sentiment indicator needs Bloomberg to tell you about *prices and levels over time*. That is `HistoricalDataRequest` on `PX_LAST`, the single most reliable thing the API does.

The honest qualification is that "the same Bloomberg connection" is doing some work in that sentence. The transport is identical. The *entitlements* are not. Roughly a third of HSBC's inputs sit behind datasets your contract may not include: short interest, fund NAV and AuM, the ICE MOVE index, and third-party survey feeds. Whether those resolve is a licensing question, not a coding one, and it cannot be answered from here. That is what `diagnose_sentiment.py` is for.

My prior, to be confirmed on the Terminal: around eleven inputs resolve cleanly, five resolve with documented proxies, and five are blocked or need a non-Bloomberg loader. That is enough to build something that tracks HSBC's published series closely on the sell side, where the vol and positioning block does most of the work.

---

## Why this is a different data pattern from the existing dashboards

Three things change, and each has an architectural consequence.

**Volume of history.** The calendar looks back 45 days. Percentile triggers need twenty years, because an expanding-window 90th percentile computed on three years of data is not a 90th percentile of anything meaningful. Sixty-three unique securities times roughly 5,700 daily observations is around 360,000 data points on the first pull. That is one heavy request run, not a per-refresh cost.

**Consequence:** the sentiment provider needs a local cache with incremental updates, not a live pull on every dashboard hit. Full history once, then a rolling top-up of the last thirty days on each refresh. Given the OneDrive and SQLite corruption problem you hit before, the cache belongs on a local unsynced path, and parquet is a better fit than SQLite here anyway since the access pattern is whole-series reads rather than row queries.

**Mixed frequencies.** Daily prices, weekly surveys and CFTC, monthly-ish fund data all land in the same aggregate. HSBC never says how they align them.

**Consequence:** you need an explicit alignment policy, and it should be forward-fill onto a daily business calendar with a staleness cutoff, so a survey that stops publishing drops out of the count rather than firing forever on its last value. The config sets those cutoffs at seven days for daily, twenty-one for weekly, sixty for monthly.

**Release lags.** CFTC comes out with a three-day lag, ICI with two, short interest bi-weekly. If you stamp each observation at its as-of date you have embedded a look-ahead into every back-test you run.

**Consequence:** the cache should store both the as-of date and the knowable-from date, and the engine should read from the latter. This is cheap to build now and painful to retrofit.

---

## Per-input reachability

My assessment before the diagnostic runs. The `Confidence` column is how likely I think it is that at least one candidate ticker resolves and is entitled on a standard Private Bank Terminal.

| # | Input | Route | Confidence |
|---|-------|-------|------------|
| 12 | VIX curve (VIX3M / VIX) | Direct, `VIX Index` and `VIX3M Index` | High |
| 11 | MASK, equity leg | `SKEW Index` | High |
| 8 | Vol-target equity allocation | Computed from SPX realised vol | High |
| 7 | Risk parity equity allocation | Computed, inverse-vol across four sleeves | High |
| 16 | Cross-asset RSI | Computed from index prices | High |
| 15 | Risk appetite, first PC | Computed, PCA on a risk-on/risk-off basket | High |
| 20 | Average survey sentiment, AAII leg | `AAIIBULL` / `AAIIBEAR Index` | High |
| 14 | MOVE | `MOVE Index`, ICE-licensed | Medium-high |
| 10 | CAPCA, equity leg | Cboe put-call ratio, ticker uncertain | Medium |
| 1, 4 | CTA equity and USD beta | SG CTA index as proxy for HSBC's index set | Medium |
| 19 | Equity sentiment composite | Computable proxy, weights undisclosed | Medium |
| 13 | VIX HY / VIX IG | Cboe ETF vol indices, several discontinued | Medium-low |
| 10, 11 | CAPCA and MASK, UST legs | No published index; derived proxy only | Low |
| 17, 18 | CFTC positioning | Symbology needs resolving via `CFTC <GO>` | Low until resolved |
| 21 | Money market 3m flow | ICI tickers uncertain; Fed H.6 as fallback | Low-medium |
| 20 | Investors Intelligence, NAAIM legs | Paid feed; NAAIM may not be carried | Low |
| 2, 3 | Fund betas | `FUND_NET_ASSET_VAL` entitlement; proxy basket | Low-medium |
| 5, 6 | ETF short interest | `SHORT_INT` is separately entitled | Low |
| 9 | Equity momentum indicator | HSBC proprietary; substitute only | Not replicable |

The three genuinely load-bearing uncertainties are CFTC symbology, short-interest entitlement, and the UST option legs of CAPCA and MASK. Everything else either resolves or has a defensible substitute.

---

## What I would flag as the real risks

**Not the data. The validation.** Your methodology note already makes the point and I think it is the most important thing in it: HSBC chose every transform, window and threshold by looking at the same history they then use to demonstrate the signal works. If you rebuild it faithfully and it back-tests well, that tells you almost nothing, because you will have inherited their in-sample selection wholesale. The tab is worth building, but the headline number should not be presented internally as a validated signal until it has been run walk-forward.

**Redundancy makes the count misleading.** VIX enters the VIX curve, the MASK block and the sentiment composite. AAII enters both the survey average and the composite. The three 20-day beta inputs measure nearly the same thing. An equal-weighted count of twenty inputs where eight are variations on "equity vol is low" is not a twenty-input indicator. The config carries a cluster map for exactly this reason, and the improved mode weights by cluster.

**Falling input counts change the reading silently.** If four inputs are blocked, the denominator drops from twenty to sixteen and every historical percentage in your series shifts. Whatever the diagnostic returns, the tab needs to display the live denominator and flag any input that dropped out on a given date, otherwise you will misread your own chart.

**The counter-case worth stating.** The strongest argument against building this at all is that contrarian sentiment aggregates have a poor live record precisely because they are the most heavily data-mined class of indicator in the industry, and HSBC revamped theirs because it misfired in January 2024. If the honest expected out-of-sample edge is close to zero, the tab's value is as a positioning-context panel for the House View narrative rather than as a signal. I think that framing is the right one and it should shape how the tab is presented, not whether it is built.

---

## Proposed architecture

Consistent with the provider isolation you already enforce. Nothing here touches `challenge_engine.py`, `house_view.yaml` or the existing HTML.

```
providers/
  sentiment_provider.py     # blpapi history pulls + parquet cache + release-lag stamping
sentiment_engine.py         # transforms, percentile triggers, both aggregation modes
sentiment_tickers.yaml      # the config map (delivered)
diagnose_sentiment.py       # ticker/field validation (delivered)
sentiment.html              # third standalone tab, same shell as view-challenges.html
cache/sentiment/*.parquet   # local unsynced path
```

Backend surface: one new endpoint, `/api/sentiment`, returning the current buy and sell readings under both modes, the band, the per-input firing table with each input's current percentile, the live denominator, and a dropped-input list. A second endpoint, `/api/sentiment/history`, serves the time series for the chart.

`sentiment_engine.py` should be pure and testable without a Terminal, same as your other core logic, with a fixture of synthetic series so the percentile and aggregation maths can be unit-tested on the Mac.

---

## Running the diagnostic

On the Windows Terminal machine, with the Terminal running and logged in:

```
python diagnose_sentiment.py
```

It is read-only, makes no changes to any dashboard file, and takes a few minutes. Ninety-eight candidate checks across sixty-three unique securities, one batched reference call plus one historical call per candidate, comfortably inside daily limits.

It writes three files. `sentiment_diagnostic.md` is the human-readable verdict table. `sentiment_diagnostic.json` is the machine-readable detail. `sentiment_tickers.resolved.yaml` contains only what passed, ready to promote into the main config.

Statuses to expect and what they mean:

- `OK` and `OK_SHORT_HISTORY` are usable. The second means under five years, so expanding-window percentiles will be unstable early.
- `FIELD_DENIED` is an entitlement question for your Bloomberg contract, not a bug. Expect it on `SHORT_INT` and possibly `FUND_NET_ASSET_VAL`.
- `INVALID_TICKER` means my candidate guess was wrong. Most likely on the CFTC, ICI and Investors Intelligence entries. Resolve on the Terminal and paste the correct symbol in as the first candidate.
- `STALE` means the series resolved but has stopped updating, which is the likely outcome for the discontinued Cboe ETF volatility indices.

Run it, send me `sentiment_diagnostic.md`, and I will build the provider and engine against the resolved set rather than against guesses.

---

## One validation result worth noting

The config reproduces HSBC's input counts exactly: twenty inputs carry a sell rule, thirteen carry a buy rule, and the only input with no sell rule is VIX HY / VIX IG. Those denominators were inferred by counting the itemised inputs on pages 11 to 21 rather than stated in the report, so matching them independently is a useful check that the rule table has been transcribed correctly. It also caught three inputs where I had initially inverted the buy and sell sides.

---

## Open decisions before the build

1. **Denominator policy.** When an input is unavailable, do you want the aggregate computed on the reduced denominator, or held at the full HSBC denominator with unavailable inputs counted as not firing? The first is more honest, the second is more comparable to HSBC's published chart. My preference is the first, with the denominator displayed.
2. **Whether the sentiment reading should feed the View Challenges scoring.** You already have a `sentiment` theme there. There is an argument for the sentiment indicator becoming an input to that theme's scoring rather than living purely in its own tab, but it changes the meaning of "challenges the view" from a data surprise to a positioning extreme.
3. **The `labour` theme decision** is still outstanding from the last session and is unrelated, but it is the other thing sitting in the House View backlog.

---

Source document: HSBC Global Research, "Sentiment shape up: Our revamped sentiment indicators", Multi-Asset Global, 5 March 2024 (Smith, Toms, Kettner, Mallisetty), as summarised in `HSBC_Sentiment_Indicator_Analysis.docx`.
