from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

import qrcode
from qrcode.image.pure import PyPNGImage


def load_merged_runtime(state_dir: Path) -> dict[str, Any]:
    core = state_dir / "runtime.json"
    if not core.exists():
        raise FileNotFoundError(f"Missing {core} — run pipelinectl configure first")
    data = json.loads(core.read_text(encoding="utf-8"))
    br = state_dir / "bridge_runtime.json"
    if br.exists():
        data.update(json.loads(br.read_text(encoding="utf-8")))
    return data


def build_direct_vless(d: dict[str, Any]) -> str:
    return (
        f"vless://{d['uuid']}@{d['exit_ip']}:{d['reality_port']}?encryption=none&flow=xtls-rprx-vision"
        f"&security=reality&sni={d['reality_sni']}&fp=chrome&pbk={d['public_key']}&sid={d['short_id']}"
        f"&type=tcp&headerType=none#Direct-VLESS-{d['exit_ip']}"
    )


def build_direct_ss(d: dict[str, Any]) -> str:
    raw = f"{d['ss_method']}:{d['ss_password']}"
    enc = base64.b64encode(raw.encode()).decode().rstrip("=")
    return f"ss://{enc}@{d['exit_ip']}:{d['ss_port']}#Direct-SS2022-{d['exit_ip']}"


def build_bridge_vless(d: dict[str, Any]) -> str | None:
    if not d.get("bridge_ip"):
        return None
    port = d.get("bridge_reality_port") or 443
    # The bridge keeps its own client-facing SNI across Exit rotations (bridge_runtime.json);
    # a bridge_runtime.json written before that field existed used the Exit's.
    sni = d.get("bridge_reality_sni") or d["reality_sni"]
    return (
        f"vless://{d['bridge_uuid']}@{d['bridge_ip']}:{port}?encryption=none&flow=xtls-rprx-vision"
        f"&security=reality&sni={sni}&fp=chrome&pbk={d['bridge_public_key']}&sid={d['bridge_short_id']}"
        f"&type=tcp&headerType=none#Bridge-VLESS-{d['bridge_ip']}"
    )


def build_bridge_xhttp(d: dict[str, Any]) -> str | None:
    """Main bridge profile: VLESS + XHTTP + TLS to the bridge's own name.

    The address is the domain, never the IP, so moving the bridge is a DNS change and the client
    keeps its link. No flow: vision belongs to the RAW profile. Nothing about the RU -> WORLD
    chain (its UUID, key or target) is in a client link.

    fp=firefox, not chrome: on 2026-10-14 the chrome (and plain Go) ClientHello, too large for one
    TCP segment, stalled after 2-3 connections on the path to the entry; firefox and safari passed
    10 of 10, and v2rayN on Windows recovered after switching to firefox.
    """
    domain, uuid, path = d.get("bridge_domain"), d.get("bridge_xhttp_uuid"), d.get("bridge_xhttp_path")
    if not (domain and uuid and path):
        return None
    port = d.get("bridge_https_port") or 443
    mode = d.get("bridge_xhttp_mode") or "auto"
    return (
        f"vless://{uuid}@{domain}:{port}?encryption=none&security=tls&sni={domain}&alpn=h2&fp=firefox"
        f"&type=xhttp&host={domain}&path={quote(path, safe='')}&mode={mode}#Bridge-XHTTP-{domain}"
    )


def build_bridge_ss(d: dict[str, Any]) -> str | None:
    if not d.get("bridge_ip"):
        return None
    # Same: the bridge SS inbound has its own creds; fall back to the Exit's for old state files.
    method = d.get("bridge_ss_method") or d["ss_method"]
    password = d.get("bridge_ss_password") or d["ss_password"]
    port = d.get("bridge_ss_port") or d["ss_port"]
    raw = f"{method}:{password}"
    enc = base64.b64encode(raw.encode()).decode().rstrip("=")
    return f"ss://{enc}@{d['bridge_ip']}:{port}#Bridge-SS2022-{d['bridge_ip']}"


def build_client_links_map(d: dict[str, Any]) -> dict[str, str]:
    out = {
        "direct_vless": build_direct_vless(d),
        "direct_shadowsocks": build_direct_ss(d),
    }
    bridge_xhttp = build_bridge_xhttp(d)
    bridge_vless = build_bridge_vless(d)
    bridge_shadowsocks = build_bridge_ss(d)
    if bridge_xhttp:
        out["bridge_xhttp"] = bridge_xhttp
    if bridge_vless:
        out["bridge_vless"] = bridge_vless
    if bridge_shadowsocks:
        out["bridge_shadowsocks"] = bridge_shadowsocks
    return out


def write_qr_exports(repo_root: Path, links_map: dict[str, str]) -> Path:
    qr_dir = repo_root / ".client-links-qr"
    qr_dir.mkdir(parents=True, exist_ok=True)
    for name, uri in links_map.items():
        img = qrcode.make(uri, image_factory=PyPNGImage)
        img.save(str(qr_dir / f"{name}.png"))
    return qr_dir


def export_client_links(
    repo_root: Path,
    state_dir: Path,
    fmt: str = "text",
    write_file: bool = True,
    write_qr: bool = False,
) -> str:
    d = load_merged_runtime(state_dir)
    links_map = build_client_links_map(d)
    direct_v = links_map["direct_vless"]
    direct_s = links_map["direct_shadowsocks"]
    bx = links_map.get("bridge_xhttp")
    bv = links_map.get("bridge_vless")
    bs = links_map.get("bridge_shadowsocks")
    if fmt == "json":
        out: dict[str, Any] = {
            "direct_vless": direct_v,
            "direct_shadowsocks": direct_s,
            "panel_url": d.get("panel_url", ""),
        }
        if bx:
            out["bridge_xhttp"] = bx
        if bv:
            out["bridge_vless"] = bv
        if bs:
            out["bridge_shadowsocks"] = bs
        return json.dumps(out, indent=2) + "\n"
    lines = [
        "# Generated by pipelinectl links export",
        "",
        "# DIRECT (test)",
        direct_v,
        "",
        direct_s,
        "",
    ]
    if bx:
        lines += ["# THROUGH BRIDGE: HTTPS + XHTTP (main profile)", bx, ""]
    if bv and bs:
        lines += ["# THROUGH BRIDGE: RAW/REALITY and SS2022 (reserve)" if bx else "# THROUGH BRIDGE", bv, "", bs, ""]
    text = "\n".join(lines)
    if write_file:
        (repo_root / ".client-links").write_text(text, encoding="utf-8")
    if write_qr:
        write_qr_exports(repo_root, links_map)
    return text
