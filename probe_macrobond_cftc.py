#!/usr/bin/env python
"""
probe_macrobond_cftc.py

Pin down the exact Macrobond series for the CFTC inputs, now that the first
probe has revealed the naming convention.

What the first probe established
--------------------------------
Macrobond is reachable via raw COM, and CFTC data is there with better history
than the CFTC's own API in places. Observed:

    cftc_cme13874a_*   S&P 500 Consolidated / E-mini, weekly, 1503 obs from 1997-09-16
    cftc_cme097741_*   Japanese Yen, weekly, ~1920 obs from 1986
    cftc_cme232741_*   Australian Dollar, weekly, ~1792 obs from 1987

Note the Japanese Yen series starts in 1986 and the Aussie in 1987, roughly a
decade before the CFTC's own public dataset. The 1503 count for the S&P also
cross-checks against the 1502 rows I pulled from the CFTC API directly, which
is a reassuring independent confirmation that we are looking at the same data.

Two searches returned nothing: gold and the 10-year Treasury. That is a
phrasing problem, not an availability problem, and this script fixes it in two
ways rather than guessing again.

Approach
--------
1. Codes follow a rigid pattern: cftc_<exchange><cftc_contract_code>_<suffix>.
   Gold is on COMEX and the 10-year note is on CBOT, so their prefixes differ
   from the CME ones we found. We construct and test the plausible prefixes.

2. Titles follow a rigid template:
       "United States, CFTC COT Report, Futures, Non-Commercial, Long, All,
        <CONTRACT> - <EXCHANGE>, Positions"
   Searching with that exact wording is far more precise than the natural
   language queries used in the first pass.

3. For every hit we need to distinguish long, short and open interest, so the
   script groups results by measure and reports which suffix means what. That
   suffix dictionary is what the provider will encode.

Read-only. Fetches series metadata and writes one JSON report.

Usage
-----
    python probe_macrobond_cftc.py
    python probe_macrobond_cftc.py --only gold,ust10y
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# The five contracts. `code` is the CFTC contract market code, verified against
# the CFTC API earlier. `prefixes` are the Macrobond code stems to try, ordered
# by likelihood given the pattern already observed on CME contracts.
# ---------------------------------------------------------------------------
CONTRACTS: Dict[str, Dict[str, Any]] = {
    "sp500": {
        "cftc_code": "13874A",
        "known_prefix": "cftc_cme13874a",     # CONFIRMED in the first probe
        "prefixes": ["cftc_cme13874a"],
        "titles": ["E-mini S&P 500 Stock Index - CME", "S&P 500 Consolidated - CME"],
        "risk_sign": +1,
        "status": "confirmed",
    },
    "jpy": {
        "cftc_code": "097741",
        "known_prefix": "cftc_cme097741",     # CONFIRMED
        "prefixes": ["cftc_cme097741"],
        "titles": ["Japanese Yen - CME"],
        "risk_sign": -1,
        "status": "confirmed",
    },
    "aud": {
        "cftc_code": "232741",
        "known_prefix": "cftc_cme232741",     # CONFIRMED
        "prefixes": ["cftc_cme232741"],
        "titles": ["Australian Dollar - CME"],
        "risk_sign": +1,
        "status": "confirmed",
    },
    "gold": {
        "cftc_code": "088691",
        "known_prefix": None,                 # search returned nothing, hence this script
        "prefixes": ["cftc_cmx088691", "cftc_comex088691", "cftc_cme088691", "cftc_nym088691"],
        "titles": ["Gold - COMEX", "Gold - CMX", "Gold - Commodity Exchange Inc."],
        "risk_sign": -1,
        "status": "unresolved",
    },
    "ust10y": {
        "cftc_code": "043602",
        "known_prefix": None,
        "prefixes": ["cftc_cbt043602", "cftc_cbot043602", "cftc_cme043602"],
        "titles": ["UST 10Y Note - CBT", "10-Year U.S. Treasury Notes - CBT",
                   "10 Year U.S. Treasury Notes - CBOT", "US Treasury Note 10Y - CBT"],
        "risk_sign": -1,
        "status": "unresolved",
    },
}

# The three measures the engine needs, and the template wording that isolates
# each. Open interest is not optional: E-mini open interest grew 355x over the
# sample, so raw net contract counts are not comparable across it and the
# percentile trigger must run on net scaled by open interest.
MEASURES = {
    "noncomm_long": "CFTC COT Report, Futures, Non-Commercial, Long, All, {title}, Positions",
    "noncomm_short": "CFTC COT Report, Futures, Non-Commercial, Short, All, {title}, Positions",
    "open_interest": "CFTC COT Report, Futures, Total, All Positions, All, {title}, Open Interest",
}


def connect():
    """Raw COM, which is what worked in the first probe."""
    import win32com.client  # type: ignore
    conn = win32com.client.Dispatch("Macrobond.Connection")
    return conn.Database


def probe_series(db, code: str) -> Dict[str, Any]:
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
        "last_value": values[-1],
        "history_years": round((last - first).days / 365.25, 1),
        "staleness_days": (dt.date.today() - last).days,
    }


def search(db, text: str, max_hits: int = 8) -> List[Dict[str, Any]]:
    try:
        q = db.CreateSearchQuery()
        q.Text = text
        q.SetEntityTypeFilter("TimeSeries")
        res = db.Search(q)
        out = []
        for e in list(res.Entities)[:max_hits]:
            try:
                out.append({"code": e.Name, "title": e.Title})
            except Exception:  # noqa: BLE001
                continue
        return out
    except Exception as exc:  # noqa: BLE001
        return [{"_error": f"{type(exc).__name__}: {exc}"}]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None, help="Comma-separated contract keys.")
    ap.add_argument("--outdir", default=".")
    args = ap.parse_args()

    try:
        db = connect()
    except Exception as exc:  # noqa: BLE001
        print(f"Could not reach Macrobond: {type(exc).__name__}: {exc}")
        print("Open the Macrobond desktop application and sign in, then re-run.")
        return 1

    print("Connected to Macrobond via COM.\n")
    only = [s.strip() for s in args.only.split(",")] if args.only else None
    report: Dict[str, Any] = {"run_at": dt.datetime.now().isoformat(timespec="seconds"), "contracts": {}}

    for key, spec in CONTRACTS.items():
        if only and key not in only:
            continue
        print(f"=== {key}  (CFTC {spec['cftc_code']}, status: {spec['status']}) ===")
        entry: Dict[str, Any] = {"cftc_code": spec["cftc_code"], "risk_sign": spec["risk_sign"],
                                 "measures": {}, "search_hits": {}}

        # --- Route 1: template-worded search, one per measure ---------------
        for measure, template in MEASURES.items():
            found: List[Dict[str, Any]] = []
            for title in spec["titles"]:
                text = template.format(title=title)
                hits = search(db, text)
                if hits and "_error" in hits[0]:
                    print(f"  search error: {hits[0]['_error']}")
                    continue
                for h in hits:
                    meta = probe_series(db, h["code"])
                    if meta.get("ok"):
                        found.append(meta)
                if found:
                    break  # this title worked, no need for the alternates

            # Keep the longest history, which for CFTC is the right tiebreak:
            # consolidated contracts generally run further back than e-minis.
            found.sort(key=lambda m: m["n"], reverse=True)
            entry["measures"][measure] = found[:4]
            if found:
                best = found[0]
                print(f"  {measure:<14} {best['code']:<26} {best['n']:>5} obs  "
                      f"{best['first_date']} to {best['last_date']}  ({best['staleness_days']}d old)")
                print(f"    {best['title'][:110]}")
            else:
                print(f"  {measure:<14} NOT FOUND via template search")

        # --- Route 2: constructed codes, for the two unresolved contracts ---
        if spec["status"] == "unresolved":
            print("  trying constructed code prefixes:")
            tried = []
            for prefix in spec["prefixes"]:
                # Reuse suffixes seen on the confirmed CME contracts.
                for suffix in ("_9c", "_9f", "_87o", "_38f", "_41c", "_41o"):
                    code = prefix + suffix
                    meta = probe_series(db, code)
                    tried.append({"code": code, **meta})
                    if meta.get("ok"):
                        print(f"    HIT {code:<26} {meta['n']:>5} obs  {meta['first_date']} to {meta['last_date']}")
                        print(f"      {meta['title'][:110]}")
            entry["constructed"] = tried
            if not any(t.get("ok") for t in tried):
                print("    no constructed prefix resolved")

        report["contracts"][key] = entry
        print()

    path = f"{args.outdir}/macrobond_cftc.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)

    print("=" * 72)
    print("Send me macrobond_cftc.json. I need, per contract: the non-commercial")
    print("long code, the short code, and the open interest code. With those, inputs")
    print("17 and 18 are done and the CFTC gap closes through a licensed vendor.")
    print(f"\nWrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
