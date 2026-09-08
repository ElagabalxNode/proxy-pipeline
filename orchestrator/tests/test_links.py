from __future__ import annotations

import base64
import json
from pathlib import Path

from pipelinectl import links

EXIT = {
    "exit_ip": "203.0.113.10",
    "uuid": "exit-uuid",
    "public_key": "exit-pbk",
    "private_key": "pr",
    "short_id": "e1e1e1e1",
    "reality_sni": "exit.example.com",
    "reality_port": 443,
    "ss_password": "exit-ss-pass",
    "ss_method": "2022-blake3-aes-128-gcm",
    "ss_port": 8388,
    "chain_uuid": "c",
    "chain_port": 8443,
    "panel_url": "http://203.0.113.10:2053/p/",
}
BRIDGE = {
    "bridge_ip": "198.51.100.5",
    "bridge_ssh_user": "ubuntu",
    "bridge_uuid": "bridge-uuid",
    "bridge_public_key": "bridge-pbk",
    "bridge_short_id": "b2b2b2b2",
    "bridge_reality_port": 443,
    "bridge_reality_sni": "bridge.example.com",
    "bridge_ss_method": "2022-blake3-aes-128-gcm",
    "bridge_ss_password": "bridge-ss-pass",
    "bridge_ss_port": 8388,
}


def _links(st: Path, exit_d: dict, bridge_d: dict) -> dict[str, str]:
    (st / "runtime.json").write_text(json.dumps(exit_d), encoding="utf-8")
    (st / "bridge_runtime.json").write_text(json.dumps(bridge_d), encoding="utf-8")
    return links.build_client_links_map(links.load_merged_runtime(st))


def _ss_userinfo(method: str, password: str) -> str:
    return base64.b64encode(f"{method}:{password}".encode()).decode().rstrip("=")


def test_bridge_links_use_bridge_identity_not_exit(tmp_path: Path) -> None:
    m = _links(tmp_path, EXIT, BRIDGE)
    bv, bs = m["bridge_vless"], m["bridge_shadowsocks"]
    assert bv.startswith("vless://bridge-uuid@198.51.100.5:443?")
    assert "sni=bridge.example.com" in bv and "pbk=bridge-pbk" in bv and "sid=b2b2b2b2" in bv
    assert "exit.example.com" not in bv
    assert (
        bs
        == f"ss://{_ss_userinfo(BRIDGE['bridge_ss_method'], 'bridge-ss-pass')}@198.51.100.5:8388#Bridge-SS2022-198.51.100.5"
    )


def test_bridge_links_survive_exit_rotation(tmp_path: Path) -> None:
    before = _links(tmp_path, EXIT, BRIDGE)
    rotated_exit = {
        **EXIT,
        "exit_ip": "203.0.113.99",
        "uuid": "new-exit-uuid",
        "public_key": "new-exit-pbk",
        "short_id": "ffffffff",
        "reality_sni": "other.example.net",  # re-probed target
        "ss_password": "new-exit-ss-pass",
        "chain_uuid": "new-chain",
    }
    after = _links(tmp_path, rotated_exit, BRIDGE)
    assert after["bridge_vless"] == before["bridge_vless"]
    assert after["bridge_shadowsocks"] == before["bridge_shadowsocks"]
    assert after["direct_vless"] != before["direct_vless"]
    assert after["direct_shadowsocks"] != before["direct_shadowsocks"]


def test_bridge_links_fall_back_to_exit_fields_for_old_state(tmp_path: Path) -> None:
    # bridge_runtime.json written before the bridge carried its own client-facing fields
    old_bridge = {
        k: v
        for k, v in BRIDGE.items()
        if k
        not in ("bridge_reality_sni", "bridge_ss_method", "bridge_ss_password", "bridge_ss_port")
    }
    m = _links(tmp_path, EXIT, old_bridge)
    assert "sni=exit.example.com" in m["bridge_vless"]
    assert (
        m["bridge_shadowsocks"]
        == f"ss://{_ss_userinfo(EXIT['ss_method'], 'exit-ss-pass')}@198.51.100.5:8388#Bridge-SS2022-198.51.100.5"
    )
