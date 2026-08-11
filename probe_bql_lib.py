#!/usr/bin/env python
"""
probe_bql_lib.py

Second opinion on the BQL entitlement, via the native `bql` library rather than
raw blpapi.

Why
---
diagnose_bql.py reached //blp/bqlsvc through blpapi's sendQuery and got:

    Error: User not authorized to use BQL. Please contact customer service.

That is a server-side authorisation rejection, so the same user ID should be
refused whichever client reaches the service. But BQuant Desktop and Desktop API
are not always the same entitlement line, and the `bql` library can initialise a
different session type, so it is worth two minutes to rule out.

If this passes where diagnose_bql.py failed, the COT work is unblocked and the
provider should use `bql` rather than raw blpapi. If it fails the same way, the
entitlement request is confirmed as the only Bloomberg-side route.

Usage
-----
    python probe_bql_lib.py

Run it in the same interpreter you use for the dashboard. If `bql` is not
importable there, also try a BQuant Desktop console if you have one, since the
library ships with that environment rather than with blpapi.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import traceback

REPORT = {"run_at": dt.datetime.now().isoformat(timespec="seconds"), "steps": []}


def step(name: str, detail: str = "", ok: bool | None = None, error: str = "") -> None:
    REPORT["steps"].append({"step": name, "ok": ok, "detail": detail, "error": error})
    mark = "PASS" if ok else ("FAIL" if ok is False else "....")
    print(f"[{mark}] {name}" + (f"  {detail}" if detail else "") + (f"\n       {error}" if error else ""))


def main() -> int:
    # ---- 1. Is the library even present ---------------------------------
    try:
        import bql  # type: ignore
    except Exception as exc:  # noqa: BLE001
        step(
            "import bql",
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
        )
        print(
            "\nThe bql library is not installed in this interpreter. It ships with\n"
            "BQuant Desktop rather than with blpapi, so this is expected on a plain\n"
            "Miniconda environment and does not by itself tell us anything about\n"
            "entitlement. If you have a BQuant Desktop console, run this there."
        )
        _write()
        return 3

    step("import bql", detail=f"version {getattr(bql, '__version__', 'unknown')}", ok=True)

    # ---- 2. Can a service be constructed --------------------------------
    try:
        svc = bql.Service()
    except Exception as exc:  # noqa: BLE001
        step("bql.Service()", ok=False, error=f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=2)}")
        _write()
        return 1

    step("bql.Service()", ok=True)

    # ---- 3. Trivial query: does authorisation hold ----------------------
    try:
        req = bql.Request("SPX Index", {"px": svc.data.px_last()})
        res = svc.execute(req)
        df = res[0].df()
        step("trivial query px_last on SPX Index", detail=f"{len(df)} row(s), last={df.iloc[-1, -1]}", ok=True)
    except Exception as exc:  # noqa: BLE001
        msg = f"{type(exc).__name__}: {exc}"
        step("trivial query px_last on SPX Index", ok=False, error=msg)
        if "not authorized" in msg.lower() or "customer service" in msg.lower():
            print(
                "\nSame entitlement rejection as the blpapi route. That confirms the block\n"
                "is on the user ID rather than the client library, and the only\n"
                "Bloomberg-side route is to have BQL enabled on the licence."
            )
        _write()
        return 2

    # ---- 4. The query that actually matters: COT ------------------------
    # Only reached if authorisation holds, in which case the COT work is live.
    try:
        cot = svc.data.cot_position(
            report_type="cftc_legacy",
            trader_type="non_commercial",
            direction="long",
            commitment_type="futures_and_options",
            crop_year="all",
        )
        req = bql.Request("ES1 Index", {"nc_long": cot})
        res = svc.execute(req)
        df = res[0].df()
        step("cot_position on ES1 Index", detail=f"{len(df)} row(s)", ok=True)
        print("\nCOT is reachable. Send me the printed frame shape and I will build the")
        print("provider against the bql library rather than raw blpapi.")
        print(df.head(10).to_string())
    except Exception as exc:  # noqa: BLE001
        step("cot_position on ES1 Index", ok=False, error=f"{type(exc).__name__}: {exc}")
        print(
            "\nBQL itself works but the COT function does not. That is a narrower and\n"
            "better problem: it means the dataset is a separate entitlement from BQL,\n"
            "or the parameter names differ in this build. Send me the error text."
        )

    _write()
    return 0


def _write() -> None:
    with open("bql_lib_probe.json", "w", encoding="utf-8") as fh:
        json.dump(REPORT, fh, indent=2, default=str)
    print("\nWrote bql_lib_probe.json")


if __name__ == "__main__":
    sys.exit(main())
