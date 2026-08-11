#!/usr/bin/env python
"""
diagnose_bql.py

Probe whether CFTC Commitments of Traders data is reachable via BQL from the
Desktop API, and work out the exact query syntax for the sentiment indicator's
positioning inputs (HSBC inputs 17 and 18).

Why this exists
---------------
Round 1 of diagnose_sentiment.py failed every CFTC ticker candidate with
INVALID_TICKER. That was the right answer to the wrong question: Bloomberg does
not expose COT as a ticker at all. It exposes it as the BQL functions
`cot_position()` and `cot_traders()`, which live behind //blp/bqlsvc rather
than //blp/refdata.

Two things are genuinely uncertain and this script resolves both:

  1. Whether //blp/bqlsvc opens on your Desktop API licence. BQL is documented
     for BQuant and B-PIPE; Desktop access exists but is not universal. If the
     service will not open, no amount of query tuning helps.
  2. The exact parameter and date-range syntax. The COT example you have shows
     the function signature but not how to request history, and BQL date syntax
     has several accepted forms.

Approach
--------
A ladder of queries from trivial to what we actually need. Each rung is only
attempted if the previous one succeeded, so the first failure tells you exactly
where the wall is rather than burying it in a generic error. Raw responses are
dumped so the response shape can be parsed properly in the provider.

Read-only. Makes no changes to any dashboard file.

Usage
-----
    python diagnose_bql.py
    python diagnose_bql.py --verbose          # dump full raw responses
    python diagnose_bql.py --outdir bql_probe
    python diagnose_bql.py --dry-run          # print the query ladder only
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from typing import Any, Dict, List, Optional

try:
    import blpapi  # type: ignore
    HAVE_BLPAPI = True
except Exception:  # noqa: BLE001
    blpapi = None  # type: ignore
    HAVE_BLPAPI = False


TODAY = dt.date.today().isoformat()
START = "2004-01-01"

# Securities we need COT for. ES1 Index already confirmed to resolve in round 1.
EQUITY_SEC = "ES1 Index"
RISK_BASKET = ["ES1 Index", "GC1 Comdty", "TY1 Comdty", "JY1 Curncy", "AD1 Curncy"]


# ---------------------------------------------------------------------------
# The query ladder
#
# Each rung: id, description, the BQL expression, and what a pass proves.
# Rungs are grouped; a group is abandoned once one of its variants passes,
# because the variants exist only to discover which syntax your build accepts.
# ---------------------------------------------------------------------------
def build_ladder() -> List[Dict[str, Any]]:
    cot_base = "report_type=cftc_legacy,crop_year=all"

    return [
        # -- Group 0: is BQL reachable at all -------------------------------
        {
            "id": "0.1_connectivity",
            "group": "connectivity",
            "desc": "Simplest possible BQL query.",
            "proves": "//blp/bqlsvc opens and accepts queries on this licence.",
            "expr": "get(px_last) for('SPX Index')",
        },
        {
            "id": "0.2_history",
            "group": "connectivity_history",
            "desc": "BQL with a relative date range.",
            "proves": "Date-range syntax form A works: range(-1Y, 0D).",
            "expr": "get(px_last(dates=range(-1Y,0D))) for('SPX Index')",
        },
        {
            "id": "0.3_history_abs",
            "group": "connectivity_history",
            "desc": "BQL with an absolute date range.",
            "proves": "Date-range syntax form B works: range('YYYY-MM-DD','YYYY-MM-DD').",
            "expr": f"get(px_last(dates=range('{START}','{TODAY}'))) for('SPX Index')",
        },

        # -- Group 1: does cot_position exist -------------------------------
        {
            "id": "1.1_cot_latest",
            "group": "cot_exists",
            "desc": "cot_position, latest value, non-commercial long, futures only.",
            "proves": "The COT function is available and ES1 Index is a valid COT subject.",
            "expr": (
                f"get(cot_position({cot_base},trader_type=non_commercial,"
                f"direction=long,commitment_type=futures)) for('{EQUITY_SEC}')"
            ),
        },
        {
            "id": "1.2_cot_latest_fo",
            "group": "cot_exists",
            "desc": "Same, futures and options combined.",
            "proves": "commitment_type=futures_and_options is accepted.",
            "expr": (
                f"get(cot_position({cot_base},trader_type=non_commercial,"
                f"direction=long,commitment_type=futures_and_options)) for('{EQUITY_SEC}')"
            ),
        },
        {
            "id": "1.3_cot_all_dims",
            "group": "cot_exists",
            "desc": "All directions and trader types, as in the COT <GO> example.",
            "proves": "The wide form returns a pivoted result we can filter client-side.",
            "expr": (
                f"get(cot_position({cot_base},trader_type=all,"
                f"direction=all,commitment_type=futures)) for('{EQUITY_SEC}')"
            ),
        },

        # -- Group 2: net non-commercial, which is the actual input ---------
        {
            "id": "2.1_net_let",
            "group": "cot_net",
            "desc": "Net non-commercial position via let(), latest value.",
            "proves": "Arithmetic across two cot_position calls works, giving HSBC input 18.",
            "expr": (
                "let("
                f"#long=cot_position({cot_base},trader_type=non_commercial,direction=long,commitment_type=futures_and_options);"
                f"#short=cot_position({cot_base},trader_type=non_commercial,direction=short,commitment_type=futures_and_options);"
                "#net=#long-#short)"
                f"get(#net) for('{EQUITY_SEC}')"
            ),
        },

        # -- Group 3: history, which is what percentile triggers need -------
        {
            "id": "3.1_net_hist_rel",
            "group": "cot_history",
            "desc": "Net non-commercial with a relative 20-year range.",
            "proves": "COT history is retrievable, form A. This is the query the provider will use.",
            "expr": (
                "let("
                f"#long=cot_position({cot_base},trader_type=non_commercial,direction=long,"
                "commitment_type=futures_and_options,dates=range(-20Y,0D));"
                f"#short=cot_position({cot_base},trader_type=non_commercial,direction=short,"
                "commitment_type=futures_and_options,dates=range(-20Y,0D));"
                "#net=#long-#short)"
                f"get(#net) for('{EQUITY_SEC}')"
            ),
        },
        {
            "id": "3.2_net_hist_abs",
            "group": "cot_history",
            "desc": "Net non-commercial with an absolute range.",
            "proves": "COT history is retrievable, form B.",
            "expr": (
                "let("
                f"#long=cot_position({cot_base},trader_type=non_commercial,direction=long,"
                f"commitment_type=futures_and_options,dates=range('{START}','{TODAY}'));"
                f"#short=cot_position({cot_base},trader_type=non_commercial,direction=short,"
                f"commitment_type=futures_and_options,dates=range('{START}','{TODAY}'));"
                "#net=#long-#short)"
                f"get(#net) for('{EQUITY_SEC}')"
            ),
        },
        {
            "id": "3.3_hist_outer_dates",
            "group": "cot_history",
            "desc": "Date range applied once at the get() level rather than per call.",
            "proves": "COT history is retrievable, form C. Cleaner if it works.",
            "expr": (
                "let("
                f"#long=cot_position({cot_base},trader_type=non_commercial,direction=long,commitment_type=futures_and_options);"
                f"#short=cot_position({cot_base},trader_type=non_commercial,direction=short,commitment_type=futures_and_options);"
                "#net=#long-#short)"
                f"get(#net(dates=range('{START}','{TODAY}'))) for('{EQUITY_SEC}')"
            ),
        },

        # -- Group 4: the risk-on/off basket, HSBC input 17 -----------------
        {
            "id": "4.1_basket",
            "group": "cot_basket",
            "desc": "Net non-commercial across the five risk-on/risk-off contracts.",
            "proves": "Multi-security COT in one request, giving HSBC input 17.",
            "expr": (
                "let("
                f"#long=cot_position({cot_base},trader_type=non_commercial,direction=long,commitment_type=futures_and_options);"
                f"#short=cot_position({cot_base},trader_type=non_commercial,direction=short,commitment_type=futures_and_options);"
                "#net=#long-#short)"
                "get(#net) for([" + ",".join(f"'{s}'" for s in RISK_BASKET) + "])"
            ),
        },

        # -- Group 5: open interest normalisation, optional but useful ------
        {
            "id": "5.1_traders",
            "group": "cot_traders",
            "desc": "cot_traders, number of reporting traders.",
            "proves": "The second COT function works, enabling a crowdedness measure.",
            "expr": (
                f"get(cot_traders({cot_base},trader_type=non_commercial,"
                f"direction=long,commitment_type=futures)) for('{EQUITY_SEC}')"
            ),
        },
        {
            "id": "5.2_open_interest",
            "group": "cot_normalise",
            "desc": "Total open interest, for scaling net position into a percentage.",
            "proves": "Net position can be normalised by open interest, which is the "
                      "more stable form of the input given contract size changes over 20 years.",
            "expr": (
                f"get(cot_position({cot_base},trader_type=all,direction=all,"
                f"commitment_type=futures_and_options)) for('{EQUITY_SEC}')"
            ),
        },
    ]


# ---------------------------------------------------------------------------
# blpapi plumbing
# ---------------------------------------------------------------------------
def element_to_py(el) -> Any:
    """Recursively convert a blpapi Element into plain Python.

    Deliberately generic: the BQL response schema is not something we should
    assume, so we convert whatever comes back and inspect it afterwards.
    """
    try:
        if el.isArray():
            return [element_to_py(el.getValueAsElement(i)) for i in range(el.numValues())]
    except Exception:  # noqa: BLE001
        pass
    try:
        if el.numElements() > 0:
            return {str(el.getElement(i).name()): element_to_py(el.getElement(i))
                    for i in range(el.numElements())}
    except Exception:  # noqa: BLE001
        pass
    for getter in ("getValueAsString", "getValueAsFloat", "getValueAsInteger"):
        try:
            return getattr(el, getter)()
        except Exception:  # noqa: BLE001
            continue
    try:
        return str(el)
    except Exception:  # noqa: BLE001
        return None


def extract_series(payload: Any) -> Optional[Dict[str, Any]]:
    """Best-effort pull of dates and values out of a BQL response.

    BQL results normally carry an idColumn, a valuesColumn, and secondaryColumns
    holding DATE and similar. We search for that shape anywhere in the tree
    rather than assuming a fixed path.
    """
    found: Dict[str, Any] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            keys = {k.lower() for k in node}
            if "valuescolumn" in keys:
                vals = None
                ids = None
                dates = None
                for k, v in node.items():
                    kl = k.lower()
                    if kl == "valuescolumn":
                        vals = _values_of(v)
                    elif kl == "idcolumn":
                        ids = _values_of(v)
                    elif kl == "secondarycolumns":
                        for col in (v if isinstance(v, list) else [v]):
                            if isinstance(col, dict):
                                nm = str(col.get("name", "")).upper()
                                if "DATE" in nm:
                                    dates = _values_of(col)
                if vals:
                    found.setdefault("values", vals)
                    found.setdefault("ids", ids)
                    found.setdefault("dates", dates)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(payload)
    if not found.get("values"):
        return None

    vals = found["values"]
    dates = found.get("dates") or []
    out = {
        "n_values": len(vals),
        "first_value": vals[0] if vals else None,
        "last_value": vals[-1] if vals else None,
        "first_date": dates[0] if dates else None,
        "last_date": dates[-1] if dates else None,
        "n_ids": len(found.get("ids") or []),
    }
    return out


def _values_of(node: Any) -> Optional[List[Any]]:
    if isinstance(node, dict):
        for k, v in node.items():
            if k.lower() in ("values", "value") and isinstance(v, list):
                return v
        for v in node.values():
            r = _values_of(v)
            if r:
                return r
    if isinstance(node, list):
        return node
    return None


class BqlSession:
    SERVICE = "//blp/bqlsvc"

    def __init__(self, host: str = "localhost", port: int = 8194, timeout_ms: int = 60000):
        self.host, self.port, self.timeout_ms = host, port, timeout_ms
        self._session = None
        self._svc = None
        self.service_error: Optional[str] = None

    def __enter__(self) -> "BqlSession":
        opts = blpapi.SessionOptions()
        opts.setServerHost(self.host)
        opts.setServerPort(self.port)
        self._session = blpapi.Session(opts)
        if not self._session.start():
            raise RuntimeError("Failed to start blpapi session. Is the Terminal running and logged in?")
        if not self._session.openService(self.SERVICE):
            # This is the single most important failure mode. Report it clearly
            # rather than letting it surface as a confusing query error.
            self.service_error = (
                f"Could not open {self.SERVICE}. BQL is not available on this Desktop API "
                "licence. COT data would then need the free CFTC CSV route instead."
            )
            return self
        self._svc = self._session.getService(self.SERVICE)
        return self

    def __exit__(self, *exc) -> None:
        if self._session is not None:
            try:
                self._session.stop()
            except Exception:  # noqa: BLE001
                pass

    def query(self, expression: str) -> Dict[str, Any]:
        if self._svc is None:
            return {"ok": False, "error": self.service_error or "service unavailable", "raw": None}

        # The request element name differs between builds; try the known forms.
        req = None
        for req_name in ("sendQuery", "SendQuery"):
            try:
                req = self._svc.createRequest(req_name)
                break
            except Exception:  # noqa: BLE001
                continue
        if req is None:
            return {"ok": False, "error": "Neither sendQuery nor SendQuery exists on the BQL service.", "raw": None}

        set_ok = False
        for field in ("expression", "query", "requestString"):
            try:
                req.set(field, expression)
                set_ok = True
                break
            except Exception:  # noqa: BLE001
                continue
        if not set_ok:
            return {"ok": False, "error": "Could not find the expression field on the BQL request.", "raw": None}

        try:
            self._session.sendRequest(req)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"sendRequest failed: {exc}", "raw": None}

        messages: List[Any] = []
        while True:
            ev = self._session.nextEvent(self.timeout_ms)
            for msg in ev:
                messages.append(element_to_py(msg.asElement()))
            if ev.eventType() == blpapi.Event.RESPONSE:
                break
            if ev.eventType() == blpapi.Event.TIMEOUT:
                return {"ok": False, "error": "BQL request timed out", "raw": None}

        blob = json.dumps(messages, default=str)
        lowered = blob.lower()
        # BQL returns errors inside a successful response envelope, so string
        # inspection is the reliable check here.
        for marker in ("responseerror", '"error"', "exception", "invalid", "syntax"):
            if marker in lowered:
                return {"ok": False, "error": f"Response contains '{marker}'", "raw": messages}

        summary = extract_series(messages)
        if summary is None:
            return {"ok": False, "error": "No values column found in response", "raw": messages}
        return {"ok": True, "error": None, "raw": messages, "summary": summary}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Probe BQL/COT availability for the sentiment indicator.")
    ap.add_argument("--outdir", default="bql_probe")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=8194)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verbose", action="store_true", help="Dump full raw responses to the report.")
    args = ap.parse_args()

    ladder = build_ladder()
    os.makedirs(args.outdir, exist_ok=True)

    print(f"BQL probe: {len(ladder)} queries in {len(set(r['group'] for r in ladder))} groups.\n")

    if args.dry_run or not HAVE_BLPAPI:
        if not args.dry_run:
            print("blpapi not importable, printing the ladder only.\n")
        for rung in ladder:
            print(f"[{rung['id']}] {rung['desc']}")
            print(f"    {rung['expr']}\n")
        return 0

    results: List[Dict[str, Any]] = []
    passed_groups: set = set()

    with BqlSession(args.host, args.port) as bql:
        if bql.service_error:
            print("FATAL: " + bql.service_error)
            with open(os.path.join(args.outdir, "bql_probe.md"), "w", encoding="utf-8") as fh:
                fh.write("# BQL probe\n\n**Service unavailable.**\n\n" + bql.service_error + "\n")
            return 1

        print(f"{BqlSession.SERVICE} opened.\n")

        for rung in ladder:
            # Skip remaining variants once a group has a winner.
            if rung["group"] in passed_groups:
                results.append({**rung, "ok": None, "error": "skipped, group already satisfied"})
                print(f"  [{rung['id']:<22}] skipped")
                continue

            res = bql.query(rung["expr"])
            entry = {
                **rung,
                "ok": res["ok"],
                "error": res.get("error"),
                "summary": res.get("summary"),
            }
            if args.verbose:
                entry["raw"] = res.get("raw")
            results.append(entry)

            if res["ok"]:
                passed_groups.add(rung["group"])
                s = res["summary"]
                print(f"  [{rung['id']:<22}] PASS  n={s['n_values']} "
                      f"{s.get('first_date')} to {s.get('last_date')} last={s.get('last_value')}")
            else:
                print(f"  [{rung['id']:<22}] FAIL  {res['error']}")

    # ---- report ----------------------------------------------------------
    lines = [
        "# BQL / COT probe",
        "",
        f"Run: {dt.datetime.now().strftime('%d %b %Y %H:%M')}",
        "",
        "## Outcome by group",
        "",
        "| Group | Satisfied | Winning query |",
        "|-------|-----------|---------------|",
    ]
    groups = []
    for r in results:
        if r["group"] not in [g[0] for g in groups]:
            groups.append((r["group"], None))
    winners = {}
    for r in results:
        if r.get("ok") and r["group"] not in winners:
            winners[r["group"]] = r["id"]
    for g, _ in groups:
        lines.append(f"| {g} | {'yes' if g in winners else 'NO'} | {winners.get(g, '-')} |")

    lines += ["", "## Query detail", ""]
    for r in results:
        state = "PASS" if r.get("ok") else ("skipped" if r.get("ok") is None else "FAIL")
        lines += [
            f"### {r['id']} — {state}",
            "",
            f"{r['desc']}",
            "",
            f"*Proves:* {r['proves']}" if r.get("proves") else "",
            "",
            "```",
            r["expr"],
            "```",
            "",
        ]
        if r.get("summary"):
            s = r["summary"]
            lines.append(
                f"Returned {s['n_values']} values across {s['n_ids']} ids, "
                f"{s.get('first_date')} to {s.get('last_date')}, last value {s.get('last_value')}."
            )
        if r.get("error"):
            lines.append(f"Error: `{r['error']}`")
        lines.append("")

    lines += [
        "## How to read this",
        "",
        "- If **connectivity** fails, BQL is not on this licence and COT must come",
        "  from the free CFTC CSV instead. Nothing below it will pass.",
        "- If **cot_exists** passes but **cot_history** fails, the data is there but",
        "  the date syntax is wrong. Send me the failing errors and I will adjust.",
        "- If **cot_history** passes, inputs 17 and 18 are recoverable and the",
        "  sentiment provider needs a second Bloomberg service alongside //blp/refdata.",
        "- **cot_normalise** passing is a bonus: scaling net position by total open",
        "  interest is more stable than raw contract counts over a twenty-year sample,",
        "  because contract sizes and market size both change materially.",
        "",
    ]

    md = os.path.join(args.outdir, "bql_probe.md")
    js = os.path.join(args.outdir, "bql_probe.json")
    with open(md, "w", encoding="utf-8") as fh:
        fh.write("\n".join(l for l in lines if l is not None))
    with open(js, "w", encoding="utf-8") as fh:
        json.dump({"run_at": dt.datetime.now().isoformat(timespec="seconds"), "results": results},
                  fh, indent=2, default=str)

    print(f"\nWrote:\n  {md}\n  {js}")
    return 0 if winners.get("cot_history") else 2


if __name__ == "__main__":
    sys.exit(main())
