#!/usr/bin/env python
"""
find_proxy.py

Discover the corporate proxy on the Barclays Windows machine, then test each
candidate against the CFTC API.

Why
---
cftc_loader.py --probe failed with WinError 10061 and both proxy variables
unset, meaning requests attempted a direct connection and the network refused
it. But REQUESTS_CA_BUNDLE is set to a corporate CA bundle, which only makes
sense if a TLS-intercepting proxy exists. It does; this shell just doesn't
know about it.

Other tools on this machine already reach the internet. pip installed packages
into julien_dev, and the browser reaches external sites. Whatever proxy they
use is recorded somewhere, and this script reads the usual places rather than
guessing.

What it checks, in order of reliability:
  1. Existing environment variables (all the casing variants).
  2. pip configuration, since pip demonstrably works here.
  3. conda configuration.
  4. WinHTTP proxy, via netsh.
  5. WinINET proxy, from the registry, which is what the browser uses.
  6. The PAC file, if AutoConfigURL is set, parsed for PROXY directives.

Then it tries each distinct candidate against the CFTC endpoint and reports
which one works, with a ready-to-paste command.

Read-only. Changes no settings and writes no configuration.

Usage
-----
    python find_proxy.py
    python find_proxy.py --target https://publicreporting.cftc.gov/resource/jun7-fc8e.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from typing import Dict, List, Optional, Tuple

try:
    import requests
except ImportError:  # pragma: no cover
    sys.stderr.write("requests is required\n")
    raise

DEFAULT_TARGET = "https://publicreporting.cftc.gov/resource/jun7-fc8e.json?$limit=1"

Candidate = Tuple[str, str]  # (proxy_url, where_it_came_from)


def _norm(proxy: str) -> Optional[str]:
    """Normalise a proxy string into a usable URL."""
    if not proxy:
        return None
    p = proxy.strip().strip('"').strip("'")
    if not p or p.lower() in ("(unset)", "none", "direct"):
        return None
    # WinINET can list per-protocol entries: "http=host:80;https=host:80"
    if "=" in p:
        parts = dict(
            kv.split("=", 1) for kv in p.split(";") if "=" in kv
        )
        p = parts.get("https") or parts.get("http") or next(iter(parts.values()), "")
        p = p.strip()
    if not p:
        return None
    # Preserve any explicit scheme. SOCKS in particular must not be prefixed
    # with http://, which silently produces the nonsense URL
    # "http://socks5h://host:1080" and a misleading connection error.
    if "://" in p:
        return p.rstrip("/")
    return "http://" + p.rstrip("/")


# ---------------------------------------------------------------------------
# Discovery sources
# ---------------------------------------------------------------------------
def from_env() -> List[Candidate]:
    out = []
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        v = _norm(os.environ.get(var, ""))
        if v:
            out.append((v, f"environment variable {var}"))
    return out


def from_pip() -> List[Candidate]:
    out = []
    try:
        r = subprocess.run([sys.executable, "-m", "pip", "config", "list"],
                           capture_output=True, text=True, timeout=30)
        for line in (r.stdout or "").splitlines():
            if "proxy" in line.lower() and "=" in line:
                v = _norm(line.split("=", 1)[1])
                if v:
                    out.append((v, f"pip config ({line.strip()})"))
    except Exception:  # noqa: BLE001
        pass

    # pip.ini is often set even when pip config list does not surface it.
    for path in (
        os.path.join(os.environ.get("APPDATA", ""), "pip", "pip.ini"),
        os.path.join(os.path.expanduser("~"), "pip", "pip.ini"),
        os.path.join(os.path.expanduser("~"), ".pip", "pip.conf"),
    ):
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                    for line in fh:
                        if line.strip().lower().startswith("proxy"):
                            v = _norm(line.split("=", 1)[-1])
                            if v:
                                out.append((v, f"{path}"))
            except Exception:  # noqa: BLE001
                pass
    return out


def from_conda() -> List[Candidate]:
    out = []
    for path in (
        os.path.join(os.path.expanduser("~"), ".condarc"),
        os.path.join(os.environ.get("CONDA_PREFIX", ""), ".condarc"),
    ):
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                    text = fh.read()
                for m in re.finditer(r"(?:http|https):\s*(\S+)", text):
                    v = _norm(m.group(1))
                    if v:
                        out.append((v, f"{path}"))
            except Exception:  # noqa: BLE001
                pass
    return out


def from_netsh() -> List[Candidate]:
    out = []
    try:
        r = subprocess.run(["netsh", "winhttp", "show", "proxy"],
                           capture_output=True, text=True, timeout=30)
        text = r.stdout or ""
        m = re.search(r"Proxy Server\(s\)\s*:\s*(\S+)", text)
        if m:
            v = _norm(m.group(1))
            if v:
                out.append((v, "netsh winhttp show proxy"))
    except Exception:  # noqa: BLE001
        pass
    return out


def from_registry() -> Tuple[List[Candidate], Optional[str]]:
    """WinINET settings. This is what the browser uses, so it is the most
    likely to be correct on a managed machine."""
    out: List[Candidate] = []
    pac: Optional[str] = None
    try:
        import winreg  # type: ignore
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        )
        try:
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
            v = _norm(str(server))
            if v:
                out.append((v, "registry ProxyServer (WinINET, browser setting)"))
        except FileNotFoundError:
            pass
        try:
            auto, _ = winreg.QueryValueEx(key, "AutoConfigURL")
            pac = str(auto).strip() or None
        except FileNotFoundError:
            pass
        winreg.CloseKey(key)
    except Exception:  # noqa: BLE001
        pass
    return out, pac


def from_pac(pac_url: str) -> List[Candidate]:
    """Fetch and scrape the PAC script for PROXY directives.

    Not a PAC interpreter. PAC files are JavaScript and can route by hostname,
    so a scraped entry may be wrong for this particular target. It is still the
    best available hint and the connection test below is the real arbiter.
    """
    out: List[Candidate] = []
    try:
        r = requests.get(pac_url, timeout=20, proxies={"http": None, "https": None})
        r.raise_for_status()
        seen = set()
        for m in re.finditer(r"PROXY\s+([A-Za-z0-9_.\-]+:\d+)", r.text):
            v = _norm(m.group(1))
            if v and v not in seen:
                seen.add(v)
                out.append((v, f"PAC file {pac_url}"))
    except Exception as exc:  # noqa: BLE001
        print(f"    could not fetch PAC ({type(exc).__name__}: {exc})")
    return out


# ---------------------------------------------------------------------------
def test(proxy: Optional[str], target: str, timeout: int = 25) -> Tuple[bool, str]:
    # trust_env=False is essential. Passing proxies={"http": None} does NOT stop
    # requests reading proxy settings from the environment, so the "direct" test
    # would silently go through whatever proxy the shell already has set and
    # report a misleading result.
    session = requests.Session()
    session.trust_env = False
    proxies = {"http": proxy, "https": proxy} if proxy else {}
    try:
        r = session.get(target, proxies=proxies, timeout=timeout)
        if r.status_code == 200:
            body = r.text[:80].replace("\n", " ")
            return True, f"HTTP 200, body starts: {body}"
        return False, f"HTTP {r.status_code}"
    except requests.exceptions.SSLError as exc:
        return False, f"TLS error (set REQUESTS_CA_BUNDLE): {str(exc)[:120]}"
    except requests.exceptions.ProxyError as exc:
        return False, f"proxy refused: {str(exc)[:120]}"
    except requests.exceptions.ConnectTimeout:
        return False, "connect timeout"
    except requests.exceptions.InvalidSchema as exc:
        # Almost always a SOCKS proxy without the extra dependency installed.
        return False, f"{exc}  (for SOCKS: pip install requests[socks])"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {str(exc)[:120]}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Find the corporate proxy and test it against the CFTC API.")
    ap.add_argument("--target", default=DEFAULT_TARGET)
    ap.add_argument("--timeout", type=int, default=25)
    args = ap.parse_args()

    print("Discovering proxy configuration.\n")
    print(f"  REQUESTS_CA_BUNDLE = {os.environ.get('REQUESTS_CA_BUNDLE') or '(unset)'}")
    if not os.environ.get("REQUESTS_CA_BUNDLE"):
        print("    Warning: unset. With a TLS-intercepting proxy you will get certificate")
        print("    errors even once the proxy address is right.")
    print()

    candidates: List[Candidate] = []
    for label, fn in (("environment", from_env), ("pip", from_pip),
                      ("conda", from_conda), ("netsh", from_netsh)):
        found = fn()
        print(f"  {label:<12} {len(found)} candidate(s)" + (f": {[f[0] for f in found]}" if found else ""))
        candidates.extend(found)

    reg, pac_url = from_registry()
    print(f"  {'registry':<12} {len(reg)} candidate(s)" + (f": {[f[0] for f in reg]}" if reg else ""))
    candidates.extend(reg)

    if pac_url:
        print(f"  {'PAC':<12} AutoConfigURL = {pac_url}")
        pac_found = from_pac(pac_url)
        print(f"  {'':<12} {len(pac_found)} candidate(s) scraped: {[f[0] for f in pac_found]}")
        candidates.extend(pac_found)
    else:
        print(f"  {'PAC':<12} no AutoConfigURL set")

    # De-duplicate, preserving the order in which sources were consulted.
    seen = set()
    unique: List[Candidate] = []
    for url, src in candidates:
        if url not in seen:
            seen.add(url)
            unique.append((url, src))

    print(f"\n{len(unique)} distinct proxy candidate(s). Testing against:\n  {args.target}\n")

    results = []
    ok_direct, detail = test(None, args.target, args.timeout)
    print(f"  {'(direct, no proxy)':<38} {'PASS' if ok_direct else 'FAIL'}  {detail}")
    results.append({"proxy": None, "source": "direct", "ok": ok_direct, "detail": detail})

    winner: Optional[Candidate] = None
    for url, src in unique:
        ok, detail = test(url, args.target, args.timeout)
        print(f"  {url:<38} {'PASS' if ok else 'FAIL'}  {detail}")
        results.append({"proxy": url, "source": src, "ok": ok, "detail": detail})
        if ok and winner is None:
            winner = (url, src)

    with open("proxy_probe.json", "w", encoding="utf-8") as fh:
        json.dump({"target": args.target, "results": results}, fh, indent=2)

    print()
    if ok_direct:
        print("Direct access works. The earlier failure was something else; re-run")
        print("cftc_loader.py --probe in this same shell.")
        return 0
    if winner:
        url, src = winner
        print(f"Working proxy found: {url}\n  (discovered via {src})\n")
        print("Set it for the session and re-run the loader:\n")
        print(f'    set HTTPS_PROXY={url}')
        print(f'    set HTTP_PROXY={url}')
        print("    python cftc_loader.py --probe\n")
        print("If that passes, put both variables into the scheduled task's environment")
        print("rather than relying on the interactive shell.")
        return 0

    print("No candidate reached the CFTC API.\n")
    print("That points at one of three things, in descending likelihood:")
    print("  1. publicreporting.cftc.gov is blocked by category at the firewall. Test")
    print("     the URL in your browser: if the browser loads it but no proxy here")
    print("     does, the proxy needs authentication rather than being blocked.")
    print("  2. The proxy requires authenticated access (NTLM/Kerberos), which plain")
    print("     requests will not do. requests-negotiate-sspi or pypac would be needed.")
    print("  3. The site is genuinely not allowlisted, which is a firewall request")
    print("     rather than anything solvable in code.")
    print("\nWrote proxy_probe.json")
    return 1


if __name__ == "__main__":
    sys.exit(main())
