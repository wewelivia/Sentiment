#!/usr/bin/env python
"""
diagnose_macrobond.py

Test whether Macrobond can fill the five gaps left by the Bloomberg diagnostics,
and find the exact series codes rather than guessing them.

Why Macrobond is the right route
--------------------------------
It is an approved vendor with a licensed desktop installation. The Python API
talks to that local application over COM, so this script opens no network
connection of its own and introduces no new data-governance question. Contrast
with the CFTC public API, which required punching through an egress control.

What we need it to cover
------------------------
    17, 18  CFTC positioning        (BQL not entitled; public API blocked)
    21      Money market fund flows (all ICI Bloomberg tickers invalid)
    20 leg  Investors Intelligence  (paid Chartcraft feed, no Bloomberg ticker)
    1, 4    CTA index               (all Bloomberg candidates invalid)

Anything Macrobond covers here is strictly better than the alternative, because
it is licensed, governed, and already on the machine.

Approach
--------
The Bloomberg round wasted two passes guessing ticker symbology. This does not
repeat that mistake: it uses Macrobond's search interface to find candidate
series by description, then reports their codes, frequency, history depth and
staleness. You get real codes back rather than my guesses.

Two client paths are tried, in order:
  1. macrobond_data_api (the modern package) using the Com backend.
  2. Raw COM via win32com, which needs only pywin32 and no new install.

Read-only. Fetches data but writes nothing except its own report.

Usage
-----
    python diagnose_macrobond.py
    python diagnose_macrobond.py --max-hits 8
    python diagnose_macrobond.py --only cftc,ici
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# What to look for. Several phrasings per target, because search wording
# matters and we would rather over-search once than iterate on your machine.
# ---------------------------------------------------------------------------
SEARCHES: Dict[str, Dict[str, Any]] = {
    "cftc": {
        "hsbc_inputs": [17, 18],
        "label": "CFTC Commitments of Traders positioning",
        "queries": [
            "CFTC E-mini S&P 500 non-commercial net positions",
            "Commitments of Traders S&P 500 non-commercial",
            "CFTC non-commercial net long gold",
            "CFTC commitments traders 10-year Treasury note non-commercial",
            "CFTC non-commercial Japanese yen futures",
            "CFTC non-commercial Australian dollar futures",
            "CFTC open interest S&P 500 futures",
        ],
        "why": "Highest value gap. Positioning is the strongest part of a sentiment aggregate.",
    },
    "ici": {
        "hsbc_inputs": [21],
        "label": "Money market fund assets and flows",
        "queries": [
            "ICI money market fund total net assets weekly",
            "money market fund assets United States weekly",
            "Investment Company Institute money market funds",
            "ICI mutual fund flows equity",
        ],
        "why": "Sole member of the flows cluster, so losing it removes the dimension entirely.",
    },
    "surveys": {
        "hsbc_inputs": [20],
        "label": "Investors Intelligence sentiment survey",
        "queries": [
            "Investors Intelligence bulls bears advisors sentiment",
            "Investors Intelligence bullish percentage",
            "advisory sentiment bulls bears United States",
            "NAAIM exposure index",
            "AAII investor sentiment bullish",
        ],
        "why": "Third leg of the survey composite. AAII and NAAIM already work via Bloomberg.",
    },
    "cta": {
        "hsbc_inputs": [1, 4],
        "label": "CTA / managed futures index, daily",
        "queries": [
            "SG CTA Index Societe Generale",
            "SG Trend Index managed futures",
            "Barclay CTA Index",
            "managed futures index daily",
            "HFRX macro systematic diversified CTA",
        ],
        "why": "Needs DAILY frequency. A monthly index cannot support a 20-day rolling beta.",
    },
}


# ---------------------------------------------------------------------------
# Client abstraction. Both backends expose the same two operations to us:
# search(text) -> list of dicts, and probe(code) -> metadata dict.
# ---------------------------------------------------------------------------
class MacrobondClient:
    backend = "none"

    def search(self, text: str, max_hits: int) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def probe(self, code: str) -> Dict[str, Any]:
        raise NotImplementedError

    def close(self) -> None:
        pass


class ModernClient(MacrobondClient):
    """macrobond_data_api with the Com backend."""
    backend = "macrobond_data_api (Com)"

    def __init__(self) -> None:
        from macrobond_data_api.com import ComClient  # type: ignore
        self._ctx = ComClient()
        self.api = self._ctx.__enter__()

    def close(self) -> None:
        try:
            self._ctx.__exit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass

    def search(self, text: str, max_hits: int) -> List[Dict[str, Any]]:
        # Method naming has moved between versions; try the known forms.
        for name in ("entity_search", "search", "entity_search_multi_filter"):
            fn = getattr(self.api, name, None)
            if fn is None:
                continue
            try:
                try:
                    res = fn(text=text, entity_types="TimeSeries")
                except TypeError:
                    res = fn(text)
            except Exception as exc:  # noqa: BLE001
                return [{"_error": f"{name}: {type(exc).__name__}: {exc}"}]

            out: List[Dict[str, Any]] = []
            for item in list(res)[:max_hits]:
                d = dict(item) if not isinstance(item, dict) else item
                out.append({
                    "code": d.get("Name") or d.get("name") or d.get("PrimName"),
                    "title": d.get("FullDescription") or d.get("Description") or d.get("description"),
                    "region": d.get("Region") or d.get("region"),
                    "frequency": d.get("Frequency") or d.get("frequency"),
                })
            return out
        return [{"_error": "no search method found on this api version"}]

    def probe(self, code: str) -> Dict[str, Any]:
        s = self.api.get_one_series(code)
        if getattr(s, "is_error", False):
            return {"ok": False, "error": getattr(s, "error_message", "unknown")}
        dates = list(getattr(s, "dates", []) or [])
        values = [v for v in (getattr(s, "values", []) or []) if v is not None]
        return _summarise(code, dates, values, getattr(s, "metadata", {}) or {})


class ComClientRaw(MacrobondClient):
    """Direct COM. Needs only pywin32, which is normally already present."""
    backend = "win32com (Macrobond.Connection)"

    def __init__(self) -> None:
        import win32com.client  # type: ignore
        self.conn = win32com.client.Dispatch("Macrobond.Connection")
        self.db = self.conn.Database

    def search(self, text: str, max_hits: int) -> List[Dict[str, Any]]:
        try:
            q = self.db.CreateSearchQuery()
            q.Text = text
            q.SetEntityTypeFilter("TimeSeries")
            res = self.db.Search(q)
        except Exception as exc:  # noqa: BLE001
            return [{"_error": f"{type(exc).__name__}: {exc}"}]

        out: List[Dict[str, Any]] = []
        try:
            entities = list(res.Entities)[:max_hits]
        except Exception:  # noqa: BLE001
            entities = []
        for e in entities:
            try:
                out.append({
                    "code": e.Name,
                    "title": e.Title,
                    "region": _meta(e, "Region"),
                    "frequency": _meta(e, "Frequency"),
                })
            except Exception as exc:  # noqa: BLE001
                out.append({"_error": str(exc)})
        return out

    def probe(self, code: str) -> Dict[str, Any]:
        s = self.db.FetchOneSeries(code)
        if s.IsError:
            return {"ok": False, "error": s.ErrorMessage}
        dates = list(s.DatesAtStartOfPeriod)
        values = [v for v in list(s.Values) if v is not None]
        meta = {}
        try:
            meta = {"Frequency": _meta(s, "Frequency"), "Region": _meta(s, "Region")}
        except Exception:  # noqa: BLE001
            pass
        return _summarise(code, dates, values, meta)


def _meta(entity: Any, key: str) -> Optional[str]:
    try:
        return str(entity.Metadata.GetFirstValue(key))
    except Exception:  # noqa: BLE001
        return None


def _summarise(code: str, dates: List[Any], values: List[Any], meta: Dict[str, Any]) -> Dict[str, Any]:
    if not dates or not values:
        return {"ok": False, "error": "series resolved but returned no observations"}

    def _d(x: Any) -> Optional[dt.date]:
        if isinstance(x, dt.datetime):
            return x.date()
        if isinstance(x, dt.date):
            return x
        try:
            return dt.datetime.fromisoformat(str(x)[:10]).date()
        except Exception:  # noqa: BLE001
            return None

    first, last = _d(dates[0]), _d(dates[-1])
    out: Dict[str, Any] = {
        "ok": True,
        "code": code,
        "n": len(values),
        "first_date": str(first) if first else None,
        "last_date": str(last) if last else None,
        "last_value": values[-1],
        "frequency": meta.get("Frequency") if isinstance(meta, dict) else None,
    }
    if first and last:
        out["history_years"] = round((last - first).days / 365.25, 1)
        out["staleness_days"] = (dt.date.today() - last).days
        # Daily frequency is a hard requirement for the CTA inputs specifically.
        gap = (last - first).days / max(len(values) - 1, 1)
        out["median_gap_days"] = round(gap, 1)
        out["inferred_frequency"] = (
            "daily" if gap <= 2 else "weekly" if gap <= 10
            else "monthly" if gap <= 45 else "quarterly_or_sparser"
        )
    return out


# ---------------------------------------------------------------------------
def connect() -> Optional[MacrobondClient]:
    print("Connecting to Macrobond.\n")

    try:
        c = ModernClient()
        print(f"  Connected via {c.backend}\n")
        return c
    except ImportError:
        print("  macrobond_data_api not installed (pip install macrobond-data-api).")
        print("  Falling back to raw COM, which needs only pywin32.")
    except Exception as exc:  # noqa: BLE001
        print(f"  macrobond_data_api present but failed: {type(exc).__name__}: {exc}")
        print("  Falling back to raw COM.")

    try:
        c = ComClientRaw()
        print(f"  Connected via {c.backend}\n")
        return c
    except ImportError:
        print("  pywin32 not installed either (pip install pywin32).")
    except Exception as exc:  # noqa: BLE001
        print(f"  Raw COM failed: {type(exc).__name__}: {exc}")
        print("\n  Usual causes, in order: the Macrobond desktop application is not")
        print("  installed, or it is installed but not running and logged in. The COM")
        print("  server is provided by the application itself, so open Macrobond and")
        print("  sign in, then re-run.")
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Probe Macrobond for the remaining sentiment inputs.")
    ap.add_argument("--max-hits", type=int, default=6)
    ap.add_argument("--only", default=None, help="Comma-separated: cftc,ici,surveys,cta")
    ap.add_argument("--outdir", default=".")
    args = ap.parse_args()

    client = connect()
    if client is None:
        print("\nCould not reach Macrobond. Nothing was changed.")
        return 1

    report: Dict[str, Any] = {
        "run_at": dt.datetime.now().isoformat(timespec="seconds"),
        "backend": client.backend,
        "targets": {},
    }

    # Sanity check the connection with a series that should exist on any licence.
    print("Verifying the connection with a known series.")
    verified = False
    for probe_code in ("usgdp", "usnaac0169", "usrate0001"):
        try:
            r = client.probe(probe_code)
            if r.get("ok"):
                print(f"  OK  {probe_code}: {r['n']} obs, {r['first_date']} to {r['last_date']}\n")
                report["connection_check"] = r
                verified = True
                break
        except Exception:  # noqa: BLE001
            continue
    if not verified:
        print("  Could not fetch any reference series. Search results below may be unreliable.\n")

    only = [s.strip() for s in args.only.split(",")] if args.only else None

    for key, spec in SEARCHES.items():
        if only and key not in only:
            continue
        print(f"=== {spec['label']}  (HSBC inputs {spec['hsbc_inputs']}) ===")
        print(f"    {spec['why']}\n")
        target: Dict[str, Any] = {"label": spec["label"], "hsbc_inputs": spec["hsbc_inputs"], "queries": {}}

        for q in spec["queries"]:
            print(f"  search: {q}")
            try:
                hits = client.search(q, args.max_hits)
            except Exception as exc:  # noqa: BLE001
                print(f"    ERROR {type(exc).__name__}: {exc}")
                target["queries"][q] = {"error": str(exc)}
                continue

            if hits and "_error" in hits[0]:
                print(f"    ERROR {hits[0]['_error']}")
                target["queries"][q] = {"error": hits[0]["_error"]}
                continue
            if not hits:
                print("    no hits")
                target["queries"][q] = {"hits": []}
                continue

            enriched = []
            for h in hits:
                code = h.get("code")
                if not code:
                    continue
                try:
                    meta = client.probe(code)
                except Exception as exc:  # noqa: BLE001
                    meta = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                if meta.get("ok"):
                    print(f"    {code:<28} {meta.get('inferred_frequency','?'):<9} "
                          f"{meta['first_date']} to {meta['last_date']} "
                          f"({meta.get('history_years','?')}y, {meta.get('staleness_days','?')}d old)")
                    print(f"      {str(h.get('title'))[:100]}")
                else:
                    print(f"    {code:<28} unreadable: {meta.get('error')}")
                enriched.append({"search_hit": h, "series": meta})
            target["queries"][q] = {"hits": enriched}
            print()

        report["targets"][key] = target
        print()

    client.close()

    path = f"{args.outdir}/macrobond_probe.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)

    print("=" * 72)
    print("Send me macrobond_probe.json and I will pick the right series codes,")
    print("wire them into sentiment_tickers.yaml, and write the provider.")
    print("\nWhat I am looking for in the results:")
    print("  CFTC     long weekly history and an open-interest series to scale by")
    print("  ICI      weekly, ideally back to the mid-1990s")
    print("  surveys  Investors Intelligence weekly bulls and bears")
    print("  CTA      DAILY frequency; a monthly index is no use for a 20-day beta")
    print(f"\nWrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
