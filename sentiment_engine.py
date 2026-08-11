#!/usr/bin/env python
"""
sentiment_engine.py

Core transforms, triggers and aggregation for the House View sentiment tab.

Pure and testable: no Bloomberg, no Macrobond, no network. Takes prepared
pandas Series in, produces signal frames out. Run the tests on the Mac without
a Terminal connection.

Design decisions worth knowing about
------------------------------------
1. Everything is causal. Percentile ranks at date t use only data up to and
   including t. There is no expanding().rank() shortcut that quietly peeks.

2. Firing is a hinge, not a binary. Binary encodes a real belief, that
   sentiment only matters in the tails, and a plain z-score average washes
   that out. But binary also throws away magnitude: an input at the 99.9th
   percentile counts the same as one at the 90.1st. The hinge is zero below
   the threshold and rises continuously to one at the extreme, keeping the
   non-linearity while recovering the magnitude. That is one parameter, not a
   family of them. `fired` is still exposed for the faithful HSBC replica.

3. Scaling is median/MAD, not mean/standard deviation. HSBC deletes 2007-09
   and 2020 from certain inputs to stop crises dominating the distribution.
   Deleting the episodes a sentiment indicator exists to handle is the wrong
   fix for a real problem. Robust scaling achieves the stated goal without
   discarding the data.

4. Denominators are explicit. If an input is unavailable on a given date it
   leaves the denominator rather than counting as "not firing", and the
   denominator is returned alongside the reading. Otherwise a data outage
   silently looks like falling sentiment.

British spelling throughout.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Union

import numpy as np
import pandas as pd

# Trading days per calendar month, used to convert HSBC's month-denominated
# horizons into observation counts. Overridden for weekly inputs.
TRADING_DAYS_PER_MONTH = 21
WEEKS_PER_MONTH = 4.348


# ===========================================================================
# Transforms
# ===========================================================================
def diff_horizon(s: pd.Series, months: float, freq: str = "daily") -> pd.Series:
    """Difference over a horizon expressed in months.

    HSBC specifies horizons in months but never says whether that means
    calendar months or a fixed number of observations. We use observation
    counts derived from the series frequency, which is stable when the
    calendar is ragged.
    """
    per_month = TRADING_DAYS_PER_MONTH if freq == "daily" else WEEKS_PER_MONTH
    lag = max(int(round(months * per_month)), 1)
    return s - s.shift(lag)


def smooth(s: pd.Series, months: float, freq: str = "daily") -> pd.Series:
    per_month = TRADING_DAYS_PER_MONTH if freq == "daily" else WEEKS_PER_MONTH
    window = max(int(round(months * per_month)), 1)
    return s.rolling(window, min_periods=max(window // 2, 1)).mean()


def robust_z(s: pd.Series, window: Optional[int] = None, winsorise: float = 3.0) -> pd.Series:
    """Median/MAD z-score, winsorised.

    1.4826 rescales MAD to be a consistent estimator of the standard deviation
    under normality, so the output is comparable in magnitude to a conventional
    z-score.
    """
    if window is None:
        med = s.expanding(min_periods=30).median()
        mad = (s - med).abs().expanding(min_periods=30).median()
    else:
        med = s.rolling(window, min_periods=max(window // 4, 30)).median()
        mad = (s - med).abs().rolling(window, min_periods=max(window // 4, 30)).median()

    scale = 1.4826 * mad
    # A constant series has zero MAD. Return zero rather than infinity.
    scale = scale.replace(0.0, np.nan)
    z = (s - med) / scale
    return z.clip(-winsorise, winsorise)


def rolling_beta(y: pd.Series, x: pd.Series, window: int) -> pd.Series:
    """Rolling OLS beta of y on x, computed on returns."""
    ry = y.pct_change()
    rx = x.pct_change()
    df = pd.concat([ry.rename("y"), rx.rename("x")], axis=1).dropna()
    cov = df["y"].rolling(window).cov(df["x"])
    var = df["x"].rolling(window).var()
    beta = cov / var.replace(0.0, np.nan)
    return beta.reindex(y.index)


def rsi(s: pd.Series, window: int = 14) -> pd.Series:
    delta = s.diff()
    gain = delta.clip(lower=0).rolling(window).mean()
    loss = (-delta.clip(upper=0)).rolling(window).mean()
    rs = gain / loss.replace(0.0, np.nan)
    out = 100 - 100 / (1 + rs)
    # All-gain windows give infinite RS, which is RSI 100 by definition.
    return out.where(loss != 0, 100.0)


def realised_vol(s: pd.Series, window: int = 60, annualise: int = 252) -> pd.Series:
    return s.pct_change().rolling(window).std() * np.sqrt(annualise)


def vol_target_weight(s: pd.Series, target_vol: float = 0.10,
                      window: int = 60, cap: float = 3.0) -> pd.Series:
    """Notional equity weight a vol-targeting fund would hold."""
    vol = realised_vol(s, window)
    return (target_vol / vol.replace(0.0, np.nan)).clip(upper=cap)


def inverse_vol_weights(sleeves: Dict[str, pd.Series], window: int = 60) -> pd.DataFrame:
    """Risk-parity style inverse-volatility weights across sleeves."""
    vols = pd.DataFrame({k: realised_vol(v, window) for k, v in sleeves.items()})
    inv = 1.0 / vols.replace(0.0, np.nan)
    return inv.div(inv.sum(axis=1), axis=0)


def mean_of_z(components: Dict[str, pd.Series], window: Optional[int] = None,
              invert: Sequence[str] = ()) -> pd.Series:
    """Average of robust z-scores, with sign inversion for risk-off legs.

    `invert` matters more than it looks. In a risk-on/risk-off basket, long
    gold and long equities are opposite expressions of the same sentiment. Not
    inverting one of them makes the composite cancel itself out and measure
    close to nothing.
    """
    zs = {}
    for name, s in components.items():
        z = robust_z(s, window)
        zs[name] = -z if name in invert else z
    return pd.DataFrame(zs).mean(axis=1, skipna=True)


def first_principal_component(frame: pd.DataFrame, window: int = 252) -> pd.Series:
    """Rolling first PC of standardised returns.

    Sign is pinned by correlation with the first column, since eigenvectors are
    only defined up to sign and would otherwise flip arbitrarily between
    windows, producing a series that looks like noise.
    """
    rets = frame.pct_change()
    out = pd.Series(index=frame.index, dtype=float)
    cols = list(frame.columns)

    for i in range(window, len(rets)):
        block = rets.iloc[i - window:i].dropna()
        if len(block) < window // 2 or block.shape[1] < 2:
            continue
        std = block.std().replace(0.0, np.nan)
        z = ((block - block.mean()) / std).dropna(axis=1, how="all").fillna(0.0)
        if z.shape[1] < 2:
            continue
        try:
            _, _, vt = np.linalg.svd(z.values, full_matrices=False)
        except np.linalg.LinAlgError:
            continue
        loadings = vt[0]
        if cols[0] in z.columns:
            anchor = list(z.columns).index(cols[0])
            if loadings[anchor] < 0:
                loadings = -loadings
        out.iloc[i] = float(z.values[-1] @ loadings)
    return out


def trend_replication(s: pd.Series, lookbacks: Sequence[int] = (21, 63, 252),
                      vol_window: int = 60, cap: float = 2.0) -> pd.Series:
    """Vol-scaled time-series momentum, the standard public approximation of a
    trend-follower's directional exposure.

    Used as a substitute where no CTA index is available. It is a substitute,
    not a replication, and should be labelled as such in the UI.
    """
    vol = realised_vol(s, vol_window)
    signals = []
    for lb in lookbacks:
        mom = s / s.shift(lb) - 1.0
        signals.append(np.sign(mom) * (mom.abs() / vol.replace(0.0, np.nan)).clip(upper=cap))
    return pd.concat(signals, axis=1).mean(axis=1)


# ===========================================================================
# Triggers
# ===========================================================================
@dataclass(frozen=True)
class TriggerRule:
    """One side of an input's rule. `pct` is 0-100."""
    rule: str                       # "gt" or "lt"
    pct: float
    window: Union[str, int] = "expanding"   # "expanding" or an observation count

    def __post_init__(self) -> None:
        if self.rule not in ("gt", "lt"):
            raise ValueError(f"rule must be 'gt' or 'lt', got {self.rule!r}")
        if not 0 < self.pct < 100:
            raise ValueError(f"pct must be strictly between 0 and 100, got {self.pct}")


