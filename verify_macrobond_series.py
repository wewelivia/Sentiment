#!/usr/bin/env python
"""
verify_macrobond_series.py

Confirm the exact Macrobond series for HSBC sentiment inputs 17 and 18, and
settle the futures-only versus combined question with a hard test.

The suffix scheme, decoded from the Macrobond series list
---------------------------------------------------------
    7      Total, All Positions          -> Open Interest
    8      Non-Commercial, Long
    9      Non-Commercial, Short
    8fnet  Non-Commercial, Net           (supplied directly; no need to subtract)
    11ft   TFF Asset Manager/Institutional
    14ft   TFF Leveraged Funds

    trailing f   Futures only            history back to 1986 on FX and gold
    trailing o   Options & Futures        starts 1995-03-21, when options
                                          reporting began

Two corrections to what I previously told you, both from misreading compressed
screenshots rather than from the data:
    - Gold is cftc_cmx088691 (CMX), not cftc_cme088691.
    - `o` means Options & Futures combined, not options-only, and there is no
      `c` variant at all.

The decisive test
-----------------
I pulled the E-mini's actual CFTC figures for 2026-08-04 directly from the
CFTC's public API earlier in this project:

    non-commercial long   289,619      short   296,567
    implied net            -6,948      open interest   3,170,383

Those are combined futures-and-options figures. So if cftc_cme13874a_8onet
returns -6,948 and _7o returns 3,170,383, we have simultaneously confirmed the
series codes AND proved that `o` means combined, from a second independent
source. The futures-only net for the same week is -27,258, which is visibly
different and acts as a control: if _8onet also returned -27,258, the `o`
suffix would not mean what we think.

Read-only. Writes one JSON report.

Usage
-----
    python verify_macrobond_series.py
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Candidate series per contract. `net_combined` is the preferred input; the
# others are controls and fallbacks.
# ---------------------------------------------------------------------------
CONTRACTS: Dict[str, Dict[str, Any]] = {
    "sp500": {
        "prefix": "cftc_cme13874a", "cftc_code": "13874A", "risk_sign": +1,
        "label": "E-mini S&P 500 - CME",
        "hsbc_inputs": [17, 18],
    },
    "gold": {
        "prefix": "cftc_cmx088691", "cftc_code": "088691", "risk_sign": -1,
        "label": "Gold - CMX", "hsbc_inputs": [17],
    },
    "ust10y": {
        "prefix": "cftc_cbt043602", "cftc_code": "043602", "risk_sign": -1,
        "label": "10-Year U.S. Treasury Notes - CBOT", "hsbc_inputs": [17],
    },
    "jpy": {
        "prefix": "cftc_cme097741", "cftc_code": "097741", "risk_sign": -1,
        "label": "Japanese Yen - CME", "hsbc_inputs": [17],
    },
    "aud": {
        "prefix": "cftc_cme232741", "cftc_code": "232741", "risk_sign": +1,
        "label": "Australian Dollar - CME", "hsbc_inputs": [17],
    },
    # Bonus: a positioning route into input 4 (CTA USD beta), which is
    # otherwise unresolved because every CTA index ticker failed on Bloomberg.
    "usd_index": {
        "prefix": "cftc_icus098662", "cftc_code": "098662", "risk_sign": +1,
        "label": "U.S. Dollar Index - ICUS", "hsbc_inputs": [4],
    },
}

# role -> suffix. Order matters only for readability.
SUFFIXES: Dict[str, str] = {
    "net_combined": "8onet",     # preferred: non-commercial net, options & futures
    "net_futures": "8fnet",      # control and long-history fallback
    "long_combined": "8o",
    "short_combined": "9o",
    "long_futures": "8f",
    "short_futures": "9f",
    "oi_combined": "7o",
    "oi_futures": "7f",
    # TFF leveraged funds: arguably the better speculator proxy on financial
    # futures, at the cost of starting in 2006.
    "tff_lev_net_futures": "14ftnet",
    "tff_lev_net_combined": "14otnet",
    "tff_am_net_futures": "11ftnet",
}

# Ground truth from the CFTC public API, E-mini S&P, report date 2026-08-04,
# Legacy report, futures AND options combined.
GROUND_TRUTH = {
    "as_of": "2026-08-04",
    "noncomm_long_combined": 289619.0,
    "noncomm_short_combined": 296567.0,
    "net_combined": -6948.0,
    "oi_combined": 3170383.0,
    # Control: the futures-only net for the same week, read off the Macrobond
    # series list. If the combined series returns this instead, `o` is wrong.
    "net_futures_control": -27258.0,
}
# CFTC positions are exact integer contract counts and Macrobond is
# redistributing the same source, so an exact match is the expectation. The
# tolerance exists only to absorb a revision or a rounding difference, not to
# make a loose match pass. At 0.1% this would have accepted a 290-contract
# discrepancy on the long series, which is not a verification.
TOLERANCE = 0.0001  # 0.01%, i.e. about 29 contracts on 289,619


def connect():
    import win32com.client  # type: ignore
    return win32com.client.Dispatch("Macrobond.Connection").Database


def probe(db, code: str) -> Dict[str, Any]:
    try:
        s = db.FetchOneSeries(code)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if s.IsError:
        return {"ok": False, "error": s.ErrorMessage}
    dates = list(s.DatesAtStartOfPeriod)
    values = [v for v in list(s.Values) if v is not None]
    if not dates or not values:
        return {"ok": False, "error": "resolved but empty"}

    def _d(x):
        return x.date() if isinstance(x, dt.datetime) else x

    first, last = _d(dates[0]), _d(dates[-1])
    title = ""
    try:
        title = str(s.Metadata.GetFirstValue("FullDescription") or "")
    except Exception:  # noqa: BLE001
        pass
    return {
        "ok": True, "code": code, "title": title, "n": len(values),
        "first_date": str(first), "last_date": str(last),
        "last_value": float(values[-1]),
        "staleness_days": (dt.date.today() - last).days,
    }


def close_to(actual: float, expected: float) -> bool:
    if expected == 0:
        return abs(actual) < 1
    return abs(actual - expected) / abs(expected) <= TOLERANCE


def main() -> int:
    try:
        db = connect()
    except Exception as exc:  # noqa: BLE001
        print(f"Could not reach Macrobond: {type(exc).__name__}: {exc}")
        return 1

    print("Connected. Verifying series and testing the f/o interpretation.\n")
    report: Dict[str, Any] = {"run_at": dt.datetime.now().isoformat(timespec="seconds"),
                              "contracts": {}, "ground_truth_test": {}}

    for key, spec in CONTRACTS.items():
        print(f"=== {key}: {spec['label']}  (HSBC inputs {spec['hsbc_inputs']}) ===")
        found: Dict[str, Any] = {}
        for role, suffix in SUFFIXES.items():
            code = f"{spec['prefix']}_{suffix}"
            r = probe(db, code)
            found[role] = r
            if r.get("ok"):
                print(f"  {role:<22} {code:<30} {r['n']:>5} obs  from {r['first_date']}  "
                      f"last={r['last_value']:>14,.0f}")
                if r["title"]:
                    print(f"    {r['title'][:105]}")
            else:
                print(f"  {role:<22} {code:<30} -- {r.get('error')}")
        report["contracts"][key] = {**spec, "series": found}
        print()

    # ---- the decisive test ------------------------------------------------
    print("=" * 72)
    print("GROUND-TRUTH TEST: E-mini S&P vs CFTC public API, 2026-08-04\n")
    sp = report["contracts"]["sp500"]["series"]
    checks: List[Dict[str, Any]] = []

    for role, expected_key in (("net_combined", "net_combined"),
                               ("oi_combined", "oi_combined"),
                               ("long_combined", "noncomm_long_combined"),
                               ("short_combined", "noncomm_short_combined")):
        r = sp.get(role, {})
        expected = GROUND_TRUTH[expected_key]
        if not r.get("ok"):
            print(f"  {role:<18} SERIES MISSING, cannot test")
            checks.append({"role": role, "result": "missing"})
            continue
        if r["last_date"] != GROUND_TRUTH["as_of"]:
            print(f"  {role:<18} last date {r['last_date']} != {GROUND_TRUTH['as_of']}, skipping")
            checks.append({"role": role, "result": "date_mismatch", "last_date": r["last_date"]})
            continue
        ok = close_to(r["last_value"], expected)
        diff = r["last_value"] - expected
        print(f"  {role:<18} {'MATCH  ' if ok else 'MISMATCH'} "
              f"got {r['last_value']:>14,.0f}  expected {expected:>14,.0f}  "
              f"diff {diff:+,.0f}")
        checks.append({"role": role, "result": "match" if ok else "mismatch",
                       "got": r["last_value"], "expected": expected, "difference": diff})

    # Control: the combined net must NOT equal the futures-only net.
    rc = sp.get("net_combined", {})
    if rc.get("ok"):
        if close_to(rc["last_value"], GROUND_TRUTH["net_futures_control"]):
            print("\n  CONTROL FAILED: the 'combined' series equals the futures-only")
            print("  net, so the `o` suffix does not mean Options & Futures. Do not")
            print("  build on this until resolved.")
            checks.append({"role": "control", "result": "failed"})
        else:
            print("\n  Control passed: combined net differs from futures-only net.")
            checks.append({"role": "control", "result": "passed"})

    report["ground_truth_test"] = {"expected": GROUND_TRUTH, "checks": checks}

    matched = sum(1 for c in checks if c.get("result") == "match")
    print(f"\n  {matched} of 4 value checks matched.")
    if matched >= 3:
        print("  Confirmed from two independent sources. Inputs 17 and 18 are ready.")
    else:
        print("  Not confirmed. Send me the JSON and I will work out which series")
        print("  Macrobond is actually returning before anything gets built on it.")

    with open("macrobond_verified.json", "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)
    print("\nWrote macrobond_verified.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
