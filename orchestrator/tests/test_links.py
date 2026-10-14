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


# ── main profile: VLESS + XHTTP + TLS to the bridge's own name ─────────────────────

HTTPS_BRIDGE = {
    **BRIDGE,
    "bridge_reality_port": 8443,
    "bridge_domain": "lk.example.ru",
    "bridge_https_port": 443,
    "bridge_xhttp_uuid": "xhttp-uuid",
    "bridge_xhttp_path": "/a1b2c3d4e5f6",
    "bridge_xhttp_mode": "stream-up",
}
CHAIN_SECRETS = {
    "chain_uuid": "chain-uuid-secret",
    "chain_public_key": "chain-pbk-secret",
    "chain_private_key": "chain-private-secret",
    "chain_short_id": "c0ffee00",
    "chain_reality_sni": "chain-target.example",
    "chain_path": "/chain-relay-secret",
}


def test_xhttp_link_addresses_the_domain_and_carries_no_flow_or_chain_secrets(tmp_path: Path) -> None:
    m = _links(tmp_path, {**EXIT, **CHAIN_SECRETS}, HTTPS_BRIDGE)
    link = m["bridge_xhttp"]
    assert link == (
        "vless://xhttp-uuid@lk.example.ru:443?encryption=none&security=tls&sni=lk.example.ru&alpn=h2"
        "&fp=firefox&type=xhttp&host=lk.example.ru&path=%2Fa1b2c3d4e5f6&mode=stream-up"
        "#Bridge-XHTTP-lk.example.ru"
    )
    assert "flow" not in link and "198.51.100.5" not in link and "reality" not in link
    everything = json.dumps(m)
    for secret in CHAIN_SECRETS.values():
        assert secret not in everything
    # The RAW/REALITY profile stays available as a reserve, on the port it was moved to.
    assert m["bridge_vless"].startswith("vless://bridge-uuid@198.51.100.5:8443?")
    assert "flow=xtls-rprx-vision" in m["bridge_vless"]


def test_xhttp_link_survives_exit_rotation_and_a_new_bridge_address(tmp_path: Path) -> None:
    before = _links(tmp_path, EXIT, HTTPS_BRIDGE)["bridge_xhttp"]
    rotated = {**EXIT, "exit_ip": "203.0.113.99", "uuid": "new", "public_key": "new-pbk",
               "chain_uuid": "new-chain", "reality_sni": "other.example.net"}
    moved = {**HTTPS_BRIDGE, "bridge_ip": "198.51.100.77"}  # DNS follows; the link must not change
    assert _links(tmp_path, rotated, moved)["bridge_xhttp"] == before


def test_no_xhttp_link_without_a_complete_https_entry(tmp_path: Path) -> None:
    assert "bridge_xhttp" not in _links(tmp_path, EXIT, BRIDGE)
    partial = {k: v for k, v in HTTPS_BRIDGE.items() if k != "bridge_xhttp_path"}
    assert "bridge_xhttp" not in _links(tmp_path, EXIT, partial)


def test_export_lists_the_https_profile_first_and_marks_the_reserve(tmp_path: Path) -> None:
    (tmp_path / "runtime.json").write_text(json.dumps(EXIT), encoding="utf-8")
    (tmp_path / "bridge_runtime.json").write_text(json.dumps(HTTPS_BRIDGE), encoding="utf-8")
    text = links.export_client_links(tmp_path, tmp_path, write_file=False)
    assert text.index("HTTPS + XHTTP (main profile)") < text.index("RAW/REALITY and SS2022 (reserve)")
    exported = json.loads(links.export_client_links(tmp_path, tmp_path, fmt="json", write_file=False))
    assert exported["bridge_xhttp"].startswith("vless://xhttp-uuid@lk.example.ru:443?")
    (tmp_path / "bridge_runtime.json").write_text(json.dumps(BRIDGE), encoding="utf-8")
    plain = links.export_client_links(tmp_path, tmp_path, write_file=False)
    assert "# THROUGH BRIDGE\n" in plain and "reserve" not in plain
