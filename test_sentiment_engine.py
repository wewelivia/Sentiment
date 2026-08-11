#!/usr/bin/env python
"""
test_sentiment_engine.py

Tests for the sentiment engine and its statistics. Runs on the Mac with no
Terminal and no network.

The tests worth reading are the ones with a known right answer rather than a
regression baseline:

  - causality: changing the future must not change the past
  - hinge geometry: zero at the threshold, one at the extreme
  - planted signal: a signal we constructed to predict must be detected
  - null signal: a signal we constructed not to predict must not be
  - overlap inflation: the naive t-statistic must be visibly larger than the
    Newey-West one on overlapping data, which is the whole argument for using
    Newey-West in the first place
  - redundancy: perfectly correlated inputs must report an effective count far
    below their nominal count

Usage
-----
    python test_sentiment_engine.py
    python -m pytest test_sentiment_engine.py -v
"""

from __future__ import annotations

import sys

import numpy as np
import pandas as pd

import sentiment_engine as E
import sentiment_stats as S

RNG = np.random.default_rng(42)
PASSED: list = []
FAILED: list = []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASSED if condition else FAILED).append(name)
    print(f"  {'PASS' if condition else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def daily_index(n: int, start: str = "2004-01-01") -> pd.DatetimeIndex:
    return pd.bdate_range(start, periods=n)


def random_walk(n: int, seed: int = 0, drift: float = 0.0003, vol: float = 0.01) -> pd.Series:
    rng = np.random.default_rng(seed)
    r = rng.normal(drift, vol, n)
    return pd.Series(100 * np.exp(np.cumsum(r)), index=daily_index(n))


# ===========================================================================
def test_causality() -> None:
    print("\n[causality]")
    n = 1000
    s = random_walk(n, seed=1)
    ranks_full = E.causal_percentile_rank(s, "expanding", min_periods=100)

    # Replace the last 200 observations with something wildly different.
    s2 = s.copy()
    s2.iloc[-200:] = s2.iloc[-200:] * 10
    ranks_mod = E.causal_percentile_rank(s2, "expanding", min_periods=100)

    before = ranks_full.iloc[:-200]
    after = ranks_mod.iloc[:-200]
    check("percentile ranks before t are unaffected by data after t",
          np.allclose(before.dropna(), after.dropna()),
          f"max diff {np.nanmax(np.abs(before - after)):.2e}")

    # And the future must in fact change the future, or the test proves nothing.
    check("the modified tail does change its own ranks",
          not np.allclose(ranks_full.iloc[-200:].dropna(),
                          ranks_mod.iloc[-200:].dropna()))

    # A monotonically rising series sits at the top of its own history always.
    rising = pd.Series(np.arange(500, dtype=float), index=daily_index(500))
    r = E.causal_percentile_rank(rising, "expanding", min_periods=10)
    check("a strictly rising series always ranks at 1.0",
          np.allclose(r.dropna(), 1.0), f"min {r.dropna().min():.4f}")

    falling = pd.Series(np.arange(500, 0, -1, dtype=float), index=daily_index(500))
    rf = E.causal_percentile_rank(falling, "expanding", min_periods=10)
    check("a strictly falling series ranks at its window minimum",
          rf.dropna().max() < 0.11, f"max {rf.dropna().max():.4f}")


def test_hinge_geometry() -> None:
    print("\n[hinge]")
    ranks = pd.Series([0.0, 0.5, 0.90, 0.95, 1.0])

    gt = E.TriggerRule("gt", 90, "expanding")
    h = E.hinge(ranks, gt)
    check("hinge is zero below and at the gt threshold",
          h.iloc[0] == 0 and h.iloc[1] == 0 and abs(h.iloc[2]) < 1e-12,
          f"at 0.90 -> {h.iloc[2]:.6f}")
    check("hinge is one at the maximum", abs(h.iloc[4] - 1.0) < 1e-12)
    check("hinge is halfway at the midpoint", abs(h.iloc[3] - 0.5) < 1e-12,
          f"at 0.95 -> {h.iloc[3]:.4f}")
    check("hinge is monotone non-decreasing in rank", bool((h.diff().dropna() >= 0).all()))

    lt = E.TriggerRule("lt", 10, "expanding")
    hl = E.hinge(pd.Series([0.0, 0.05, 0.10, 0.5, 1.0]), lt)
    check("lt hinge is one at zero rank and zero at/above threshold",
          abs(hl.iloc[0] - 1.0) < 1e-12 and abs(hl.iloc[2]) < 1e-12 and hl.iloc[3] == 0)
    check("hinge stays within [0, 1]", bool(((h >= 0) & (h <= 1)).all()
                                            and ((hl >= 0) & (hl <= 1)).all()))

    f = E.fired(ranks, gt)
    check("binary firing agrees with a positive hinge above the threshold",
          bool(f.iloc[3]) and bool(f.iloc[4]) and not bool(f.iloc[1]))


def test_transforms() -> None:
    print("\n[transforms]")
    n = 800
    idx = daily_index(n)

    # Robust scaling must not be dragged around by a single outlier.
    base = pd.Series(RNG.normal(0, 1, n), index=idx)
    spiked = base.copy()
    spiked.iloc[400] = 60.0
    z_clean = E.robust_z(base).iloc[500:]
    z_spiked = E.robust_z(spiked).iloc[500:]
    drift = float((z_clean - z_spiked).abs().mean())
    check("a 60-sigma outlier barely moves later robust z-scores",
          drift < 0.15, f"mean shift {drift:.4f}")

    conventional = ((spiked - spiked.expanding(30).mean()) /
                    spiked.expanding(30).std()).iloc[500:]
    conv_drift = float((((base - base.expanding(30).mean()) /
                         base.expanding(30).std()).iloc[500:] - conventional).abs().mean())
    check("robust scaling is less disturbed than mean/sd scaling",
          drift < conv_drift, f"robust {drift:.4f} vs conventional {conv_drift:.4f}")

    check("robust z is winsorised at 3", float(E.robust_z(spiked).abs().max()) <= 3.0 + 1e-9)

    # Beta of a series against itself is one.
    p = random_walk(n, seed=3)
    b = E.rolling_beta(p, p, 60).dropna()
    check("beta of a series on itself is 1", np.allclose(b, 1.0, atol=1e-8),
          f"mean {b.mean():.6f}")

    # Beta of a 2x-levered version is two.
    lev = pd.Series(100 * np.exp(np.cumsum(np.log(p / p.shift(1)).fillna(0) * 2)), index=p.index)
    b2 = E.rolling_beta(lev, p, 60).dropna()
    check("beta of a 2x series is approximately 2", abs(b2.mean() - 2.0) < 0.05,
          f"mean {b2.mean():.4f}")

    # RSI bounds and the all-gain edge case that divides by zero.
    r = E.rsi(p, 14).dropna()
    check("RSI stays within [0, 100]", bool(((r >= 0) & (r <= 100)).all()))
    monotone = pd.Series(np.arange(1, 100, dtype=float), index=daily_index(99))
    check("RSI of a monotonically rising series is 100 (no divide-by-zero)",
          np.allclose(E.rsi(monotone, 14).dropna(), 100.0))

    # diff_horizon must respect the requested lag.
    lin = pd.Series(np.arange(300, dtype=float), index=daily_index(300))
    d3 = E.diff_horizon(lin, 3, "daily").dropna()
    check("3-month difference on a unit-slope series equals 63",
          np.allclose(d3, 63.0), f"got {d3.iloc[0]}")

    # Sign inversion in mean_of_z must actually cancel.
    a = pd.Series(RNG.normal(0, 1, n), index=idx)
    comp = E.mean_of_z({"x": a, "y": a}, invert=("y",)).dropna()
    check("inverting one of two identical legs cancels to zero",
          float(comp.abs().max()) < 1e-9)
    comp2 = E.mean_of_z({"x": a, "y": a}).dropna()
    check("without inversion the same legs reinforce", float(comp2.abs().max()) > 0.5)


def _make_inputs(n: int = 1500, k: int = 8, correlated: bool = False) -> list:
    idx = daily_index(n)
    inputs = []
    common = pd.Series(RNG.normal(0, 1, n), index=idx).rolling(20).mean()
    for i in range(k):
        if correlated:
            s = common + pd.Series(RNG.normal(0, 0.05, n), index=idx)
        else:
            s = pd.Series(RNG.normal(0, 1, n), index=idx).rolling(20).mean()
        inputs.append(E.SentimentInput(
            id=f"in{i}", series=s, cluster=f"c{i % 3}",
            sell=E.TriggerRule("gt", 90, "expanding"),
            buy=E.TriggerRule("lt", 10, "expanding"),
            min_periods=250,
        ))
    return inputs


def test_aggregation() -> None:
    print("\n[aggregation]")
    inputs = _make_inputs(1500, 8)
    eng = E.SentimentEngine(inputs)

    res = eng.compute("sell", "replica")
    check("replica reading stays within [0, 1]",
          bool(((res.reading.dropna() >= 0) & (res.reading.dropna() <= 1)).all()))
    check("denominator equals the number of available inputs",
          int(res.denominator.iloc[-1]) == 8, f"got {int(res.denominator.iloc[-1])}")
    check("published denominator counts inputs carrying the rule",
          eng.published_denominator("sell") == 8)

    imp = eng.compute("sell", "improved")
    check("improved reading stays within [0, 1]",
          bool(((imp.reading.dropna() >= 0) & (imp.reading.dropna() <= 1)).all()))

    # A 90th percentile rule should fire about 10% of the time per input.
    rate = float(res.fired_count.dropna().mean() / 8)
    check("a 90th-percentile rule fires about 10% of the time",
          0.05 < rate < 0.16, f"observed {rate:.3f}")

    # Dropping an input must move the denominator, not the reading's meaning.
    reduced = list(inputs)
    reduced[0] = E.SentimentInput(
        id="in0", series=inputs[0].series * np.nan, cluster="c0",
        sell=E.TriggerRule("gt", 90, "expanding"), min_periods=250)
    res2 = E.SentimentEngine(reduced).compute("sell", "replica")
    check("an unavailable input leaves the denominator rather than counting as not firing",
          int(res2.denominator.iloc[-1]) == 7, f"got {int(res2.denominator.iloc[-1])}")
    check("dropped inputs are reported by name",
          "in0" in res2.dropped.iloc[-1], f"{res2.dropped.iloc[-1]}")

    # Duplicate ids are a config error and must be refused loudly.
    try:
        E.SentimentEngine([inputs[0], inputs[0]])
        check("duplicate input ids are rejected", False)
    except ValueError:
        check("duplicate input ids are rejected", True)

    # Cluster weighting must damp a redundant cluster.
    idx = daily_index(1500)
    shared = pd.Series(RNG.normal(0, 1, 1500), index=idx).rolling(20).mean()
    lopsided = [E.SentimentInput(id=f"d{i}", series=shared.copy(), cluster="crowd",
                                 sell=E.TriggerRule("gt", 90, "expanding"), min_periods=250)
                for i in range(6)]
    lopsided.append(E.SentimentInput(
        id="lonely", series=pd.Series(RNG.normal(0, 1, 1500), index=idx).rolling(20).mean(),
        cluster="solo", sell=E.TriggerRule("gt", 90, "expanding"), min_periods=250))
    eng2 = E.SentimentEngine(lopsided)
    rep = eng2.compute("sell", "replica").reading.dropna()
    cw = eng2.compute("sell", "improved").reading.dropna()
    check("cluster weighting gives the lone input more influence than a 1-in-7 vote",
          cw.corr(rep) < 0.995, f"corr {cw.corr(rep):.4f}")


def test_newey_west() -> None:
    print("\n[newey-west]")
    n = 2000
    x = RNG.normal(0, 1, n)
    y = 0.5 * x + RNG.normal(0, 1, n)
    X = np.column_stack([np.ones(n), x])

    b0, se0 = S.newey_west_ols(y, X, lags=0)
    check("with zero lags the slope matches OLS", abs(b0[1] - 0.5) < 0.1,
          f"beta {b0[1]:.4f}")

    ols_se = np.sqrt(np.sum((y - X @ b0) ** 2) / (n - 2) / np.sum((x - x.mean()) ** 2))
    check("zero-lag Newey-West standard error is close to the OLS one",
          abs(se0[1] - ols_se) / ols_se < 0.10, f"NW {se0[1]:.5f} vs OLS {ols_se:.5f}")

    # The headline demonstration: overlapping returns inflate the naive t-stat.
    #
    # The mechanism matters and an earlier version of this test got it wrong.
    # Overlap alone is not enough: with an iid regressor the autocovariance
    # terms E[x_t x_{t-l}] vanish in expectation and Newey-West barely moves
    # the answer. The inflation comes from the SIGNAL being persistent as well
    # as the returns overlapping. Real sentiment readings fire in multi-week
    # runs, so the real case is the persistent one. Both are tested below,
    # because the contrast is the point.
    horizon = 63
    prices = random_walk(3000, seed=7)
    fwd = S.forward_returns(prices, horizon).dropna()

    iid_sig = pd.Series(RNG.random(len(fwd)) < 0.2, index=fwd.index)
    naive_iid = S.mean_difference_test(fwd, iid_sig, horizon, lags=0)
    nw_iid = S.mean_difference_test(fwd, iid_sig, horizon)
    check("with an iid signal, overlap alone barely changes the t-statistic",
          abs(abs(nw_iid["t_stat"]) - abs(naive_iid["t_stat"])) < 0.5,
          f"naive {naive_iid['t_stat']:.2f} -> NW {nw_iid['t_stat']:.2f}")

    # A persistent signal, which is what a sentiment reading actually is.
    slow = pd.Series(RNG.normal(0, 1, len(fwd)), index=fwd.index).rolling(120).mean()
    persistent = (slow > slow.quantile(0.80)).fillna(False)
    runs = int((persistent.astype(int).diff() == 1).sum())
    check("the persistent test signal really does fire in runs",
          runs < persistent.sum() / 10,
          f"{int(persistent.sum())} firing days in {runs} runs")

    naive = S.mean_difference_test(fwd, persistent, horizon, lags=0)
    corrected = S.mean_difference_test(fwd, persistent, horizon)
    ratio = abs(naive["t_stat"]) / max(abs(corrected["t_stat"]), 1e-9)
    check("with a persistent signal, Newey-West materially shrinks the t-statistic",
          abs(corrected["t_stat"]) < abs(naive["t_stat"]) * 0.75,
          f"naive {naive['t_stat']:.2f} -> NW {corrected['t_stat']:.2f} "
          f"({ratio:.1f}x overstatement)")
    check("the correction uses a lag matched to the overlap",
          corrected["lags"] == horizon - 1, f"lags {corrected['lags']:.0f}")

    n_eff = S.effective_sample_size(len(fwd), horizon)
    check("effective sample size is the observation count divided by the horizon",
          abs(n_eff - len(fwd) / horizon) < 1e-9,
          f"{len(fwd)} obs -> {n_eff:.0f} independent")


def test_lift_and_base_rate() -> None:
    print("\n[lift over base rate]")
    n, horizon = 4000, 63
    prices = random_walk(n, seed=11, drift=0.0004)
    fwd = S.forward_returns(prices, horizon)

    base_up = float((fwd.dropna() > 0).mean())
    check("a drifting market has a positive base rate well above one half",
          base_up > 0.6, f"P(up) = {base_up:.1%}")

    # A signal that fires only before genuine falls must show positive lift.
    planted = pd.Series(False, index=prices.index)
    planted[fwd < fwd.quantile(0.2)] = True
    good = S.evaluate_signal(prices, planted, "sell", horizon, n_boot=0)
    check("a planted sell signal beats the base rate",
          good.lift > 0.15, f"base {good.base_rate:.1%} -> {good.conditional_rate:.1%} "
                            f"(lift {good.lift:+.1%})")

    # A random signal must not.
    rand = pd.Series(RNG.random(n) < 0.2, index=prices.index)
    null = S.evaluate_signal(prices, rand, "sell", horizon, n_boot=0)
    check("a random signal shows negligible lift", abs(null.lift) < 0.10,
          f"lift {null.lift:+.1%}, t {null.t_stat:.2f}")

    # The point of the whole exercise: a high hit rate can be uninformative.
    always = pd.Series(True, index=prices.index)
    buy_always = S.evaluate_signal(prices, always, "buy", horizon, n_boot=0)
    check("an always-on buy signal has a high hit rate but zero lift",
          buy_always.conditional_rate > 0.6 and abs(buy_always.lift) < 1e-9,
          f"hit rate {buy_always.conditional_rate:.1%}, lift {buy_always.lift:+.2%}")

    check("evaluation reports effective rather than raw sample size",
          null.n_effective < null.n / 10,
          f"n {null.n} -> n_eff {null.n_effective:.0f}")
    check("the non-overlapping cross-check is computed",
          not np.isnan(null.non_overlapping_difference))

    ci = S.bootstrap_lift_ci((fwd.dropna() < 0), rand.reindex(fwd.dropna().index),
                             horizon, n_boot=200, seed=3)
    check("the bootstrap interval for a null signal contains zero",
          ci[0] <= 0 <= ci[1], f"CI [{ci[0]:+.3f}, {ci[1]:+.3f}]")


def test_band_calibration() -> None:
    print("\n[band calibration]")
    inputs = _make_inputs(2500, 10)
    eng = E.SentimentEngine(inputs)
    reading = eng.compute("sell", "replica").reading

    bands = S.calibrate_bands(reading)
    check("calibrated thresholds increase with the quantile",
          bands["Mild"] <= bands["Moderate"] <= bands["Strong"] <= bands["Extreme"],
          ", ".join(f"{k} {v:.3f}" for k, v in bands.items()))

    published = {"Mild": 0.20, "Moderate": 0.30, "Strong": 0.40, "Extreme": 0.50}
    differs = any(abs(bands[k] - published[k]) > 0.05 for k in published)
    check("calibrated thresholds differ from the published round numbers", differs)

    # The replica reading is discrete: with k inputs it can only take k+1
    # values. With HSBC's 20 sell inputs that is 5-point steps, so the
    # published 20/30/40/50 bands sit exactly on grid points and any
    # distinction finer than 5 points is not meaningful. Bootstrap intervals
    # of zero width are the correct answer here, not a bug.
    distinct = sorted(reading.dropna().unique())
    step = min(np.diff(distinct)) if len(distinct) > 1 else np.nan
    check("the replica reading is discrete, on a 1/k grid",
          abs(step - 1.0 / 10) < 1e-9, f"{len(distinct)} distinct values, step {step:.3f}")

    ci = S.calibrate_bands_ci(reading, n_boot=150, seed=5)
    check("bootstrap intervals are produced for each band", len(ci) == 4)
    if len(ci) == 4:
        check("discrete-reading intervals have non-negative width",
              bool((ci["width"] >= 0).all()),
              ", ".join(f"{r.band} [{r.lo:.3f}, {r.hi:.3f}]" for r in ci.itertuples()))

    # The improved reading is continuous, so its thresholds are genuinely
    # uncertain and the intervals must have width.
    cont = eng.compute("sell", "improved").reading
    ci_c = S.calibrate_bands_ci(cont, n_boot=150, seed=5)
    check("continuous-reading intervals have positive width",
          len(ci_c) == 4 and bool((ci_c["width"] > 0).all()),
          ", ".join(f"{r.band} [{r.lo:.3f}, {r.hi:.3f}]" for r in ci_c.itertuples()))

    wf = S.walk_forward_bands(reading, min_periods=500)
    check("walk-forward thresholds are undefined during the burn-in",
          bool(wf["Extreme"].iloc[:499].isna().all()))
    check("walk-forward thresholds exist after the burn-in",
          bool(wf["Extreme"].iloc[-1] == wf["Extreme"].iloc[-1]))

    # Independent inputs should look roughly binomial; correlated ones must not.
    ref = S.binomial_reference(10, 0.10, (0.95, 0.99))
    check("the independent reference produces sensible tail quantiles",
          0.0 < ref[0.95] <= ref[0.99] <= 1.0,
          f"95th {ref[0.95]:.2f}, 99th {ref[0.99]:.2f}")

    corr_inputs = _make_inputs(2500, 10, correlated=True)
    corr_reading = E.SentimentEngine(corr_inputs).compute("sell", "replica").reading
    ind_q = ref[0.95]
    corr_q = float(corr_reading.dropna().quantile(0.95))
    check("correlated inputs have a fatter tail than the independent reference",
          corr_q > ind_q, f"correlated 95th {corr_q:.2f} vs independent {ind_q:.2f}")

    red = S.redundancy_ratio(corr_reading, k=10, fire_prob=0.10, quantile=0.95)
    check("redundancy analysis reports an effective count below the nominal one",
          red["effective_k"] < red["nominal_k"],
          f"nominal {red['nominal_k']:.0f} -> effective {red['effective_k']:.0f}")


def test_band_table() -> None:
    print("\n[band lift table]")
    n, horizon = 3000, 63
    prices = random_walk(n, seed=13, drift=0.0004)
    inputs = _make_inputs(n, 8)
    reading = E.SentimentEngine(inputs).compute("sell", "replica").reading
    bands = E.label_bands(reading)

    table = S.evaluate_by_band(prices, reading, bands, "sell", horizon)
    check("the band table is produced", not table.empty, f"{len(table)} bands")
    if not table.empty:
        check("every row carries the base rate alongside the conditional rate",
              "base_rate" in table.columns and "conditional_rate" in table.columns
              and "lift" in table.columns)
        check("shares of time sum to one", abs(table["share_of_time"].sum() - 1.0) < 1e-9)
        check("thin bands are flagged by effective sample size",
              "thin" in table.columns and table["n_effective"].min() < table["n"].min())
        base = table["base_rate"].iloc[0]
        check("the base rate is constant across bands",
              bool((table["base_rate"] - base).abs().max() < 1e-12),
              f"base {base:.1%}")


def main() -> int:
    print("=" * 72)
    print("sentiment engine tests")
    print("=" * 72)
    for fn in (test_causality, test_hinge_geometry, test_transforms,
               test_aggregation, test_newey_west, test_lift_and_base_rate,
               test_band_calibration, test_band_table):
        fn()

    print("\n" + "=" * 72)
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        for f in FAILED:
            print(f"  FAILED: {f}")
        return 1
    print("All tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
