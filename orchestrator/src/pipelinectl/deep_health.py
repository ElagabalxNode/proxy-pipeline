"""Deep health checks: 3X-UI API, Exit egress IP, TLS handshake for Reality.

The panel is reached only through an SSH tunnel to its loopback listener on WORLD: the
firewall does not open the panel port, and no credentials cross the internet in plain HTTP.
"""

from __future__ import annotations

import json
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any

from pipelinectl.health import CheckResult, HealthReport

EXPECTED_INBOUND_REMARKS = frozenset(
    {"VLESS-Reality-Vision", "SS2022-Fallback", "Chain-Bridge-Entry"},
)


def _panel_request_prefix(panel_path: str) -> str:
    p = (panel_path or "/").strip()
    if p in ("", "/"):
        return ""
    p = p.strip("/")
    return f"/{p}" if p else ""


def _panel_endpoint(data: dict[str, Any]) -> tuple[int, str]:
    """Panel port and request prefix on WORLD; the public panel_url is never dialled."""
    port = int(data.get("panel_port") or 2053)
    return port, _panel_request_prefix(str(data.get("panel_path") or "/"))


@contextmanager
def _panel_tunnel(
    exit_ip: str, exit_user: str, exit_key: Path, panel_port: int
) -> Iterator[str]:
    """Forward a free loopback port to the panel's loopback listener; yields the local origin."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        local = sock.getsockname()[1]
    cmd = [
        "ssh",
        "-i",
        str(exit_key),
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "ExitOnForwardFailure=yes",
        "-N",
        "-L",
        f"127.0.0.1:{local}:127.0.0.1:{panel_port}",
        f"{exit_user}@{exit_ip}",
    ]
    with subprocess.Popen(
        cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    ) as proc:
        try:
            deadline = time.monotonic() + 15
            while True:
                if proc.poll() is not None:
                    raise OSError(f"ssh tunnel to {exit_ip} exited with {proc.returncode}")
                try:
                    with socket.create_connection(("127.0.0.1", local), timeout=0.2):
                        break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise OSError(f"ssh tunnel to {exit_ip} timed out") from None
                    time.sleep(0.1)
            yield f"http://127.0.0.1:{local}"
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def _panel_csrf_token(base: str, opener: urllib.request.OpenerDirector) -> str | None:
    """3X-UI >= 3.4 rejects any POST without an X-CSRF-Token header (HTTP 403, empty body).

    The token is bound to the session cookie, so it is fetched through the same opener right
    before the request that needs it. Older panels have no /csrf-token: return None and go on.
    """
    req = urllib.request.Request(f"{base}/csrf-token", method="GET")
    try:
        with opener.open(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):  # HTTPError is an OSError
        return None
    tok = payload.get("obj") if isinstance(payload, dict) else None
    return str(tok) if tok else None


def _panel_login(
    base: str,
    user: str,
    panel_pass: str,
    jar: CookieJar,
) -> tuple[bool, str, urllib.request.OpenerDirector]:
    login_url = f"{base}/login"
    body = urllib.parse.urlencode({"username": user, "password": panel_pass}).encode()
    # The tunnel end is loopback: an HTTP proxy from the environment must not see the login.
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(jar)
    )
    csrf = _panel_csrf_token(base, opener)
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if csrf:
        headers["X-CSRF-Token"] = csrf
    req = urllib.request.Request(login_url, data=body, method="POST", headers=headers)
    try:
        with opener.open(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        hint = "" if csrf else f" (no CSRF token from {base}/csrf-token)"
        return (
            False,
            f"HTTP {e.code} on {login_url}{hint}: {(e.read() or b'').decode()[:300]}",
            opener,
        )
    except OSError as e:
        return False, f"{login_url}: {e}", opener

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return False, f"Non-JSON login response: {raw[:200]}", opener

    if payload.get("success") is True:
        return True, "", opener
    return False, str(payload)[:500], opener


def _panel_inbounds_list(
    base: str,
    opener: urllib.request.OpenerDirector,
) -> tuple[bool, str]:
    list_url = f"{base}/panel/api/inbounds/list"
    csrf = _panel_csrf_token(base, opener)
    req = urllib.request.Request(
        list_url, method="GET", headers={"X-CSRF-Token": csrf} if csrf else {}
    )
    try:
        with opener.open(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code} on {list_url}"
    except OSError as e:
        return False, f"{list_url}: {e}"

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return False, f"Non-JSON inbounds response: {raw[:200]}"

    if payload.get("success") is not True:
        return False, str(payload)[:500]

    obj = payload.get("obj")
    if not isinstance(obj, list):
        return False, f"Unexpected obj type: {type(obj).__name__}"

    found: dict[str, bool] = {}
    for item in obj:
        if not isinstance(item, dict):
            continue
        remark = str(item.get("remark") or "")
        if remark in EXPECTED_INBOUND_REMARKS:
            found[remark] = bool(item.get("enable", False))

    missing = sorted(EXPECTED_INBOUND_REMARKS - set(found))
    disabled = sorted(k for k, v in found.items() if not v)
    if missing or disabled:
        return False, f"missing={missing} disabled={disabled} total={len(obj)}"

    return True, f"inbounds_ok count={len(obj)}"


def _exit_public_ip_via_ssh(
    exit_ip: str,
    exit_user: str,
    exit_key: Path,
) -> tuple[bool, str]:
    cmd = [
        "ssh",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "ConnectTimeout=10",
        "-i",
        str(exit_key),
        f"{exit_user}@{exit_ip}",
        "curl -sS --max-time 10 https://api4.ipify.org",
    ]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
    except Exception as e:  # noqa: BLE001
        return False, str(e)
    out = (p.stdout or "").strip()
    if p.returncode != 0:
        return False, (p.stderr or out or "ssh curl failed")[:400]
    if not out:
        return False, "empty ipify response"
    if out != exit_ip:
        return False, f"expected_exit_ip={exit_ip} observed={out}"
    return True, out


def _tls_reality_probe(host: str, port: int, server_hostname: str) -> tuple[bool, str]:
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((host, port), timeout=10) as sock:
            with ctx.wrap_socket(sock, server_hostname=server_hostname) as tls:
                cert = tls.getpeercert()
    except ssl.SSLError as e:
        return False, f"ssl_error: {e}"
    except OSError as e:
        return False, f"connect_error: {e}"

    # Handshake succeeded with check_hostname=True — cert matches server_hostname.
    subj = cert.get("subject", ()) if cert else ()
    cn = ""
    for item in subj:
        for pair in item:
            if len(pair) == 2 and pair[0] == "commonName":
                cn = str(pair[1])
                break
    return True, f"handshake_ok sni={server_hostname} cn={cn or '?'}"


def _entry_https_probe(
    domain: str, bridge_ip: str, port: int, cafile: str | None = None,
    resolve: Any = socket.getaddrinfo,
) -> list[CheckResult]:
    """The HTTPS entry as a client outside the bridge sees it: public DNS, then TLS and the page.

    DNS and TLS are separate results on purpose: a wrong or missing record and a broken
    certificate need different fixes, and neither is a reason to touch the Exit.
    """
    try:
        resolved = sorted({info[4][0] for info in resolve(domain, port, type=socket.SOCK_STREAM)})
    except OSError as e:
        resolved, dns_detail = [], f"{domain} does not resolve: {e}"
    else:
        dns_detail = f"{domain} -> {', '.join(resolved)}"
    dns_ok = resolved == [bridge_ip]
    if resolved and not dns_ok:
        dns_detail += f" (expected only {bridge_ip})"
    results = [CheckResult("entry_dns", dns_ok, "info" if dns_ok else "error", dns_detail)]
    context = ssl.create_default_context(cafile=cafile)
    try:
        with socket.create_connection((bridge_ip, port), timeout=10) as sock, \
                context.wrap_socket(sock, server_hostname=domain) as tls:
            tls.sendall(f"GET / HTTP/1.1\r\nHost: {domain}\r\nConnection: close\r\n\r\n".encode())
            status = tls.recv(64).split(b"\r\n", 1)[0].decode(errors="replace")
            protocol = tls.version()
    except ssl.SSLCertVerificationError as e:
        # A certificate that does not validate is wrong from every vantage point.
        results.append(CheckResult("entry_https", False, "error", f"certificate: {e}"))
    except OSError as e:  # includes handshake timeouts and resets
        # Reachability from the controller's network says little about clients: on 2026-10-13
        # this path timed out while Russian and German vantage points got the page. The bridge's
        # own bridge_entry_https check is the one that fails the report.
        results.append(CheckResult(
            "entry_https", False, "warn", f"not reachable from the controller ({bridge_ip}:{port}): {e}"))
    else:
        ok = " 200" in status
        results.append(CheckResult("entry_https", ok, "info" if ok else "error", f"{protocol} {status}"))
    return results


BRIDGE_PROBE_SCRIPT = "/usr/local/bin/chain-probe.sh"
BRIDGE_PROBE_LOG = "/var/log/chain-probe/probe.jsonl"


def _bridge_probe_round(
    bridge_ip: str,
    bridge_user: str,
    bridge_key: Path,
    runner: Any = subprocess.run,
) -> tuple[bool, str, list[dict[str, Any]]]:
    """Run ONE chain-probe round on the bridge and return its records.

    This is the vantage clients depend on: bridge→Exit hop, the whole chain, the bridge's own
    egress and its Reality dest. Checks from the controller cannot see any of that.
    """
    remote = (
        f"test -x {BRIDGE_PROBE_SCRIPT} || {{ echo NO_PROBE; exit 3; }}; "
        f"sudo {BRIDGE_PROBE_SCRIPT} && sudo tail -n 16 {BRIDGE_PROBE_LOG}"
    )
    cmd = [
        "ssh",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=15",
        "-i",
        str(bridge_key),
        f"{bridge_user}@{bridge_ip}",
        remote,
    ]
    try:
        p = runner(cmd, capture_output=True, text=True, timeout=150)
    except Exception as e:  # noqa: BLE001
        return False, str(e), []
    out = p.stdout or ""
    if "NO_PROBE" in out:
        return False, "chain-probe not installed on the bridge (run pipelinectl configure)", []
    if p.returncode != 0:
        return False, (p.stderr or out or "ssh failed").strip()[:300], []
    recs: list[dict[str, Any]] = []
    for line in out.splitlines():
        if line.startswith("{"):
            try:
                recs.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if not recs:
        return False, "no probe output", []
    last_ts = recs[-1].get("ts")
    return True, "", [r for r in recs if r.get("ts") == last_ts]


def run_deep_health(
    data: dict[str, Any],
    exit_key: Path,
    panel_pass: str,
    exit_user: str,
    bridge_ip: str = "",
    bridge_user: str = "ubuntu",
    bridge_key: Path | None = None,
) -> HealthReport:
    rpt = HealthReport()
    exit_ip = str(data.get("exit_ip") or "")
    if not exit_ip:
        rpt.add(CheckResult("deep_prereq", False, "error", "exit_ip missing in runtime"))
        return rpt

    if not panel_pass:
        rpt.add(
            CheckResult(
                "deep_prereq",
                False,
                "error",
                "panel_pass missing (env PROXY_PIPELINE_PANEL_PASS or config)",
            )
        )
        return rpt

    panel_port, prefix = _panel_endpoint(data)
    user = str(data.get("panel_user") or "admin")
    ok_login, msg_login, ok_ib, msg_ib = False, "", False, ""
    try:
        with _panel_tunnel(exit_ip, exit_user, exit_key, panel_port) as origin:
            ok_login, msg_login, opener = _panel_login(
                f"{origin}{prefix}", user, panel_pass, CookieJar()
            )
            if ok_login:
                ok_ib, msg_ib = _panel_inbounds_list(f"{origin}{prefix}", opener)
    except OSError as e:
        ok_login, msg_login = False, str(e)
    via = f"via ssh to 127.0.0.1:{panel_port}"
    rpt.add(
        CheckResult(
            "panel_login", ok_login, "info" if ok_login else "error", msg_login if msg_login else via
        )
    )
    if ok_login:
        rpt.add(CheckResult("panel_inbounds", ok_ib, "error" if not ok_ib else "info", msg_ib))
    else:
        rpt.add(CheckResult("panel_inbounds", False, "warn", "skipped: login failed"))

    ok_ip, msg_ip = _exit_public_ip_via_ssh(exit_ip, exit_user, exit_key)
    rpt.add(CheckResult("exit_public_ip", ok_ip, "error" if not ok_ip else "info", msg_ip))

    reality_port = int(data.get("reality_port", 443))
    reality_sni = str(data.get("reality_sni") or "")
    if reality_sni:
        ok_tls, msg_tls = _tls_reality_probe(exit_ip, reality_port, reality_sni)
        rpt.add(CheckResult("tls_reality", ok_tls, "error" if not ok_tls else "info", msg_tls))
    else:
        rpt.add(CheckResult("tls_reality", False, "warn", "reality_sni missing in runtime"))

    entry_domain = str(data.get("bridge_domain") or "")
    if bridge_ip and entry_domain:
        for check in _entry_https_probe(entry_domain, bridge_ip, int(data.get("bridge_https_port") or 443)):
            rpt.add(check)

    if bridge_ip and bridge_key:
        ok_p, msg_p, recs = _bridge_probe_round(bridge_ip, bridge_user, bridge_key)
        if not ok_p:
            rpt.add(CheckResult("bridge_probe", False, "warn", msg_p))
        # Reference checks do not fail the report: the bridge's direct egress is never used by
        # clients, and with the HTTPS entry the Reality dest matters only to the reserve profile.
        reference = {"direct_http"} | ({"dest_tls"} if entry_domain else set())
        for r in recs:
            good = r.get("ok") is True
            detail = f"{r.get('ms')}ms"
            if r.get("code"):
                detail += f" http={r.get('code')}"
            if r.get("err"):
                detail += f" {r.get('err')}"
            failed = "warn" if r.get("check") in reference else "error"
            rpt.add(
                CheckResult(f"bridge_{r.get('check')}", good, failed if not good else "info", detail)
            )

    return rpt