def causal_percentile_rank(s: pd.Series, window: Union[str, int] = "expanding",
                           min_periods: int = 252) -> pd.Series:
    """Percentile rank of each observation within its own history.

    At date t the rank uses s[:t] inclusive and nothing after it. Returns the
    fraction of the window less than or equal to the current value, so the
    output is in (0, 1].
    """
    clean = s.dropna()
    if clean.empty:
        return pd.Series(index=s.index, dtype=float)

    def _rank(arr: np.ndarray) -> float:
        return float((arr <= arr[-1]).sum()) / float(len(arr))

    if window == "expanding":
        ranks = clean.expanding(min_periods=min_periods).apply(_rank, raw=True)
    else:
        w = int(window)
        ranks = clean.rolling(w, min_periods=min(min_periods, w)).apply(_rank, raw=True)
    return ranks.reindex(s.index)


def hinge(ranks: pd.Series, rule: TriggerRule) -> pd.Series:
    """Continuous firing strength in [0, 1].

    Zero at and below the threshold, one at the observed extreme. This is the
    compromise between HSBC's binary count and a plain z-score average: it
    keeps the tail-only non-linearity while recovering magnitude.
    """
    p = rule.pct / 100.0
    if rule.rule == "gt":
        denom = 1.0 - p
        raw = (ranks - p) / denom if denom > 0 else (ranks - p)
    else:
        denom = p
        raw = (p - ranks) / denom if denom > 0 else (p - ranks)
    return raw.clip(lower=0.0, upper=1.0)


