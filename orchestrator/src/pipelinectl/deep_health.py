"""Deep health checks: 3X-UI API, Exit egress IP, TLS handshake for Reality."""

from __future__ import annotations

import json
import socket
import ssl
import subprocess
import urllib.error
import urllib.parse
import urllib.request
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


def _login_base_url(data: dict[str, Any]) -> str:
    panel_url = str(data.get("panel_url") or "").rstrip("/")
    if panel_url.startswith("http"):
        return panel_url
    exit_ip = str(data.get("exit_ip") or "")
    port = str(data.get("panel_port") or "2053")
    prefix = _panel_request_prefix(str(data.get("panel_path") or "/"))
    return f"http://{exit_ip}:{port}{prefix}".rstrip("/")


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
    data: dict[str, Any],
    panel_pass: str,
    jar: CookieJar,
) -> tuple[bool, str, urllib.request.OpenerDirector]:
    base = _login_base_url(data)
    login_url = f"{base}/login"
    user = str(data.get("panel_user") or "admin")
    body = urllib.parse.urlencode({"username": user, "password": panel_pass}).encode()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
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
    data: dict[str, Any],
    opener: urllib.request.OpenerDirector,
) -> tuple[bool, str]:
    base = _login_base_url(data)
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
        f"sudo {BRIDGE_PROBE_SCRIPT} && sudo tail -n 8 {BRIDGE_PROBE_LOG}"
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

    jar = CookieJar()
    ok_login, msg_login, opener = _panel_login(data, panel_pass, jar)
    rpt.add(CheckResult("panel_login", ok_login, "error" if not ok_login else "info", msg_login))

    if ok_login:
        ok_ib, msg_ib = _panel_inbounds_list(data, opener)
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

    if bridge_ip and bridge_key:
        ok_p, msg_p, recs = _bridge_probe_round(bridge_ip, bridge_user, bridge_key)
        if not ok_p:
            rpt.add(CheckResult("bridge_probe", False, "warn", msg_p))
        for r in recs:
            good = r.get("ok") is True
            detail = f"{r.get('ms')}ms"
            if r.get("code"):
                detail += f" http={r.get('code')}"
            if r.get("err"):
                detail += f" {r.get('err')}"
            rpt.add(
                CheckResult(
                    f"bridge_{r.get('check')}", good, "error" if not good else "info", detail
                )
            )

    return rpt
