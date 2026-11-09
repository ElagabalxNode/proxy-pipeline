from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

import qrcode
from qrcode.image.pure import PyPNGImage

# TLS fingerprint of the profiles that enter Russia through the bridge. On 2026-10-14 the chrome
# (and plain Go) ClientHello, too large for one TCP segment, stalled after 2-3 connections on the
# path to the bridge; firefox and safari passed 10 of 10. The direct profile keeps chrome: it has
# worked unchanged for clients and a new fingerprint would need a re-import for nothing.
BRIDGE_FINGERPRINT = "firefox"
# XHTTP mode written into the client link when the server accepts every mode ("auto"). Left to
# itself ("auto" in the link) Shadowrocket picked packet-up: one HTTPS request per piece of upload,
# >1400 requests a minute in bursts, after which the user's path to the bridge was cut (2026-10-20).
DEFAULT_XHTTP_CLIENT_MODE = "stream-up"
XHTTP_CLIENT_MODES = ("stream-up", "stream-one", "packet-up")
# Profile names shown in client apps. Fixed, without addresses: client configs (proxy groups in
# Shadowrocket, policy groups in v2rayN) refer to these names, and a rotated Exit or bridge must
# not break them. Bridge-SS2022 is listed for completeness; its key is not valid for strict clients.
NAMES = {
    "direct_vless": "Direct-VLESS",
    "direct_shadowsocks": "Direct-SS2022",
    "bridge_xhttp": "Bridge-XHTTP",
    "bridge_vless": "Bridge-VLESS",
    "bridge_shadowsocks": "Bridge-SS2022",
}


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
        f"&type=tcp&headerType=none#{NAMES['direct_vless']}"
    )


def _ss_userinfo(method: str, password: str) -> str:
    """SIP002 userinfo: URL-safe base64 of method:password without padding ('+' and '/' of the
    standard alphabet would be read as URL syntax by some clients)."""
    return base64.urlsafe_b64encode(f"{method}:{password}".encode()).decode().rstrip("=")


def build_direct_ss(d: dict[str, Any]) -> str:
    enc = _ss_userinfo(d["ss_method"], d["ss_password"])
    return f"ss://{enc}@{d['exit_ip']}:{d['ss_port']}#{NAMES['direct_shadowsocks']}"


def build_bridge_vless(d: dict[str, Any]) -> str | None:
    if not d.get("bridge_ip"):
        return None
    port = d.get("bridge_reality_port") or 443
    # The bridge keeps its own client-facing SNI across Exit rotations (bridge_runtime.json);
    # a bridge_runtime.json written before that field existed used the Exit's.
    sni = d.get("bridge_reality_sni") or d["reality_sni"]
    return (
        f"vless://{d['bridge_uuid']}@{d['bridge_ip']}:{port}?encryption=none&flow=xtls-rprx-vision"
        f"&security=reality&sni={sni}&fp={BRIDGE_FINGERPRINT}&pbk={d['bridge_public_key']}&sid={d['bridge_short_id']}"
        f"&type=tcp&headerType=none#{NAMES['bridge_vless']}"
    )


def xhttp_link_mode(server_mode: str | None, client_mode: str | None = None) -> str:
    """Mode the client is told to use: the server's own mode, or for an "auto" server the
    configured client mode (default stream-up). A link never says "auto": clients then choose."""
    server = (server_mode or "auto").strip()
    if server != "auto":
        return server
    mode = (client_mode or DEFAULT_XHTTP_CLIENT_MODE).strip()
    if mode not in XHTTP_CLIENT_MODES:
        raise ValueError(f"bridge_entry.client_mode must be one of {', '.join(XHTTP_CLIENT_MODES)}")
    return mode


def build_bridge_xhttp(d: dict[str, Any], client_mode: str | None = None) -> str | None:
    """Main bridge profile: VLESS + XHTTP + TLS to the bridge's own name.

    The address is the domain, never the IP, so moving the bridge is a DNS change and the client
    keeps its link. No flow: vision belongs to the RAW profile. Nothing about the RU -> WORLD
    chain (its UUID, key or target) is in a client link.

    Fingerprint and mode: BRIDGE_FINGERPRINT and xhttp_link_mode().
    """
    domain, uuid, path = d.get("bridge_domain"), d.get("bridge_xhttp_uuid"), d.get("bridge_xhttp_path")
    if not (domain and uuid and path):
        return None
    port = d.get("bridge_https_port") or 443
    mode = xhttp_link_mode(d.get("bridge_xhttp_mode"), client_mode)
    return (
        f"vless://{uuid}@{domain}:{port}?encryption=none&security=tls&sni={domain}&alpn=h2&fp={BRIDGE_FINGERPRINT}"
        f"&type=xhttp&host={domain}&path={quote(path, safe='')}&mode={mode}#{NAMES['bridge_xhttp']}"
    )


def build_bridge_ss(d: dict[str, Any]) -> str | None:
    if not d.get("bridge_ip"):
        return None
    # Same: the bridge SS inbound has its own creds; fall back to the Exit's for old state files.
    method = d.get("bridge_ss_method") or d["ss_method"]
    password = d.get("bridge_ss_password") or d["ss_password"]
    port = d.get("bridge_ss_port") or d["ss_port"]
    enc = _ss_userinfo(method, password)
    return f"ss://{enc}@{d['bridge_ip']}:{port}#{NAMES['bridge_shadowsocks']}"


def build_client_links_map(d: dict[str, Any], xhttp_client_mode: str | None = None) -> dict[str, str]:
    out = {
        "direct_vless": build_direct_vless(d),
        "direct_shadowsocks": build_direct_ss(d),
    }
    bridge_xhttp = build_bridge_xhttp(d, xhttp_client_mode)
    bridge_vless = build_bridge_vless(d)
    bridge_shadowsocks = build_bridge_ss(d)
    if bridge_xhttp:
        out["bridge_xhttp"] = bridge_xhttp
    if bridge_vless:
        out["bridge_vless"] = bridge_vless
    if bridge_shadowsocks:
        out["bridge_shadowsocks"] = bridge_shadowsocks
    return out


def _write_private(path: Path, data: bytes) -> None:
    """Links carry keys and passwords: owner-only file, replaced atomically."""
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def write_qr_exports(repo_root: Path, links_map: dict[str, str]) -> Path:
    """One PNG per link; PNGs of links that no longer exist are removed, so a stale QR (an old
    bridge address) cannot be handed out again."""
    qr_dir = repo_root / ".client-links-qr"
    qr_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(qr_dir, 0o700)
    for stale in qr_dir.glob("*.png"):
        if stale.stem not in links_map:
            stale.unlink()
    for name, uri in links_map.items():
        img = qrcode.make(uri, image_factory=PyPNGImage)
        target = qr_dir / f"{name}.png"
        with open(qr_dir / f".{name}.png.tmp", "wb") as stream:
            img.save(stream)
        os.chmod(qr_dir / f".{name}.png.tmp", 0o600)
        os.replace(qr_dir / f".{name}.png.tmp", target)
    return qr_dir


def export_client_links(
    repo_root: Path,
    state_dir: Path,
    fmt: str = "text",
    write_file: bool = True,
    write_qr: bool = False,
    xhttp_client_mode: str | None = None,
) -> str:
    d = load_merged_runtime(state_dir)
    links_map = build_client_links_map(d, xhttp_client_mode)
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
        _write_private(repo_root / ".client-links", text.encode("utf-8"))
    if write_qr:
        write_qr_exports(repo_root, links_map)
    return text
