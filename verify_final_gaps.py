#!/usr/bin/env python
"""
verify_final_gaps.py

Close the last two gaps: money market flows (input 21) and the CTA inputs
(1 and 4). Everything else is already resolved and verified.

Money market flows
------------------
usfund0066 looks right: weekly, 2003-12-22 to 2026-08-03, 1181 observations.
But the titles were truncated in the search output at "Weekly Money Market Fund
Assets, T...", and several siblings share that prefix with different final
words (Total, Retail, Institutional, Taxable, Prime). Picking the wrong one
would give a series that populates cleanly and charts plausibly while measuring
a subset of the universe. This prints the full titles so the choice is made on
evidence.

CTA inputs
----------
The Barclay Hedge CTA indices are monthly and 71 days stale. Neither is
survivable: a 20-day rolling beta needs daily data, and a live tab cannot carry
an input whose latest reading predates the last two FOMC meetings.

The substitute is CFTC Leveraged Funds positioning from the Traders in
Financial Futures report. Leveraged Funds are, in practice, the CTAs and
systematic macro funds, so this measures the same underlying idea directly
rather than inferring it from index returns. Weekly, current, from June 2006.

Be clear about what changes: HSBC's input is a rolling BETA (return
sensitivity); this is net POSITIONING. Related, not identical. It should be
labelled a substitute in the UI, not passed off as a replication.

Read-only. Prints a summary and writes one JSON.

Usage
-----
    python verify_final_gaps.py
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from typing import Any, Dict, List

# --- Money market and fund flow candidates, from the Macrobond search --------
MMF_CANDIDATES: List[str] = [
    # Longest history, the leading candidate for input 21.
    "usfund0066", "usfund0067",
    # 2007 starts, various cuts of the same universe.
    "usfund0057", "usfund0058", "usfund0059", "usfund0060",
    "usfund0252", "usfund0254", "usfund0068",
]
FLOW_CANDIDATES: List[str] = [
    # Estimated Long-Term Mutual Fund Flows, weekly from 2007. Would give the
    # flows cluster a second member instead of a single point of failure.
    "usfund0075", "usfund0076", "usfund0077",
    "usfund1927", "usfund1932", "usfund1933",
]

# --- CTA substitute: TFF Leveraged Funds -------------------------------------
# _14ftnet  Leveraged Funds, net, futures only
# _14otnet  Leveraged Funds, net, options and futures
# _11ftnet  Asset Manager/Institutional, net, futures (the other side of the
#           trade, useful as a cross-check rather than as an input)
CTA_SUBSTITUTE: Dict[str, Dict[str, Any]] = {
    "equity_leveraged_funds": {
        "hsbc_input": 1,
        "contract": "E-mini S&P 500 - CME",
        "codes": ["cftc_cme13874a_14otnet", "cftc_cme13874a_14ftnet",
                  "cftc_cme13874a_11ftnet"],
    },
    "usd_leveraged_funds": {
        "hsbc_input": 4,
        "contract": "U.S. Dollar Index - ICUS",
        "codes": ["cftc_icus098662_14otnet", "cftc_icus098662_14ftnet",
                  "cftc_icus098662_11ftnet"],
    },
}

# For the record, so the decision is auditable rather than implicit.
CTA_INDICES_REJECTED: Dict[str, str] = {
    "hfin0292": "Barclay Hedge CTA Index, monthly, last 2026-06-01",
    "hfin0313": "Barclay Hedge Systematic Traders, monthly, last 2026-06-01",
    "hfin0322": "Barclay Hedge BTOP50, monthly, last 2026-06-01",
}


def connect():
    import win32com.client  # type: ignore
    return win32com.client.Dispatch("Macrobond.Connection").Database


def probe(db, code: str) -> Dict[str, Any]:
    try:
        s = db.FetchOneSeries(code)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "code": code, "error": f"{type(exc).__name__}: {exc}"}
    if s.IsError:
        return {"ok": False, "code": code, "error": s.ErrorMessage}

    dates = list(s.DatesAtStartOfPeriod)
    values = [v for v in list(s.Values) if v is not None]
    if not dates or not values:
        return {"ok": False, "code": code, "error": "resolved but empty"}

    def _d(x):
        return x.date() if isinstance(x, dt.datetime) else x

    first, last = _d(dates[0]), _d(dates[-1])
    title = ""
    for key in ("FullDescription", "Description", "Title"):
        try:
            v = s.Metadata.GetFirstValue(key)
            if v and str(v).strip() and str(v) != "None":
                title = str(v)
                break
        except Exception:  # noqa: BLE001
            continue

    gap = (last - first).days / max(len(values) - 1, 1)
    return {
        "ok": True, "code": code, "title": title, "n": len(values),
        "first_date": str(first), "last_date": str(last),
        "last_value": float(values[-1]),
        "staleness_days": (dt.date.today() - last).days,
        "history_years": round((last - first).days / 365.25, 1),
        "frequency": ("daily" if gap <= 2 else "weekly" if gap <= 10
                      else "monthly" if gap <= 45 else "sparser"),
    }


def show(db, title: str, codes: List[str], note: str = "") -> List[Dict[str, Any]]:
    print(f"=== {title} ===")
    if note:
        print(f"    {note}\n")
    out = []
    for c in codes:
        r = probe(db, c)
        out.append(r)
        if r.get("ok"):
            flag = "  <-- STALE" if r["staleness_days"] > 21 else ""
            print(f"  {c:<26} {r['frequency']:<8} {r['first_date']} -> {r['last_date']}  "
                  f"n={r['n']:<6} {r['history_years']:>5}y  last={r['last_value']:>16,.0f}{flag}")
            print(f"    {r['title'][:112]}")
        else:
            print(f"  {c:<26} -- {r.get('error')}")
    print()
    return out


def main() -> int:
    try:
        db = connect()
    except Exception as exc:  # noqa: BLE001
        print(f"Could not reach Macrobond: {type(exc).__name__}: {exc}")
        print("Open the Macrobond application and sign in, then re-run.")
        return 1

    print("Connected.\n")
    report: Dict[str, Any] = {"run_at": dt.datetime.now().isoformat(timespec="seconds")}

    report["mmf"] = show(
        db, "Money market fund assets (HSBC input 21)", MMF_CANDIDATES,
        "Need the TOTAL universe, not a Retail/Institutional/Prime subset. "
        "The rule uses a 5-year rolling percentile, so longer history wins ties.")

    report["flows"] = show(
        db, "Long-term fund flows (candidate second member of the flows cluster)",
        FLOW_CANDIDATES,
        "Not an HSBC input. Worth having so the flows cluster is not one series deep.")

    for key, spec in CTA_SUBSTITUTE.items():
        report[key] = show(
            db, f"CTA substitute: {spec['contract']} (HSBC input {spec['hsbc_input']})",
            spec["codes"],
            "Leveraged Funds net positioning, standing in for the CTA beta. "
            "Prefer the options-and-futures variant for consistency with inputs 17 and 18.")

    print("=" * 74)
    print("Rejected for the CTA inputs, recorded so the decision is auditable:")
    for code, why in CTA_INDICES_REJECTED.items():
        print(f"  {code:<12} {why}")
    print("\n  Monthly frequency cannot support a 20-day rolling beta, and a")
    print("  71-day publication lag cannot support a live reading. Both would")
    print("  have to be fixed for these to be usable, and neither can be.")

    with open("macrobond_final_gaps.json", "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)
    print("\nWrote macrobond_final_gaps.json")

    ok_mmf = [r for r in report["mmf"] if r.get("ok")]
    ok_cta = [r for r in report["equity_leveraged_funds"] if r.get("ok")]
    print("\nWhat I need back: the full titles above. With those I can pick the")
    print("total-universe money market series and finalise the config.")
    print(f"(resolved: {len(ok_mmf)} money market, {len(ok_cta)} equity leveraged-fund)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
