#!/usr/bin/env python
"""
sentiment_stats.py

Calibration and evaluation for the sentiment indicator. This is the part that
decides whether the tab is worth looking at, so it is built as a first-class
output rather than bolted on afterwards.

Three problems it exists to solve
---------------------------------

**1. Hit rates without base rates are meaningless.**
Three-month S&P returns are positive roughly 70% of the time. So a buy signal
with a 75% hit rate is close to worthless, while a sell signal followed by a
decline 45% of the time is genuinely informative, because it lifts the odds of
a fall from 30% to 45%. A raw hit rate presented without its base rate
systematically flatters the buy side. Everything here is reported as lift over
base rate, and the base rate travels alongside it.

**2. Overlapping observations inflate every significance test.**
Evaluating three-month forward returns on weekly data across 29 years gives
about 1,500 observations but only 116 non-overlapping ones. Treating them as
independent overstates t-statistics by roughly the square root of the overlap,
a factor of three or four. Every test here uses Newey-West standard errors with
the lag matched to the overlap, reports effective sample size, and is
cross-checked against a strictly non-overlapping subsample.

**3. The published bands are round numbers, not thresholds.**
Twenty, thirty, forty, fifty percent. Under no signal the firing count is not
binomial, because the inputs are heavily correlated and clusters fire together,
which fattens the tails by construction. `calibrate_bands` derives thresholds
from the reading's own distribution, `binomial_reference` quantifies how much
the correlation matters, and `walk_forward_bands` does it causally so the
thresholds are not fitted on data they are then used to judge.

British spelling throughout.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd


# ===========================================================================
# Forward returns and sample size
# ===========================================================================
def forward_returns(prices: pd.Series, horizon: int) -> pd.Series:
    """Simple forward return over `horizon` observations, aligned to the
    decision date. The value at t is the return realised from t to t+horizon,
    so pairing it with a signal at t involves no look-ahead."""
    return prices.shift(-horizon) / prices - 1.0


def effective_sample_size(n: int, horizon: int) -> float:
    """Number of genuinely independent observations.

    With h-period overlapping returns sampled every period, consecutive
    observations share h-1 periods of data. The independent-equivalent count is
    n/h. This single number is the most useful antidote to an impressive-looking
    t-statistic.
    """
    return float(n) / float(max(horizon, 1))


# ===========================================================================
# Newey-West
# ===========================================================================
def newey_west_ols(y: np.ndarray, X: np.ndarray, lags: int) -> Tuple[np.ndarray, np.ndarray]:
    """OLS with Newey-West (Bartlett) heteroskedasticity and autocorrelation
    consistent covariance. Returns (coefficients, standard errors)."""
    n, k = X.shape
    XtX = X.T @ X
    XtX_inv = np.linalg.pinv(XtX)
    beta = XtX_inv @ (X.T @ y)
    resid = y - X @ beta

    # S = Gamma_0 + sum_l w_l (Gamma_l + Gamma_l')
    S = (X * resid[:, None]).T @ (X * resid[:, None])
    for l in range(1, lags + 1):
        w = 1.0 - l / (lags + 1.0)          # Bartlett kernel
        A = (X[l:] * resid[l:, None]).T @ (X[:-l] * resid[:-l, None])
        S += w * (A + A.T)

    cov = XtX_inv @ S @ XtX_inv
    se = np.sqrt(np.maximum(np.diag(cov), 0.0))
    return beta, se


def mean_difference_test(values: pd.Series, mask: pd.Series,
                         horizon: int, lags: Optional[int] = None) -> Dict[str, float]:
    """Test whether the mean of `values` differs when `mask` is true.

    Implemented as a regression on a constant and a dummy, so the dummy's
    coefficient IS the difference in means and Newey-West applies directly.
    """
    df = pd.concat([values.rename("y"), mask.rename("d")], axis=1).dropna()
    if len(df) < 30 or df["d"].sum() < 5 or (~df["d"].astype(bool)).sum() < 5:
        return {"difference": np.nan, "t_stat": np.nan, "se": np.nan,
                "n": float(len(df)), "n_signal": float(df["d"].sum()),
                "n_effective": np.nan, "lags": np.nan}

    y = df["y"].to_numpy(dtype=float)
    d = df["d"].astype(float).to_numpy()
    X = np.column_stack([np.ones(len(d)), d])

    # Default lag is the overlap length. Anything shorter leaves serial
    # correlation in the residuals and reinflates the t-statistic.
    L = int(lags) if lags is not None else max(int(horizon) - 1, 1)
    beta, se = newey_west_ols(y, X, L)

    diff, se_diff = float(beta[1]), float(se[1])
    return {
        "difference": diff,
        "se": se_diff,
        "t_stat": diff / se_diff if se_diff > 0 else np.nan,
        "n": float(len(df)),
        "n_signal": float(d.sum()),
        "n_effective": effective_sample_size(len(df), horizon),
        "lags": float(L),
    }


def non_overlapping_check(values: pd.Series, mask: pd.Series, horizon: int) -> Dict[str, float]:
    """Repeat the comparison on a strictly non-overlapping subsample.

    Takes every horizon-th observation, averaged over all possible offsets so
    the answer does not depend on an arbitrary starting point. Crude, but it
    cannot be accused of overlap bias, and if it disagrees sharply with the
    Newey-West result that disagreement is the finding.
    """
    df = pd.concat([values.rename("y"), mask.rename("d")], axis=1).dropna()
    if len(df) < horizon * 4:
        return {"difference": np.nan, "n_blocks": 0.0, "offsets_used": 0.0}

    diffs, blocks = [], []
    for offset in range(horizon):
        sub = df.iloc[offset::horizon]
        sig = sub.loc[sub["d"].astype(bool), "y"]
        non = sub.loc[~sub["d"].astype(bool), "y"]
        if len(sig) >= 3 and len(non) >= 3:
            diffs.append(sig.mean() - non.mean())
            blocks.append(len(sub))

    if not diffs:
        return {"difference": np.nan, "n_blocks": 0.0, "offsets_used": 0.0}
    return {"difference": float(np.mean(diffs)),
            "dispersion_across_offsets": float(np.std(diffs)),
            "n_blocks": float(np.mean(blocks)),
            "offsets_used": float(len(diffs))}


# ===========================================================================
# Base rates and lift
# ===========================================================================
@dataclass
class LiftResult:
    """The headline evaluation for one signal against one horizon."""
    side: str
    horizon: int
    base_rate: float               # unconditional P(outcome)
    conditional_rate: float        # P(outcome | signal)
    lift: float                    # conditional - base, in percentage points
    lift_ratio: float              # conditional / base
    mean_return_all: float
    mean_return_signal: float
    return_difference: float
    t_stat: float
    n: int
    n_signal: int
    n_effective: float
    non_overlapping_difference: float
    lift_ci: Tuple[float, float] = (np.nan, np.nan)

    def summary(self) -> str:
        return (
            f"{self.side} @ {self.horizon}obs: base {self.base_rate:.1%} -> "
            f"conditional {self.conditional_rate:.1%} (lift {self.lift:+.1%}), "
            f"return diff {self.return_difference:+.2%} "
            f"t={self.t_stat:.2f}, n_eff={self.n_effective:.0f}"
        )


def evaluate_signal(prices: pd.Series, signal: pd.Series, side: str,
                    horizon: int, n_boot: int = 0,
                    mean_block: Optional[int] = None,
                    seed: int = 0) -> LiftResult:
    """Evaluate one boolean signal, reporting lift over base rate throughout.

    For a sell signal the outcome of interest is a negative forward return; for
    a buy signal, positive. That asymmetry is the whole point: the two sides
    face very different base rates and cannot be judged on the same scale.
    """
    fwd = forward_returns(prices, horizon)
    df = pd.concat([fwd.rename("fwd"), signal.rename("sig")], axis=1).dropna()
    if df.empty:
        raise ValueError("no overlapping observations between prices and signal")

    df["sig"] = df["sig"].astype(bool)
    outcome = df["fwd"] < 0 if side == "sell" else df["fwd"] > 0

    base = float(outcome.mean())
    sig_mask = df["sig"]
    cond = float(outcome[sig_mask].mean()) if sig_mask.sum() else np.nan

    test = mean_difference_test(df["fwd"], sig_mask, horizon)
    nonov = non_overlapping_check(df["fwd"], sig_mask, horizon)

    ci = (np.nan, np.nan)
    if n_boot > 0:
        ci = bootstrap_lift_ci(outcome, sig_mask, horizon,
                               n_boot=n_boot, mean_block=mean_block, seed=seed)

    return LiftResult(
        side=side, horizon=horizon,
        base_rate=base, conditional_rate=cond,
        lift=cond - base if not np.isnan(cond) else np.nan,
        lift_ratio=cond / base if base > 0 and not np.isnan(cond) else np.nan,
        mean_return_all=float(df["fwd"].mean()),
        mean_return_signal=float(df.loc[sig_mask, "fwd"].mean()) if sig_mask.sum() else np.nan,
        return_difference=test["difference"],
        t_stat=test["t_stat"],
        n=int(len(df)), n_signal=int(sig_mask.sum()),
        n_effective=test["n_effective"],
        non_overlapping_difference=nonov["difference"],
        lift_ci=ci,
    )


def evaluate_by_band(prices: pd.Series, reading: pd.Series, bands: pd.Series,
                     side: str, horizon: int) -> pd.DataFrame:
    """Lift table by band. This is the table the tab should show.

    A monotonic lift across bands is the property that matters. A high lift in
    the top band alone, with nothing in between, usually means a handful of
    episodes are doing all the work.
    """
    fwd = forward_returns(prices, horizon)
    df = pd.concat([fwd.rename("fwd"), reading.rename("reading"),
                    bands.rename("band")], axis=1).dropna()
    if df.empty:
        return pd.DataFrame()

    outcome = df["fwd"] < 0 if side == "sell" else df["fwd"] > 0
    base = float(outcome.mean())
    base_ret = float(df["fwd"].mean())

    rows = []
    for band, grp in df.groupby("band", sort=False):
        idx = grp.index
        hit = float(outcome.loc[idx].mean())
        rows.append({
            "band": band,
            "n": len(grp),
            "n_effective": effective_sample_size(len(grp), horizon),
            "share_of_time": len(grp) / len(df),
            "base_rate": base,
            "conditional_rate": hit,
            "lift": hit - base,
            "mean_return": float(grp["fwd"].mean()),
            "mean_return_all": base_ret,
            "return_difference": float(grp["fwd"].mean()) - base_ret,
        })
    out = pd.DataFrame(rows)
    # Warn where a band's conclusion rests on very few independent observations.
    out["thin"] = out["n_effective"] < 10
    return out


# ===========================================================================
# Bootstrap
# ===========================================================================
def stationary_bootstrap_indices(n: int, mean_block: int, rng: np.random.Generator) -> np.ndarray:
    """Politis-Romano stationary bootstrap.

    Geometric block lengths preserve autocorrelation without imposing a fixed
    block size, which matters here because the reading is highly persistent:
    naive iid resampling would destroy exactly the structure that makes the
    tails fat.
    """
    p = 1.0 / max(mean_block, 1)
    idx = np.empty(n, dtype=int)
    i = int(rng.integers(0, n))
    for t in range(n):
        idx[t] = i
        if rng.random() < p:
            i = int(rng.integers(0, n))
        else:
            i = (i + 1) % n
    return idx


def bootstrap_lift_ci(outcome: pd.Series, signal: pd.Series, horizon: int,
                      n_boot: int = 500, mean_block: Optional[int] = None,
                      seed: int = 0, alpha: float = 0.05) -> Tuple[float, float]:
    """Confidence interval on lift, resampling in blocks to respect overlap."""
    rng = np.random.default_rng(seed)
    o = outcome.to_numpy(dtype=float)
    s = signal.to_numpy(dtype=bool)
    n = len(o)
    block = mean_block if mean_block is not None else max(horizon, 2)

    lifts = []
    for _ in range(n_boot):
        idx = stationary_bootstrap_indices(n, block, rng)
        ob, sb = o[idx], s[idx]
        if sb.sum() < 5 or (~sb).sum() < 5:
            continue
        lifts.append(ob[sb].mean() - ob.mean())
    if len(lifts) < 20:
        return (np.nan, np.nan)
    return (float(np.quantile(lifts, alpha / 2)), float(np.quantile(lifts, 1 - alpha / 2)))


# ===========================================================================
# Band calibration
# ===========================================================================
DEFAULT_BAND_QUANTILES: Dict[str, float] = {
    "Mild": 0.70, "Moderate": 0.85, "Strong": 0.95, "Extreme": 0.99,
}


def calibrate_bands(reading: pd.Series,
                    quantiles: Dict[str, float] = None) -> Dict[str, float]:
    """Derive band thresholds from the reading's own distribution.

    Replaces the published round numbers with thresholds that mean something:
    "Extreme" becomes the 99th percentile of readings actually observed, given
    however correlated the inputs turn out to be.
    """
    q = quantiles or DEFAULT_BAND_QUANTILES
    clean = reading.dropna()
    if clean.empty:
        return {k: np.nan for k in q}
    return {name: float(clean.quantile(p)) for name, p in q.items()}


def calibrate_bands_ci(reading: pd.Series, quantiles: Dict[str, float] = None,
                       n_boot: int = 500, mean_block: int = 21,
                       seed: int = 0, alpha: float = 0.05) -> pd.DataFrame:
    """Bootstrap confidence intervals on the band thresholds.

    Tells you whether two candidate thresholds are actually distinguishable.
    Overlapping intervals between "Strong" and "Extreme" mean the distinction
    is not supported by the sample, regardless of how it reads on a chart.
    """
    q = quantiles or DEFAULT_BAND_QUANTILES
    rng = np.random.default_rng(seed)
    clean = reading.dropna()
    vals = clean.to_numpy(dtype=float)
    n = len(vals)
    if n < 100:
        return pd.DataFrame()

    draws: Dict[str, List[float]] = {k: [] for k in q}
    for _ in range(n_boot):
        idx = stationary_bootstrap_indices(n, mean_block, rng)
        sample = vals[idx]
        for name, p in q.items():
            draws[name].append(float(np.quantile(sample, p)))

    rows = []
    for name, p in q.items():
        arr = np.array(draws[name])
        rows.append({
            "band": name, "quantile": p,
            "threshold": float(np.quantile(vals, p)),
            "lo": float(np.quantile(arr, alpha / 2)),
            "hi": float(np.quantile(arr, 1 - alpha / 2)),
            "width": float(np.quantile(arr, 1 - alpha / 2) - np.quantile(arr, alpha / 2)),
        })
    return pd.DataFrame(rows)


def walk_forward_bands(reading: pd.Series, quantiles: Dict[str, float] = None,
                       min_periods: int = 756) -> pd.DataFrame:
    """Band thresholds computed causally, using only data available at the time.

    Without this the thresholds are fitted on the same sample they are then
    used to judge, which is the exact criticism levelled at the published
    rules. Default burn-in is three years of daily data.
    """
    q = quantiles or DEFAULT_BAND_QUANTILES
    out = {}
    for name, p in q.items():
        out[name] = reading.expanding(min_periods=min_periods).quantile(p)
    return pd.DataFrame(out, index=reading.index)


def binomial_reference(k: int, fire_prob: float, quantiles: Sequence[float] = (0.7, 0.85, 0.95, 0.99),
                       n_sim: int = 20000, seed: int = 0) -> Dict[float, float]:
    """What the firing share would look like if the k inputs were independent.

    Comparing this against the observed distribution quantifies redundancy. If
    the observed 99th percentile sits far above the independent one, the inputs
    are moving together and the effective number of independent signals is well
    below k. That gap is the argument for cluster weighting, expressed as a
    number rather than an assertion.
    """
    rng = np.random.default_rng(seed)
    counts = rng.binomial(k, fire_prob, size=n_sim) / float(k)
    return {q: float(np.quantile(counts, q)) for q in quantiles}


def redundancy_ratio(reading: pd.Series, k: int, fire_prob: float,
                     quantile: float = 0.95, seed: int = 0) -> Dict[str, float]:
    """Effective number of independent inputs implied by the observed tails.

    Solves for the k_eff whose independent binomial tail matches the observed
    tail. A k_eff of 6 against a nominal 20 says the indicator has about six
    independent opinions, not twenty.
    """
    observed = float(reading.dropna().quantile(quantile))
    best_k, best_gap = k, np.inf
    for k_try in range(2, k + 1):
        ref = binomial_reference(k_try, fire_prob, (quantile,), n_sim=8000, seed=seed)[quantile]
        gap = abs(ref - observed)
        if gap < best_gap:
            best_gap, best_k = gap, k_try
    return {"observed_quantile": observed, "nominal_k": float(k),
            "effective_k": float(best_k), "ratio": best_k / float(k)}


# ===========================================================================
# Full report
# ===========================================================================
def full_evaluation(prices: pd.Series, reading: pd.Series, signal: pd.Series,
                    side: str, horizons: Sequence[int] = (21, 63, 126),
                    n_boot: int = 300) -> Dict[str, object]:
    """Everything, for one side of the indicator.

    Multiple horizons are deliberate. A signal that only works at one specific
    horizon and nowhere near it is usually an artefact of the search that found
    it, not a property of the market.
    """
    results = {"side": side, "by_horizon": {}, "bands": {}}

    for h in horizons:
        try:
            results["by_horizon"][h] = evaluate_signal(
                prices, signal, side, h, n_boot=n_boot)
        except ValueError:
            continue

    thresholds = calibrate_bands(reading)
    results["bands"]["calibrated"] = thresholds
    results["bands"]["ci"] = calibrate_bands_ci(reading, n_boot=min(n_boot, 300))
    results["bands"]["published"] = {"Mild": 0.20, "Moderate": 0.30,
                                     "Strong": 0.40, "Extreme": 0.50}
    return results


__all__ = [
    "forward_returns", "effective_sample_size", "newey_west_ols",
    "mean_difference_test", "non_overlapping_check", "evaluate_signal",
    "evaluate_by_band", "LiftResult", "stationary_bootstrap_indices",
    "bootstrap_lift_ci", "calibrate_bands", "calibrate_bands_ci",
    "walk_forward_bands", "binomial_reference", "redundancy_ratio",
    "full_evaluation", "DEFAULT_BAND_QUANTILES",
]
