from __future__ import annotations

import json
from pathlib import Path

import yaml

from pipelinectl import links


def test_example_pipeline_yaml_parses() -> None:
    root = Path(__file__).resolve().parents[2]  # .../orchestrator/tests -> repo root
    ex = root / "config" / "pipeline.example.yaml"
    data = yaml.safe_load(ex.read_text(encoding="utf-8"))
    assert data.get("version") == 1
    assert "paths" in data
    assert "providers" in data


def test_docs_mention_contract_outputs() -> None:
    root = Path(__file__).resolve().parents[2]
    doc = (root / "docs" / "PIPELINE.md").read_text(encoding="utf-8")
    for token in ("exit_ip", "bridge_ip", "runtime.json", "terraform output"):
        assert token in doc


def test_links_json_roundtrip(tmp_path: Path) -> None:
    st = tmp_path
    (st / "runtime.json").write_text(
        json.dumps(
            {
                "exit_ip": "203.0.113.10",
                "uuid": "u",
                "public_key": "pk",
                "private_key": "pr",
                "short_id": "abcd",
                "reality_sni": "www.example.com",
                "reality_port": 443,
                "ss_password": "pw",
                "ss_method": "2022-blake3-aes-128-gcm",
                "ss_port": 8388,
                "chain_uuid": "c",
                "chain_port": 8443,
            }
        ),
        encoding="utf-8",
    )
    out = links.export_client_links(tmp_path, st, fmt="json")
    assert "direct_vless" in out
    assert "203.0.113.10" in out


def test_links_qr_export(tmp_path: Path) -> None:
    st = tmp_path
    (st / "runtime.json").write_text(
        json.dumps(
            {
                "exit_ip": "203.0.113.10",
                "uuid": "u",
                "public_key": "pk",
                "private_key": "pr",
                "short_id": "abcd",
                "reality_sni": "www.example.com",
                "reality_port": 443,
                "ss_password": "pw",
                "ss_method": "2022-blake3-aes-128-gcm",
                "ss_port": 8388,
                "chain_uuid": "c",
                "chain_port": 8443,
                "bridge_ip": "198.51.100.20",
                "bridge_uuid": "bu",
                "bridge_public_key": "bpk",
                "bridge_short_id": "ef01",
            }
        ),
        encoding="utf-8",
    )
    links.export_client_links(tmp_path, st, fmt="text", write_qr=True)
    qr_dir = tmp_path / ".client-links-qr"
    assert (qr_dir / "direct_vless.png").exists()
    assert (qr_dir / "direct_shadowsocks.png").exists()
    assert (qr_dir / "bridge_vless.png").exists()
    assert (qr_dir / "bridge_shadowsocks.png").exists()
