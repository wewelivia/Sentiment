#!/usr/bin/env python
"""
diagnose_sentiment.py

Ticker and field validation for the HSBC-style sentiment indicator, to be run on
the Windows Bloomberg Terminal machine before any provider code is written.

Purpose
-------
sentiment_tickers.yaml contains CANDIDATE Bloomberg symbology for the 21 HSBC
inputs. Bloomberg symbology for survey, CFTC and fund-flow series is not
reliably documented outside the Terminal, and several fields (short interest,
MOVE, fund NAV) are separately entitled. This script resolves, for every
candidate, four questions:

  1. Does the security resolve at all?
  2. Is the requested field valid and entitled on this licence?
  3. How much history does it carry, and from when?
  4. Is it still being updated, and at what frequency?

It then reports, per input, whether at least one candidate is usable, so you
know before building which of the 21 inputs are actually reachable.

Design notes
------------
- Read-only. Makes no changes to any dashboard file.
- Degrades gracefully: with no blpapi installed it runs in --dry-run and prints
  the full test plan, so the config can be reviewed on the Mac.
- Writes two artefacts: a machine-readable JSON and a markdown report.
- Also emits sentiment_tickers.resolved.yaml containing only the candidates that
  passed, ready to promote into the main config.

Usage
-----
    python diagnose_sentiment.py
    python diagnose_sentiment.py --config sentiment_tickers.yaml --outdir .
    python diagnose_sentiment.py --dry-run          # no Terminal needed
    python diagnose_sentiment.py --only vix_curve,move,average_survey_sentiment

British spelling throughout, consistent with the rest of the project.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import statistics
import sys
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.stderr.write("PyYAML is required: pip install pyyaml\n")
    raise

try:
    import blpapi  # type: ignore
    HAVE_BLPAPI = True
except Exception:  # noqa: BLE001 - any import failure means no Terminal path
    blpapi = None  # type: ignore
    HAVE_BLPAPI = False


# ---------------------------------------------------------------------------
# Status vocabulary
# ---------------------------------------------------------------------------
OK = "OK"
OK_SHORT_HISTORY = "OK_SHORT_HISTORY"
STALE = "STALE"
NO_DATA = "NO_DATA"
FIELD_DENIED = "FIELD_DENIED"
FIELD_INVALID = "FIELD_INVALID"
INVALID_TICKER = "INVALID_TICKER"
ERROR = "ERROR"

USABLE = {OK, OK_SHORT_HISTORY}


# ---------------------------------------------------------------------------
# Config traversal
# ---------------------------------------------------------------------------
def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def build_test_plan(cfg: dict, only: Optional[List[str]] = None) -> List[dict]:
    """Flatten the config into one test per (input, role, candidate, field)."""
    defaults = cfg.get("defaults", {})
    default_field = defaults.get("field", "PX_LAST")
    plan: List[dict] = []

    for inp in cfg.get("inputs", []):
        input_id = inp["id"]
        if only and input_id not in only:
            continue

        # Field precedence: series-level > input-level > global default.
        input_field = inp.get("field", default_field)
        input_alts = inp.get("field_alternates", []) or []

        for series in inp.get("series", []) or []:
            role = series.get("role", "primary")
            field = series.get("field", input_field)
            alternates = series.get("field_alternates", input_alts) or []
            # Basket price/weight fields are tested as separate entries.
            extra_fields = []
            if series.get("price_field"):
                field = series["price_field"]
                if series.get("weight_field"):
                    extra_fields.append(series["weight_field"])

            for candidate in series.get("candidates", []) or []:
                plan.append(
                    {
                        "input_id": input_id,
                        "input_name": inp.get("name", input_id),
                        "kind": inp.get("kind", "direct"),
                        "role": role,
                        "ticker": candidate,
                        "field": field,
                        "field_alternates": list(alternates),
                        "extra_fields": extra_fields,
                        "frequency": inp.get("frequency", "daily"),
                        "resolve_via_terminal": series.get("resolve_via_terminal"),
                    }
                )
    return plan


# ---------------------------------------------------------------------------
# Bloomberg session
# ---------------------------------------------------------------------------
class BloombergSession:
    """Thin blpapi wrapper. Mirrors the conventions in blpapi_provider.py."""

    def __init__(self, host: str = "localhost", port: int = 8194, timeout_ms: int = 30000):
        self.host = host
        self.port = port
        self.timeout_ms = timeout_ms
        self._session = None
        self._svc = None

    def __enter__(self) -> "BloombergSession":
        opts = blpapi.SessionOptions()
        opts.setServerHost(self.host)
        opts.setServerPort(self.port)
        self._session = blpapi.Session(opts)
        if not self._session.start():
            raise RuntimeError("Failed to start blpapi session. Is the Terminal running and logged in?")
        if not self._session.openService("//blp/refdata"):
            raise RuntimeError("Failed to open //blp/refdata")
        self._svc = self._session.getService("//blp/refdata")
        return self

    def __exit__(self, *exc) -> None:
        if self._session is not None:
            try:
                self._session.stop()
            except Exception:  # noqa: BLE001
                pass

    # -- request plumbing ---------------------------------------------------
    def _send(self, request) -> List[Any]:
        """Send a request and collect every message until the response ends."""
        self._session.sendRequest(request)
        messages: List[Any] = []
        while True:
            ev = self._session.nextEvent(self.timeout_ms)
            for msg in ev:
                messages.append(msg)
            if ev.eventType() == blpapi.Event.RESPONSE:
                break
            if ev.eventType() == blpapi.Event.TIMEOUT:
                raise TimeoutError("blpapi request timed out")
        return messages

    def reference(self, securities: List[str], fields: List[str]) -> Dict[str, dict]:
        """ReferenceDataRequest. Returns {ticker: {...}} including error detail."""
        out: Dict[str, dict] = {}
        req = self._svc.createRequest("ReferenceDataRequest")
        for sec in securities:
            req.getElement("securities").appendValue(sec)
        for fld in fields:
            req.getElement("fields").appendValue(fld)

        for msg in self._send(req):
            if not msg.hasElement("securityData"):
                continue
            arr = msg.getElement("securityData")
            for i in range(arr.numValues()):
                sd = arr.getValueAsElement(i)
                ticker = sd.getElementAsString("security")
                entry: Dict[str, Any] = {"security_error": None, "field_exceptions": {}, "fields": {}}

                if sd.hasElement("securityError"):
                    se = sd.getElement("securityError")
                    entry["security_error"] = {
                        "category": _s(se, "category"),
                        "message": _s(se, "message"),
                        "subcategory": _s(se, "subcategory"),
                    }

                if sd.hasElement("fieldExceptions"):
                    fx = sd.getElement("fieldExceptions")
                    for j in range(fx.numValues()):
                        fe = fx.getValueAsElement(j)
                        fid = fe.getElementAsString("fieldId")
                        info = fe.getElement("errorInfo")
                        entry["field_exceptions"][fid] = {
                            "category": _s(info, "category"),
                            "message": _s(info, "message"),
                            "subcategory": _s(info, "subcategory"),
                        }

                if sd.hasElement("fieldData"):
                    fd = sd.getElement("fieldData")
                    for j in range(fd.numElements()):
                        el = fd.getElement(j)
                        try:
                            entry["fields"][str(el.name())] = el.getValueAsString()
                        except Exception:  # noqa: BLE001
                            entry["fields"][str(el.name())] = None

                out[ticker] = entry
        return out

    def history(
        self,
        security: str,
        field: str,
        start: str,
        end: str,
        periodicity: str = "DAILY",
    ) -> Tuple[List[Tuple[str, float]], Optional[dict]]:
        """HistoricalDataRequest for one security/field. Returns (points, error)."""
        req = self._svc.createRequest("HistoricalDataRequest")
        req.getElement("securities").appendValue(security)
        req.getElement("fields").appendValue(field)
        req.set("startDate", start)
        req.set("endDate", end)
        req.set("periodicitySelection", periodicity)
        req.set("nonTradingDayFillOption", "ACTIVE_DAYS_ONLY")

        points: List[Tuple[str, float]] = []
        error: Optional[dict] = None

        for msg in self._send(req):
            if not msg.hasElement("securityData"):
                continue
            sd = msg.getElement("securityData")

            if sd.hasElement("securityError"):
                se = sd.getElement("securityError")
                error = {
                    "kind": "security",
                    "category": _s(se, "category"),
                    "message": _s(se, "message"),
                }
                continue

            if sd.hasElement("fieldExceptions") and sd.getElement("fieldExceptions").numValues() > 0:
                fe = sd.getElement("fieldExceptions").getValueAsElement(0)
                info = fe.getElement("errorInfo")
                error = {
                    "kind": "field",
                    "category": _s(info, "category"),
                    "message": _s(info, "message"),
                }

            if sd.hasElement("fieldData"):
                fd = sd.getElement("fieldData")
                for i in range(fd.numValues()):
                    row = fd.getValueAsElement(i)
                    if not row.hasElement(field):
                        continue
                    d = row.getElementAsDatetime("date")
                    try:
                        v = row.getElementAsFloat(field)
                    except Exception:  # noqa: BLE001
                        continue
                    points.append((dt.date(d.year, d.month, d.day).isoformat(), v))

        points.sort(key=lambda p: p[0])
        return points, error


def _s(element, name: str) -> Optional[str]:
    try:
        return element.getElementAsString(name)
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Assessment
# ---------------------------------------------------------------------------
def classify(
    points: List[Tuple[str, float]],
    hist_error: Optional[dict],
    ref_entry: Optional[dict],
    field: str,
    frequency: str,
    min_history_years: float,
    max_staleness: Dict[str, int],
    today: dt.date,
) -> Dict[str, Any]:
    """Turn raw request outcomes into a single status plus supporting metrics."""
    result: Dict[str, Any] = {
        "status": ERROR,
        "detail": None,
        "points": len(points),
        "first_date": points[0][0] if points else None,
        "last_date": points[-1][0] if points else None,
        "history_years": None,
        "staleness_days": None,
        "median_gap_days": None,
        "inferred_frequency": None,
        "last_value": points[-1][1] if points else None,
    }

    # Security-level failure takes precedence.
    if ref_entry and ref_entry.get("security_error"):
        result["status"] = INVALID_TICKER
        result["detail"] = ref_entry["security_error"].get("message")
        return result

    if hist_error:
        cat = (hist_error.get("category") or "").upper()
        msg = hist_error.get("message")
        if hist_error.get("kind") == "security":
            result["status"] = INVALID_TICKER
            result["detail"] = msg
            return result
        # Field-level failure: distinguish entitlement from bad field name.
        if "AUTH" in cat or "ENTITL" in cat or "PERMISSION" in cat:
            result["status"] = FIELD_DENIED
        else:
            result["status"] = FIELD_INVALID
        result["detail"] = f"{cat}: {msg}"
        if not points:
            return result

    # A field exception on the reference call is a useful secondary signal.
    if not points and ref_entry and field in (ref_entry.get("field_exceptions") or {}):
        fx = ref_entry["field_exceptions"][field]
        cat = (fx.get("category") or "").upper()
        result["status"] = FIELD_DENIED if ("AUTH" in cat or "ENTITL" in cat) else FIELD_INVALID
        result["detail"] = f"{cat}: {fx.get('message')}"
        return result

    if not points:
        result["status"] = NO_DATA
        result["detail"] = result["detail"] or "Ticker resolved but returned no observations for the field."
        return result

    first = dt.date.fromisoformat(points[0][0])
    last = dt.date.fromisoformat(points[-1][0])
    result["history_years"] = round((last - first).days / 365.25, 2)
    result["staleness_days"] = (today - last).days

    if len(points) > 5:
        gaps = [
            (dt.date.fromisoformat(points[i + 1][0]) - dt.date.fromisoformat(points[i][0])).days
            for i in range(len(points) - 1)
        ]
        med = statistics.median(gaps)
        result["median_gap_days"] = med
        if med <= 2:
            result["inferred_frequency"] = "daily"
        elif med <= 10:
            result["inferred_frequency"] = "weekly"
        elif med <= 45:
            result["inferred_frequency"] = "monthly"
        else:
            result["inferred_frequency"] = "quarterly_or_sparser"

    freq_key = result["inferred_frequency"] or frequency
    if freq_key not in max_staleness:
        freq_key = "daily" if freq_key == "daily" else "monthly"
    limit = max_staleness.get(freq_key, 60)

    if result["staleness_days"] > limit:
        result["status"] = STALE
        result["detail"] = f"Last observation {result['staleness_days']}d old, limit {limit}d for {freq_key}."
    elif result["history_years"] < min_history_years:
        result["status"] = OK_SHORT_HISTORY
        result["detail"] = (
            f"Only {result['history_years']}y of history, below the {min_history_years}y minimum. "
            "Expanding-window percentiles will be unstable early in the sample."
        )
    else:
        result["status"] = OK

    return result


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def summarise_by_input(results: List[dict], dry_run: bool = False) -> "OrderedDict[str, dict]":
    """Roll candidate-level results up to a per-input verdict."""
    by_input: "OrderedDict[str, dict]" = OrderedDict()
    for r in results:
        iid = r["input_id"]
        entry = by_input.setdefault(
            iid,
            {
                "input_id": iid,
                "input_name": r["input_name"],
                "kind": r["kind"],
                "roles": OrderedDict(),
            },
        )
        role_entry = entry["roles"].setdefault(r["role"], {"candidates": [], "resolved": None})
        role_entry["candidates"].append(r)
        if role_entry["resolved"] is None and r["status"] in USABLE:
            role_entry["resolved"] = r["ticker"]

    for entry in by_input.values():
        roles = entry["roles"]
        if dry_run:
            for rd in roles.values():
                rd.setdefault("resolved_basket", None)
            entry["verdict"] = "NOT_TESTED"
            continue
        # A basket role only needs some of its members; single roles need one.
        satisfied = []
        for role, rd in roles.items():
            if role.endswith("basket") or role == "basket":
                usable = [c for c in rd["candidates"] if c["status"] in USABLE]
                rd["resolved_basket"] = [c["ticker"] for c in usable]
                satisfied.append(len(usable) >= 2)
            else:
                satisfied.append(rd["resolved"] is not None)
        if all(satisfied):
            entry["verdict"] = "BUILDABLE"
        elif any(satisfied):
            entry["verdict"] = "PARTIAL"
        else:
            entry["verdict"] = "BLOCKED"
    return by_input


def write_markdown(by_input: dict, results: List[dict], outpath: str, dry_run: bool) -> None:
    total = len(by_input)
    buildable = sum(1 for e in by_input.values() if e["verdict"] == "BUILDABLE")
    partial = sum(1 for e in by_input.values() if e["verdict"] == "PARTIAL")
    blocked = sum(1 for e in by_input.values() if e["verdict"] == "BLOCKED")

    lines = [
        "# Sentiment indicator: Bloomberg ticker diagnostic",
        "",
        f"Run: {dt.datetime.now().strftime('%d %b %Y %H:%M')}"
        + ("  \n**DRY RUN - no Terminal connection, statuses are placeholders.**" if dry_run else ""),
        "",
        "## Summary",
        "",
        f"- Inputs tested: **{total}**",
        f"- Buildable (every role resolved): **{buildable}**",
        f"- Partial (some roles resolved): **{partial}**",
        f"- Blocked (nothing resolved): **{blocked}**",
        "",
        "A buildable count materially below 21 is expected. The question that matters",
        "is whether the buildable set spans enough distinct clusters for the aggregate",
        "to be meaningful, not whether every HSBC input survives.",
        "",
        "## Per-input verdicts",
        "",
        "| # | Input | Kind | Verdict | Resolved tickers |",
        "|---|-------|------|---------|------------------|",
    ]

    for i, entry in enumerate(by_input.values(), start=1):
        resolved: List[str] = []
        for role, rd in entry["roles"].items():
            if rd.get("resolved_basket"):
                resolved.append(f"{role}: {', '.join(rd['resolved_basket'][:4])}")
            elif rd.get("resolved"):
                resolved.append(f"{role}: {rd['resolved']}")
        lines.append(
            f"| {i} | {entry['input_name']} | {entry['kind']} | **{entry['verdict']}** | "
            f"{'; '.join(resolved) if resolved else '-'} |"
        )

    lines += ["", "## Candidate detail", ""]
    lines.append("| Input | Role | Ticker | Field | Status | Points | From | To | Stale (d) | Freq | Detail |")
    lines.append("|-------|------|--------|-------|--------|--------|------|----|-----------|------|--------|")
    for r in results:
        detail = (r.get("detail") or "")[:90].replace("|", "/")
        lines.append(
            f"| {r['input_id']} | {r['role']} | `{r['ticker']}` | `{r['field']}` | {r['status']} | "
            f"{r.get('points', 0)} | {r.get('first_date') or '-'} | {r.get('last_date') or '-'} | "
            f"{r.get('staleness_days') if r.get('staleness_days') is not None else '-'} | "
            f"{r.get('inferred_frequency') or '-'} | {detail} |"
        )

    lines += [
        "",
        "## What to do with blocked inputs",
        "",
        "1. For CFTC series, open `CFTC <GO>` on the Terminal, find the exact security",
        "   for the E-mini S&P non-commercial net position, and paste it into",
        "   `sentiment_tickers.yaml` as the first candidate.",
        "2. For short interest and fund NAV fields, a `FIELD_DENIED` status is an",
        "   entitlement question for your Bloomberg contract, not a code problem.",
        "3. For survey series that do not resolve, AAII and NAAIM publish free weekly",
        "   data that can be loaded outside blpapi into the same cache.",
        "4. Re-run this script after each config change. It is cheap and read-only.",
        "",
    ]

    with open(outpath, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def write_resolved_yaml(by_input: dict, outpath: str) -> None:
    """Emit only what passed, ready to promote into the main config."""
    resolved = {"resolved": {}}
    for iid, entry in by_input.items():
        roles = {}
        for role, rd in entry["roles"].items():
            if rd.get("resolved_basket"):
                roles[role] = rd["resolved_basket"]
            elif rd.get("resolved"):
                roles[role] = rd["resolved"]
        resolved["resolved"][iid] = {"verdict": entry["verdict"], "tickers": roles}
    with open(outpath, "w", encoding="utf-8") as fh:
        yaml.safe_dump(resolved, fh, sort_keys=False, default_flow_style=False)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Validate Bloomberg symbology for the sentiment indicator.")
    ap.add_argument("--config", default="sentiment_tickers.yaml")
    ap.add_argument("--outdir", default=".")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=8194)
    ap.add_argument("--dry-run", action="store_true", help="Print the test plan without contacting Bloomberg.")
    ap.add_argument("--only", default=None, help="Comma-separated input ids to test.")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.config):
        sys.stderr.write(f"Config not found: {args.config}\n")
        return 2

    cfg = load_config(args.config)
    only = [s.strip() for s in args.only.split(",")] if args.only else None
    plan = build_test_plan(cfg, only)

    defaults = cfg.get("defaults", {})
    start = defaults.get("history_start", "2004-01-01").replace("-", "")
    end = dt.date.today().strftime("%Y%m%d")
    min_hist = float(defaults.get("min_history_years", 5))
    max_stale = defaults.get("max_staleness_days", {"daily": 7, "weekly": 21, "monthly": 60})
    today = dt.date.today()

    unique_tickers = sorted({t["ticker"] for t in plan})
    print(f"Test plan: {len(plan)} candidate checks across {len(unique_tickers)} unique securities.")
    print(f"History window: {start} to {end}")

    dry = args.dry_run or not HAVE_BLPAPI
    if dry and not args.dry_run:
        print("\nblpapi not importable - falling back to dry run. Run this on the Terminal machine for real results.\n")

    results: List[dict] = []

    if dry:
        for t in plan:
            r = dict(t)
            r.update(
                {
                    "status": "NOT_TESTED",
                    "detail": "Dry run" + (f"; resolve via {t['resolve_via_terminal']}" if t.get("resolve_via_terminal") else ""),
                    "points": 0,
                    "first_date": None,
                    "last_date": None,
                    "history_years": None,
                    "staleness_days": None,
                    "median_gap_days": None,
                    "inferred_frequency": None,
                    "last_value": None,
                }
            )
            results.append(r)
            if args.verbose:
                print(f"  would test {t['ticker']:<28} {t['field']:<24} ({t['input_id']}/{t['role']})")
    else:
        with BloombergSession(args.host, args.port) as bb:
            # One batched reference call resolves names and surfaces bad tickers
            # cheaply before the heavier historical loop.
            print("Resolving securities via ReferenceDataRequest...")
            ref: Dict[str, dict] = {}
            batch = 50
            for i in range(0, len(unique_tickers), batch):
                chunk = unique_tickers[i : i + batch]
                try:
                    ref.update(bb.reference(chunk, ["NAME", "SECURITY_TYP", "CRNCY", "LAST_UPDATE_DT"]))
                except Exception as exc:  # noqa: BLE001
                    sys.stderr.write(f"Reference batch failed: {exc}\n")

            print("Pulling history per candidate...")
            for n, t in enumerate(plan, start=1):
                fields_to_try = [t["field"]] + [f for f in t["field_alternates"] if f != t["field"]]
                best: Optional[dict] = None
                for field in fields_to_try:
                    try:
                        pts, err = bb.history(t["ticker"], field, start, end)
                    except Exception as exc:  # noqa: BLE001
                        pts, err = [], {"kind": "field", "category": "EXCEPTION", "message": str(exc)}
                    assessed = classify(
                        pts, err, ref.get(t["ticker"]), field, t["frequency"], min_hist, max_stale, today
                    )
                    assessed["field"] = field
                    if best is None or (assessed["status"] in USABLE and best["status"] not in USABLE):
                        best = assessed
                    if assessed["status"] in USABLE:
                        break

                r = dict(t)
                r.update(best or {})
                r["bbg_name"] = (ref.get(t["ticker"], {}).get("fields", {}) or {}).get("NAME")
                results.append(r)
                print(f"  [{n}/{len(plan)}] {t['ticker']:<28} {r['field']:<24} -> {r['status']}")

    by_input = summarise_by_input(results, dry_run=dry)

    os.makedirs(args.outdir, exist_ok=True)
    json_path = os.path.join(args.outdir, "sentiment_diagnostic.json")
    md_path = os.path.join(args.outdir, "sentiment_diagnostic.md")
    resolved_path = os.path.join(args.outdir, "sentiment_tickers.resolved.yaml")

    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "run_at": dt.datetime.now().isoformat(timespec="seconds"),
                "dry_run": dry,
                "config": os.path.abspath(args.config),
                "candidates": results,
                "by_input": {k: {kk: vv for kk, vv in v.items() if kk != "roles"} | {
                    "roles": {r: {"resolved": d.get("resolved"), "resolved_basket": d.get("resolved_basket")}
                              for r, d in v["roles"].items()}
                } for k, v in by_input.items()},
            },
            fh,
            indent=2,
            default=str,
        )

    write_markdown(by_input, results, md_path, dry)
    write_resolved_yaml(by_input, resolved_path)

    print("\n--- Verdict ---")
    for entry in by_input.values():
        print(f"  {entry['verdict']:<10} {entry['input_id']}")
    print(f"\nWrote:\n  {md_path}\n  {json_path}\n  {resolved_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
