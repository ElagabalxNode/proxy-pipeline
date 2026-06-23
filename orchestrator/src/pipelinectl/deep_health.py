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


def _panel_login(
    data: dict[str, Any],
    panel_pass: str,
    jar: CookieJar,
) -> tuple[bool, str, urllib.request.OpenerDirector]:
    base = _login_base_url(data)
    login_url = f"{base}/login"
    user = str(data.get("panel_user") or "admin")
    body = urllib.parse.urlencode({"username": user, "password": panel_pass}).encode()
    req = urllib.request.Request(
        login_url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    try:
        with opener.open(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code} on {login_url}: {(e.read() or b'').decode()[:300]}", opener
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
    req = urllib.request.Request(list_url, method="GET")
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


def run_deep_health(
    data: dict[str, Any],
    exit_key: Path,
    panel_pass: str,
    exit_user: str,
) -> HealthReport:
    rpt = HealthReport()
    exit_ip = str(data.get("exit_ip") or "")
    if not exit_ip:
        rpt.add(CheckResult("deep_prereq", False, "error", "exit_ip missing in runtime"))
        return rpt

    if not panel_pass:
        rpt.add(CheckResult("deep_prereq", False, "error", "panel_pass missing (env PROXY_PIPELINE_PANEL_PASS or config)"))
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

    return rpt