def fired(ranks: pd.Series, rule: TriggerRule) -> pd.Series:
    """Binary firing, for the faithful HSBC replica."""
    p = rule.pct / 100.0
    out = ranks >= p if rule.rule == "gt" else ranks <= p
    return out.where(ranks.notna())


# ===========================================================================
# Inputs and aggregation
# ===========================================================================
@dataclass
class SentimentInput:
    """One of the 21 inputs, already transformed to its signal-ready form."""
    id: str
    series: pd.Series
    cluster: str
    sell: Optional[TriggerRule] = None
    buy: Optional[TriggerRule] = None
    label: str = ""
    is_substitute: bool = False     # true where we proxy a proprietary HSBC input
    min_periods: int = 252

    def ranks_for(self, side: str) -> pd.Series:
        rule = self.sell if side == "sell" else self.buy
        if rule is None:
            return pd.Series(index=self.series.index, dtype=float)
        return causal_percentile_rank(self.series, rule.window, self.min_periods)


@dataclass
class AggregationResult:
    """Everything the tab needs to render a reading honestly."""
    reading: pd.Series                  # 0-1, the headline
    denominator: pd.Series              # inputs available on each date
    fired_count: pd.Series
    per_input_hinge: pd.DataFrame
    per_input_fired: pd.DataFrame
    per_input_rank: pd.DataFrame
    dropped: pd.Series                  # ids unavailable on each date
    mode: str = ""
    side: str = ""


