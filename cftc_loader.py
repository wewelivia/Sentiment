#!/usr/bin/env python
"""
cftc_loader.py

Commitments of Traders loader for HSBC sentiment inputs 17 and 18.

Why this exists
---------------
Bloomberg exposes COT only through BQL, and BQL is not entitled on this licence
("User not authorized to use BQL"). The `bql` library is not an alternative: it
ships with BQuant Desktop, is not on PyPI, and its kernel cannot be called from
the FastAPI app. So COT comes from the CFTC directly.

That turns out to be the better source anyway. Verified against the live API on
11 August 2026:

  - Full weekly history back to 1997-09-16 for the E-mini S&P (1502
    observations), which is nine years deeper than the 2004 start we planned
    for on Bloomberg.
  - Free, no authentication, no rate limit that matters at this volume.
  - Carries open interest, so net position can be scaled rather than used raw.

Provider isolation
------------------
This deliberately does NOT live in providers/ alongside the Bloomberg
providers. It writes a parquet cache; the sentiment provider reads that cache
and never calls the network itself. So the Bloomberg path stays pure, and if
the CFTC feed is unreachable the dashboard degrades to a missing input rather
than failing.

Two things that will bite in a corporate environment
----------------------------------------------------
1. The proxy. requests picks up HTTP_PROXY / HTTPS_PROXY automatically, but
   Barclays may require explicit configuration. Run --probe first.
2. TLS interception. A MITM proxy presents its own certificate and requests
   will reject it. The correct fix is REQUESTS_CA_BUNDLE pointing at the
   corporate root CA. --insecure exists for diagnosis only and should never be
   left on in the scheduled path.

Usage
-----
    python cftc_loader.py --probe            # reachability only, no writes
    python cftc_loader.py --full             # first build, all history
    python cftc_loader.py --refresh          # incremental top-up (scheduled use)
    python cftc_loader.py --refresh --cache D:/devhome/data/cache/cftc

British spelling throughout.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from typing import Any, Dict, List, Optional

try:
    import requests
except ImportError:  # pragma: no cover
    sys.stderr.write("requests is required: pip install requests\n")
    raise

try:
    import pandas as pd
except ImportError:  # pragma: no cover
    sys.stderr.write("pandas is required\n")
    raise


# ---------------------------------------------------------------------------
# Endpoint and contract map. All five contract codes verified live against the
# API on 11 August 2026; the returned market names are recorded so the loader
# can assert it is still pulling what we think it is.
#
# Dataset: Legacy report, futures AND options combined. The futures-only
# dataset is 6dca-aqww if you ever want to compare. HSBC does not say which
# they use; combined is less distorted by option hedging.
# ---------------------------------------------------------------------------
BASE_URL = "https://publicreporting.cftc.gov/resource/jun7-fc8e.json"

CONTRACTS: Dict[str, Dict[str, Any]] = {
    "13874A": {
        "alias": "es_mini_sp500",
        "expect_name_contains": "E-MINI S&P 500",
        "bloomberg_equivalent": "ES1 Index",
        "risk_sign": +1,     # long equities = risk-on
        "in_equities_input": True,
        "in_risk_basket": True,
    },
    "088691": {
        "alias": "gold",
        "expect_name_contains": "GOLD",
        "bloomberg_equivalent": "GC1 Comdty",
        "risk_sign": -1,     # long gold = risk-off
        "in_equities_input": False,
        "in_risk_basket": True,
    },
    "043602": {
        "alias": "ust_10y",
        "expect_name_contains": "10Y NOTE",
        "bloomberg_equivalent": "TY1 Comdty",
        "risk_sign": -1,     # long duration = risk-off
        "in_equities_input": False,
        "in_risk_basket": True,
    },
    "097741": {
        "alias": "jpy",
        "expect_name_contains": "JAPANESE YEN",
        "bloomberg_equivalent": "JY1 Curncy",
        "risk_sign": -1,     # long yen = risk-off
        "in_equities_input": False,
        "in_risk_basket": True,
    },
    "232741": {
        "alias": "aud",
        "expect_name_contains": "AUSTRALIAN DOLLAR",
        "bloomberg_equivalent": "AD1 Curncy",
        "risk_sign": +1,     # long Aussie = risk-on
        "in_equities_input": False,
        "in_risk_basket": True,
    },
}

# Only the columns we actually need. Note two typos in the CFTC's own schema
# that a naive parser would silently miss: "postions" and "spead". We do not
# use those two, but they are recorded here as a warning to anyone extending
# this list.
COLUMNS = [
    "report_date_as_yyyy_mm_dd",
    "cftc_contract_market_code",
    "market_and_exchange_names",
    "open_interest_all",
    "noncomm_positions_long_all",
    "noncomm_positions_short_all",
    "traders_noncomm_long_all",
    "traders_noncomm_short_all",
]
CFTC_SCHEMA_TYPOS = ["noncomm_postions_spread_all", "change_in_noncomm_spead_all"]

# COT is measured as of Tuesday close and published the following Friday at
# 15:30 Eastern. Stamping observations at the Tuesday date would embed a
# three-day look-ahead into every back-test, so we carry both.
RELEASE_LAG_DAYS = 3

DEFAULT_CACHE = os.path.join(os.path.expanduser("~"), ".cache", "housview", "cftc")


# ---------------------------------------------------------------------------
def make_session(insecure: bool = False, timeout: int = 30) -> requests.Session:
    s = requests.Session()
    s.headers.update({"Accept": "application/json", "User-Agent": "HouseView-Sentiment/0.1"})
    if insecure:
        s.verify = False
        try:
            import urllib3
            urllib3.disable_warnings()
        except Exception:  # noqa: BLE001
            pass
    s.request_timeout = timeout  # type: ignore[attr-defined]
    return s


def fetch(session: requests.Session, params: Dict[str, str], timeout: int = 30) -> List[dict]:
    r = session.get(BASE_URL, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
def probe(session: requests.Session) -> int:
    """Reachability check. Answers the proxy question before anything is built."""
    print("Probing the CFTC public reporting API.\n")
    print(f"  HTTP_PROXY  = {os.environ.get('HTTP_PROXY') or os.environ.get('http_proxy') or '(unset)'}")
    print(f"  HTTPS_PROXY = {os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy') or '(unset)'}")
    print(f"  REQUESTS_CA_BUNDLE = {os.environ.get('REQUESTS_CA_BUNDLE') or '(unset)'}\n")

    try:
        rows = fetch(session, {"$limit": "1", "$select": "report_date_as_yyyy_mm_dd"})
    except requests.exceptions.SSLError as exc:
        print(f"  FAIL  TLS error: {exc}\n")
        print("  This is almost certainly the corporate TLS-interception proxy. Set")
        print("  REQUESTS_CA_BUNDLE to the Barclays root CA bundle. Do not reach for")
        print("  --insecure beyond confirming that this is the cause.")
        return 2
    except requests.exceptions.ProxyError as exc:
        print(f"  FAIL  Proxy error: {exc}\n")
        print("  Set HTTP_PROXY and HTTPS_PROXY, or configure them in the service account.")
        return 3
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL  {type(exc).__name__}: {exc}")
        return 4

    print(f"  PASS  API reachable, sample row {rows[0] if rows else '(empty)'}\n")

    # Confirm each contract still resolves and still means what we think.
    all_ok = True
    for code, meta in CONTRACTS.items():
        try:
            rows = fetch(session, {
                "$limit": "1",
                "$where": f"cftc_contract_market_code='{code}'",
                "$select": "market_and_exchange_names",
            })
            name = rows[0]["market_and_exchange_names"] if rows else ""
            ok = meta["expect_name_contains"] in name.upper()
            print(f"  {'PASS' if ok else 'FAIL'}  {code} {meta['alias']:<14} {name}")
            all_ok &= ok
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL  {code} {meta['alias']:<14} {type(exc).__name__}: {exc}")
            all_ok = False

    print("\nProbe " + ("passed." if all_ok else "FAILED on one or more contracts."))
    return 0 if all_ok else 5


# ---------------------------------------------------------------------------
def load_contract(session: requests.Session, code: str, since: Optional[str] = None) -> pd.DataFrame:
    """Pull one contract's full or incremental history."""
    where = f"cftc_contract_market_code='{code}'"
    if since:
        where += f" AND report_date_as_yyyy_mm_dd > '{since}'"

    frames: List[dict] = []
    offset = 0
    page = 50000  # Socrata maximum; the whole history fits in one page
    while True:
        rows = fetch(session, {
            "$limit": str(page),
            "$offset": str(offset),
            "$where": where,
            "$select": ",".join(COLUMNS),
            "$order": "report_date_as_yyyy_mm_dd ASC",
        })
        frames.extend(rows)
        if len(rows) < page:
            break
        offset += page

    if not frames:
        return pd.DataFrame()

    df = pd.DataFrame(frames)

    # Guard: the exchange name changed over the sample (International Monetary
    # Market became CME), which is exactly why we key on the contract code.
    # Assert the code still maps to the instrument we expect.
    expect = CONTRACTS[code]["expect_name_contains"]
    names = df["market_and_exchange_names"].str.upper()
    if not names.str.contains(expect, regex=False).any():
        raise ValueError(
            f"Contract {code} no longer matches '{expect}'. Observed: {sorted(set(names))[:3]}"
        )

    df["date"] = pd.to_datetime(df["report_date_as_yyyy_mm_dd"]).dt.date
    for col in ("open_interest_all", "noncomm_positions_long_all", "noncomm_positions_short_all",
                "traders_noncomm_long_all", "traders_noncomm_short_all"):
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["net"] = df["noncomm_positions_long_all"] - df["noncomm_positions_short_all"]
    # Scaling by open interest is the important bit. Raw contract counts are not
    # comparable across a 29-year sample in which E-mini open interest went from
    # tens of thousands to over three million.
    df["net_pct_oi"] = df["net"] / df["open_interest_all"].replace(0, pd.NA)
    df["traders_noncomm"] = df["traders_noncomm_long_all"] + df["traders_noncomm_short_all"]

    # Release-lag discipline: the value is not knowable until the Friday.
    df["knowable_from"] = pd.to_datetime(df["date"]) + pd.Timedelta(days=RELEASE_LAG_DAYS)
    df["knowable_from"] = df["knowable_from"].dt.date

    df["code"] = code
    df["alias"] = CONTRACTS[code]["alias"]

    out = df[["date", "knowable_from", "code", "alias", "net", "net_pct_oi",
              "open_interest_all", "noncomm_positions_long_all",
              "noncomm_positions_short_all", "traders_noncomm"]].copy()
    return out.sort_values("date").reset_index(drop=True)


