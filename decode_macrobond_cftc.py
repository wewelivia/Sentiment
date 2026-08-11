#!/usr/bin/env python
"""
decode_macrobond_cftc.py

Decode the Macrobond CFTC suffix scheme so we know which series is
non-commercial long, which is short, and which is open interest.

Where we are
------------
All five contract prefixes are now known:

    sp500    cftc_cme13874a      confirmed, 1503 obs from 1997-09-16
    jpy      cftc_cme097741      confirmed, ~1920 obs from 1986
    aud      cftc_cme232741      confirmed, ~1792 obs from 1987
    gold     cftc_cme088691      resolved by construction, 1928 obs from 1986-01-13
    ust10y   cftc_cbt043602      resolved by construction, ~1920 obs from 1986

Note gold is under `cme`, not `cmx`: Macrobond files the whole CME Group,
COMEX included, under one prefix. That is why the COMEX guesses failed.

What is still unknown
---------------------
The suffix meanings. The previous probe returned empty titles because
FullDescription did not resolve as a metadata key, so we got codes without
knowing what they contain. Using the wrong suffix would silently produce a
plausible-looking but wrong indicator, which is the worst failure mode
available to us.

A hypothesis worth testing, not assuming
----------------------------------------
Observed on gold: `_9f` has 1928 points from 1986, while `_87o` and `_41o`
have ~1636 from 1995. CFTC options reporting began in 1995. That is consistent
with a trailing letter encoding the report basis:

    f = futures only        long history, back to the 1980s
    o = options             starts ~1995
    c = combined            starts ~1995

If it holds, `c` is what we want: HSBC's construction uses futures and options
combined. This script tests the hypothesis rather than trusting it, by
reporting each series' start date alongside its decoded description.

What it does
------------
1. Enumerates suffixes against each known prefix.
2. For every hit, dumps the COMPLETE metadata dictionary, trying several ways
   to list keys since the COM metadata interface varies by version.
3. Groups results so the suffix scheme becomes readable at a glance.

Read-only. Writes one JSON report.

Usage
-----
    python decode_macrobond_cftc.py
    python decode_macrobond_cftc.py --only gold
    python decode_macrobond_cftc.py --wide      # much broader suffix sweep
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from typing import Any, Dict, List, Optional

PREFIXES: Dict[str, Dict[str, Any]] = {
    "sp500":  {"prefix": "cftc_cme13874a",  "cftc_code": "13874A", "risk_sign": +1},
    "jpy":    {"prefix": "cftc_cme097741",  "cftc_code": "097741", "risk_sign": -1},
    "aud":    {"prefix": "cftc_cme232741",  "cftc_code": "232741", "risk_sign": +1},
    "gold":   {"prefix": "cftc_cme088691",  "cftc_code": "088691", "risk_sign": -1},
    "ust10y": {"prefix": "cftc_cbt043602",  "cftc_code": "043602", "risk_sign": -1},
}

# Suffixes actually observed in the two previous probes, plus their siblings.
# The numeric part appears to encode trader type and direction; the trailing
# letter the report basis.
OBSERVED = ["9c", "9f", "9o", "38f", "38c", "38o", "41c", "41f", "41o",
            "87o", "87c", "87f", "46ct", "48f", "48c", "48o"]

# Metadata keys worth asking for by name. The previous probe failed because it
# asked only for FullDescription.
META_KEYS = ["FullDescription", "Description", "Title", "LongLabel", "ShortLabel",
             "Frequency", "Region", "Source", "Class", "Category", "DisplayUnit",
             "Currency", "PrimName", "Name", "IHCategory", "cftc_trader_category",
             "cftc_position_type", "cftc_report_type"]


def build_suffixes(wide: bool) -> List[str]:
    if not wide:
        return OBSERVED
    out = list(OBSERVED)
    # Systematic sweep. Kept bounded: this is one COM call per code and a wide
    # run across five contracts is already a few thousand calls.
    for n in list(range(1, 100)):
        for letter in ("f", "c", "o"):
            s = f"{n}{letter}"
            if s not in out:
                out.append(s)
    return out


def connect():
    import win32com.client  # type: ignore
    return win32com.client.Dispatch("Macrobond.Connection").Database


def dump_metadata(series) -> Dict[str, Any]:
    """Get everything the metadata object will give us.

    Tries enumeration first, since that returns keys we would not think to ask
    for, then falls back to querying a known list by name.
    """
    meta: Dict[str, Any] = {}
    md = getattr(series, "Metadata", None)
    if md is None:
        return meta

    # Route 1: enumerate all keys.
    for lister in ("ListNames", "GetNames", "Names"):
        try:
            attr = getattr(md, lister, None)
            names = attr() if callable(attr) else attr
            for n in list(names or []):
                key = str(n)
                try:
                    meta[key] = str(md.GetFirstValue(key))
                except Exception:  # noqa: BLE001
                    continue
            if meta:
                return meta
        except Exception:  # noqa: BLE001
            continue

    # Route 2: iterate the object directly.
    try:
        for item in md:
            try:
                meta[str(item)] = str(md.GetFirstValue(str(item)))
            except Exception:  # noqa: BLE001
                continue
        if meta:
            return meta
    except Exception:  # noqa: BLE001
        pass

    # Route 3: ask for known keys by name.
    for key in META_KEYS:
        try:
            v = md.GetFirstValue(key)
            if v is not None and str(v).strip():
                meta[key] = str(v)
        except Exception:  # noqa: BLE001
            continue
    return meta


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
    meta = dump_metadata(s)
    # The description is the whole point of this run, so surface whichever key
    # actually carried it.
    desc = ""
    for k in ("FullDescription", "Description", "Title", "LongLabel", "ShortLabel"):
        if meta.get(k):
            desc = meta[k]
            break

    return {
        "ok": True, "code": code, "description": desc,
        "n": len(values), "first_date": str(first), "last_date": str(last),
        "last_value": values[-1],
        "staleness_days": (dt.date.today() - last).days,
        "metadata": meta,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None)
    ap.add_argument("--wide", action="store_true", help="Sweep suffixes 1-99 x f/c/o.")
    ap.add_argument("--outdir", default=".")
    args = ap.parse_args()

    try:
        db = connect()
    except Exception as exc:  # noqa: BLE001
        print(f"Could not reach Macrobond: {type(exc).__name__}: {exc}")
        print("Open the Macrobond application and sign in, then re-run.")
        return 1

    suffixes = build_suffixes(args.wide)
    only = [s.strip() for s in args.only.split(",")] if args.only else None
    targets = {k: v for k, v in PREFIXES.items() if not only or k in only}

    print(f"Connected. Testing {len(suffixes)} suffixes across {len(targets)} contract(s).")
    print(f"{len(suffixes) * len(targets)} probes total.\n")

    report: Dict[str, Any] = {"run_at": dt.datetime.now().isoformat(timespec="seconds"),
                              "contracts": {}}

    for key, spec in targets.items():
        print(f"=== {key}  ({spec['prefix']}) ===")
        hits: List[Dict[str, Any]] = []
        for suf in suffixes:
            code = f"{spec['prefix']}_{suf}"
            r = probe(db, code)
            if r.get("ok"):
                r["suffix"] = suf
                hits.append(r)
                print(f"  {suf:<6} {r['n']:>5} obs  {r['first_date']}  last={r['last_value']:>14,.0f}")
                if r["description"]:
                    print(f"         {r['description'][:110]}")
                else:
                    keys = list(r["metadata"].keys())[:6]
                    print(f"         (no description; metadata keys found: {keys})")

        report["contracts"][key] = {**spec, "hits": hits}

        # Test the futures/options/combined hypothesis against start dates.
        if hits:
            print("\n  start-date grouping (tests the f/o/c hypothesis):")
            by_letter: Dict[str, List[str]] = {}
            for h in hits:
                letter = h["suffix"][-1]
                by_letter.setdefault(letter, []).append(h["first_date"])
            for letter, starts in sorted(by_letter.items()):
                earliest = min(starts)
                print(f"    '{letter}' suffixes: earliest start {earliest}  ({len(starts)} series)")
            print("    If 'f' starts in the 1980s and 'o'/'c' around 1995, the")
            print("    hypothesis holds and 'c' (combined) is the one to use.\n")
        else:
            print("  no hits\n")

    path = f"{args.outdir}/macrobond_cftc_decoded.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)

    print("=" * 72)
    print("Send me macrobond_cftc_decoded.json. The descriptions are what I need:")
    print("with those I can pick long, short and open interest per contract and")
    print("write the provider. If descriptions are still blank, the start-date")
    print("grouping above is enough to proceed on, with a caveat in the config.")
    print(f"\nWrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