class SentimentEngine:
    """Builds both readings from the same inputs so they stay comparable."""

    def __init__(self, inputs: Sequence[SentimentInput],
                 cluster_weights: Optional[Dict[str, float]] = None):
        ids = [i.id for i in inputs]
        if len(ids) != len(set(ids)):
            dupes = {i for i in ids if ids.count(i) > 1}
            raise ValueError(f"duplicate input ids: {sorted(dupes)}")
        self.inputs = list(inputs)
        self.cluster_weights = cluster_weights

    # -- helpers ---------------------------------------------------------
    def _side_inputs(self, side: str) -> List[SentimentInput]:
        return [i for i in self.inputs if (i.sell if side == "sell" else i.buy) is not None]

    def published_denominator(self, side: str) -> int:
        """HSBC's own count: 20 sell, 13 buy."""
        return len(self._side_inputs(side))

    # -- the main computation --------------------------------------------
    def compute(self, side: str, mode: str = "improved",
                index: Optional[pd.Index] = None) -> AggregationResult:
        if side not in ("sell", "buy"):
            raise ValueError("side must be 'sell' or 'buy'")
        if mode not in ("replica", "improved"):
            raise ValueError("mode must be 'replica' or 'improved'")

        members = self._side_inputs(side)
        if not members:
            raise ValueError(f"no inputs carry a {side} rule")

        if index is None:
            index = members[0].series.index
            for m in members[1:]:
                index = index.union(m.series.index)

        ranks, hinges, fires = {}, {}, {}
        for inp in members:
            rule = inp.sell if side == "sell" else inp.buy
            r = inp.ranks_for(side).reindex(index)
            ranks[inp.id] = r
            hinges[inp.id] = hinge(r, rule)
            fires[inp.id] = fired(r, rule)

        rank_df = pd.DataFrame(ranks, index=index)
        hinge_df = pd.DataFrame(hinges, index=index)
        fire_df = pd.DataFrame(fires, index=index)

        available = rank_df.notna()
        denominator = available.sum(axis=1)
        fired_count = fire_df.fillna(False).astype(bool).sum(axis=1)

        if mode == "replica":
            # Equal-weighted binary count over available inputs. Dividing by
            # the live denominator rather than HSBC's fixed 20 or 13 means a
            # data outage shows as a smaller denominator, not as a fall in
            # sentiment.
            reading = fired_count / denominator.replace(0, np.nan)
        else:
            reading = self._cluster_weighted(hinge_df, available, members)

        dropped = available.apply(
            lambda row: sorted([c for c in available.columns if not row[c]]), axis=1
        )

        return AggregationResult(
            reading=reading, denominator=denominator, fired_count=fired_count,
            per_input_hinge=hinge_df, per_input_fired=fire_df, per_input_rank=rank_df,
            dropped=dropped, mode=mode, side=side,
        )

    def _cluster_weighted(self, hinge_df: pd.DataFrame, available: pd.DataFrame,
                          members: Sequence[SentimentInput]) -> pd.Series:
        """Average hinge within cluster, then across clusters.

        This is the de-duplication fix. An equal-weighted count over 20 inputs
        where eight are variations on "equity vol is low" is not a twenty-input
        indicator. Weighting by cluster stops whichever theme happens to have
        the most representatives from dominating the reading.

        Clusters with no available input on a date drop out and the remaining
        weights renormalise, so the reading stays on a 0-1 scale.
        """
        by_cluster: Dict[str, List[str]] = {}
        for m in members:
            by_cluster.setdefault(m.cluster, []).append(m.id)

        cluster_means, cluster_live = {}, {}
        for cluster, ids in by_cluster.items():
            sub = hinge_df[ids].where(available[ids])
            cluster_means[cluster] = sub.mean(axis=1, skipna=True)
            cluster_live[cluster] = available[ids].any(axis=1)

        means = pd.DataFrame(cluster_means)
        live = pd.DataFrame(cluster_live)

        if self.cluster_weights:
            w = pd.Series({c: self.cluster_weights.get(c, 1.0) for c in means.columns})
        else:
            w = pd.Series(1.0, index=means.columns)

        weights = live.astype(float).mul(w, axis=1)
        total = weights.sum(axis=1).replace(0.0, np.nan)
        return (means.fillna(0.0) * weights).sum(axis=1) / total

    def compute_all(self, index: Optional[pd.Index] = None) -> Dict[str, AggregationResult]:
        """Both sides in both modes, so replica and improved stay comparable."""
        out = {}
        for side in ("sell", "buy"):
            if not self._side_inputs(side):
                continue
            for mode in ("replica", "improved"):
                out[f"{side}_{mode}"] = self.compute(side, mode, index)
        return out


# ===========================================================================
# Bands
# ===========================================================================
PUBLISHED_BANDS: List[tuple] = [
    ("No signal", 0.00, 0.20),
    ("Mild", 0.20, 0.30),
    ("Moderate", 0.30, 0.40),
    ("Strong", 0.40, 0.50),
    ("Extreme", 0.50, 1.01),
]


def label_bands(reading: pd.Series, bands: Sequence[tuple] = PUBLISHED_BANDS) -> pd.Series:
    """Map a reading onto named bands.

    The published bands are round numbers, not calibrated thresholds. See
    sentiment_stats.calibrate_bands for the empirical alternative, which asks
    what reading is actually unusual given how correlated the inputs are.
    """
    def _label(v: float) -> Optional[str]:
        if pd.isna(v):
            return None
        for name, lo, hi in bands:
            if lo <= v < hi:
                return name
        return bands[-1][0]

    return reading.map(_label)


__all__ = [
    "TriggerRule", "SentimentInput", "SentimentEngine", "AggregationResult",
    "causal_percentile_rank", "hinge", "fired", "label_bands", "PUBLISHED_BANDS",
    "diff_horizon", "smooth", "robust_z", "rolling_beta", "rsi", "realised_vol",
    "vol_target_weight", "inverse_vol_weights", "mean_of_z",
    "first_principal_component", "trend_replication",
]