# ---------------------------------------------------------------------------
def run(cache_dir: str, full: bool, session: requests.Session) -> int:
    os.makedirs(cache_dir, exist_ok=True)
    summary: Dict[str, Any] = {"run_at": dt.datetime.now().isoformat(timespec="seconds"),
                               "mode": "full" if full else "refresh", "contracts": {}}
    failures = 0

    for code, meta in CONTRACTS.items():
        path = os.path.join(cache_dir, f"cot_{meta['alias']}.parquet")
        existing: Optional[pd.DataFrame] = None
        since = None

        if not full and os.path.exists(path):
            try:
                existing = pd.read_parquet(path)
                if len(existing):
                    since = str(max(existing["date"]))
            except Exception as exc:  # noqa: BLE001
                print(f"  {meta['alias']}: cache unreadable ({exc}), rebuilding in full.")
                existing = None

        try:
            fresh = load_contract(session, code, since)
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL {meta['alias']:<14} {type(exc).__name__}: {exc}")
            summary["contracts"][meta["alias"]] = {"ok": False, "error": str(exc)}
            failures += 1
            continue

        if existing is not None and len(fresh):
            combined = pd.concat([existing, fresh], ignore_index=True)
            combined = combined.drop_duplicates(subset=["date", "code"], keep="last")
            combined = combined.sort_values("date").reset_index(drop=True)
        elif existing is not None:
            combined = existing
        else:
            combined = fresh

        if not len(combined):
            print(f"  WARN {meta['alias']:<14} no rows")
            summary["contracts"][meta["alias"]] = {"ok": False, "error": "no rows"}
            failures += 1
            continue

        combined.to_parquet(path, index=False)
        last = combined.iloc[-1]
        staleness = (dt.date.today() - last["date"]).days
        print(f"  OK   {meta['alias']:<14} {len(combined):>5} obs  "
              f"{combined.iloc[0]['date']} to {last['date']}  "
              f"(+{len(fresh)} new, {staleness}d old)  "
              f"net%OI={last['net_pct_oi']:+.4f}")
        summary["contracts"][meta["alias"]] = {
            "ok": True, "rows": int(len(combined)), "new_rows": int(len(fresh)),
            "first": str(combined.iloc[0]["date"]), "last": str(last["date"]),
            "staleness_days": int(staleness), "last_net_pct_oi": float(last["net_pct_oi"]),
            "path": path,
        }
        # COT is weekly; anything older than a fortnight means the feed stalled.
        if staleness > 14:
            print(f"       WARNING: {staleness} days since last report, feed may have stalled.")

    with open(os.path.join(cache_dir, "cot_summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, default=str)

    print(f"\nCache: {cache_dir}")
    return 0 if failures == 0 else 6


def main() -> int:
    ap = argparse.ArgumentParser(description="Load CFTC Commitments of Traders for the sentiment indicator.")
    ap.add_argument("--cache", default=DEFAULT_CACHE,
                    help="Cache directory. Keep it on a local unsynced path, not OneDrive.")
    ap.add_argument("--probe", action="store_true", help="Reachability check only, no writes.")
    ap.add_argument("--full", action="store_true", help="Rebuild all history from 1997.")
    ap.add_argument("--refresh", action="store_true", help="Incremental top-up.")
    ap.add_argument("--insecure", action="store_true",
                    help="Skip TLS verification. Diagnosis only; never leave this on.")
    ap.add_argument("--timeout", type=int, default=30)
    args = ap.parse_args()

    if args.insecure:
        print("WARNING: TLS verification disabled. Use this only to confirm that a")
        print("         MITM proxy is the problem, then fix it with REQUESTS_CA_BUNDLE.\n")

    session = make_session(insecure=args.insecure, timeout=args.timeout)

    if args.probe:
        return probe(session)
    if not (args.full or args.refresh):
        ap.error("Choose one of --probe, --full or --refresh.")

    print(f"CFTC COT loader, mode={'full' if args.full else 'refresh'}\n")
    return run(args.cache, args.full, session)


if __name__ == "__main__":
    sys.exit(main())
