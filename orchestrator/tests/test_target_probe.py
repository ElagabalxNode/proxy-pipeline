import io
import json
import shlex
import ssl
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest
from typer.testing import CliRunner

from pipelinectl import cli, probe, target_probe

TARGET = {
    "id": "edge", "dest": "192.0.2.7:8443", "sni": "site.example",
    "expected_alpn": ["h2"], "priority": 1,
}


@pytest.fixture
def tls_env(monkeypatch):
    sock = MagicMock()
    sock.__enter__.return_value = sock
    sock.getpeername.return_value = ("192.0.2.7", 8443)
    tls = MagicMock()
    tls.__enter__.return_value = tls
    tls.version.return_value = "TLSv1.3"
    tls.selected_alpn_protocol.return_value = "h2"
    ctx = Mock()
    ctx.wrap_socket.return_value = tls
    connect = Mock(return_value=sock)
    monkeypatch.setattr(target_probe.socket, "create_connection", connect)
    monkeypatch.setattr(target_probe.ssl, "create_default_context", lambda: ctx)
    return connect, sock, ctx, tls


def test_dial_dest_separately_from_sni_and_require_tls13_alpn(tls_env):
    connect, sock, ctx, tls = tls_env
    result = target_probe.probe_target(TARGET)
    connect.assert_called_once_with(("192.0.2.7", 8443), timeout=10)
    ctx.wrap_socket.assert_called_once_with(sock, server_hostname="site.example")
    ctx.set_alpn_protocols.assert_called_once_with(["h2"])
    assert ctx.minimum_version == ssl.TLSVersion.TLSv1_3
    assert result["eligible"] is True
    assert result["stage"] == "ok"
    assert result["peer_ip"] == "192.0.2.7"
    assert result["tls_version"] == "TLSv1.3"
    assert result["rtt_ms"] is not None and result["handshake_ms"] is not None
    sock.__exit__.assert_called_once()
    tls.__exit__.assert_called_once()


@pytest.mark.parametrize("alpn", [None, "http/1.1"])
def test_tls_without_expected_alpn_is_ineligible(tls_env, alpn):
    _, _, _, tls = tls_env
    tls.selected_alpn_protocol.return_value = alpn
    result = target_probe.probe_target(TARGET)
    assert result["tls_ok"] is True
    assert result["eligible"] is False
    assert result["stage"] == "alpn"
    assert "ALPN" in result["error"]
    assert probe._score(result) < 0
    with pytest.raises(RuntimeError, match="TLS/ALPN"):
        probe.pick_winner([result], "192.0.2.10", "test")


def test_tls12_cannot_be_selected(tls_env):
    tls_env[3].version.return_value = "TLSv1.2"
    result = target_probe.probe_target(TARGET)
    assert result["stage"] == "tls"
    assert not result["eligible"] and not result["tls_ok"]


def test_tcp_failure_and_certificate_failure_are_distinct(tls_env):
    connect, sock, ctx, _ = tls_env
    connect.side_effect = TimeoutError("TCP timed out")
    result = target_probe.probe_target(TARGET)
    assert result["stage"] == "tcp" and not result["tcp_ok"]
    assert "timed out" in result["error"]
    connect.side_effect = None
    ctx.wrap_socket.side_effect = ssl.SSLCertVerificationError("hostname mismatch")
    result = target_probe.probe_target(TARGET)
    assert result["stage"] == "tls" and result["tcp_ok"]
    assert "hostname mismatch" in result["error"]
    assert not result["eligible"]
    sock.__exit__.assert_called_once()


@pytest.mark.parametrize("dest", [
    "site.example", "site.example:0", "site.example:65536", "https://site.example:443",
    "user@site.example:443", "site.example:443/path", "site.example:443?q=x", "[::1]",
])
def test_malformed_dest_fails_before_network(tls_env, dest):
    result = target_probe.probe_target({**TARGET, "dest": dest})
    assert result["stage"] == "config" and not result["eligible"]
    tls_env[0].assert_not_called()


def test_ipv6_dest_and_malformed_alpn(tls_env):
    assert target_probe.target_endpoint({"dest": "[2001:db8::1]:8443"}) == ("2001:db8::1", 8443)
    result = target_probe.probe_target({**TARGET, "expected_alpn": "h2"})
    assert result["stage"] == "config"
    tls_env[0].assert_not_called()


def test_standalone_main_reports_success_and_failure(tls_env, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps([TARGET, {**TARGET, "dest": "bad"}])))
    target_probe.main()
    results = json.loads(capsys.readouterr().out)
    assert results[0]["eligible"] is True
    assert results[1]["stage"] == "config"


def test_ssh_executes_same_standalone_code_and_preserves_alpn(monkeypatch):
    def run(cmd, **kwargs):
        parts = shlex.split(cmd[-1])
        assert parts[:2] == ["python3", "-c"]
        assert parts[2] == Path(target_probe.__file__).read_text()
        assert json.loads(kwargs["input"])[0]["expected_alpn"] == ["h2"]
        return subprocess.CompletedProcess(cmd, 0, '[{"stage":"config"}]', "")

    monkeypatch.setattr(probe.subprocess, "run", run)
    assert probe.probe_targets_on_exit([TARGET], "192.0.2.1", "root", Path("/key")) == [
        {"stage": "config"}
    ]


def test_remote_payload_runs_without_pipelinectl_installation(tmp_path):
    result = subprocess.run(
        [sys.executable, "-I", "-c", Path(target_probe.__file__).read_text()],
        input=json.dumps([{**TARGET, "dest": "invalid"}]),
        text=True, capture_output=True, check=True, cwd=tmp_path,
    )
    assert json.loads(result.stdout)[0]["stage"] == "config"


def test_chain_probe_state_cannot_replace_direct(tmp_path):
    direct = {"selected": {"sni": "direct.example", "dest": "direct.example:443"}}
    trial = {"selected": {"sni": "chain.example", "dest": "chain.example:443"}}
    probe.write_reality_target(tmp_path, direct)
    probe.write_reality_target(tmp_path, trial, purpose="chain")
    assert probe.load_reality_target(tmp_path) == direct
    assert probe.load_reality_target(tmp_path, purpose="chain") == trial
    with pytest.raises(ValueError):
        probe.write_reality_target(tmp_path, trial, purpose="../direct")


def test_cli_chain_purpose_selects_trial_pool_and_state(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[2]
    monkeypatch.setattr(cli, "_cfg", lambda: {})
    monkeypatch.setattr(cli.paths, "repo_root", lambda: root)
    monkeypatch.setattr(cli.paths, "state_dir", lambda cfg: tmp_path)
    monkeypatch.setattr(cli.terraform, "terraform_output_json", lambda path: {
        "exit_ip": "192.0.2.1"
    })
    measured = Mock(return_value=[{
        **TARGET, "tls_ok": True, "alpn_ok": True, "eligible": True,
        "rtt_ms": 1, "alpn": "h2", "tls_version": "TLSv1.3",
    }])
    monkeypatch.setattr(cli.probe, "probe_targets_on_exit", measured)
    result = CliRunner().invoke(cli.app, ["probe-targets", "--purpose", "chain"])
    assert result.exit_code == 0, result.output
    assert {t["sni"] for t in measured.call_args.args[0]} == {
        "storage.yandex.net", "yastatic.net", "cdnvideo.ru", "okcdn.ru", "userapi.com",
    }
    assert not (tmp_path / "reality_target.json").exists()
    assert probe.load_reality_target(tmp_path, "chain")["purpose"] == "chain"

    # An unsuccessful trial must also preserve the live direct selection.
    direct = {"selected": {"sni": "direct.example", "dest": "direct.example:443"}}
    probe.write_reality_target(tmp_path, direct)
    measured.return_value = [{**TARGET, "tls_ok": False, "eligible": False, "stage": "tcp"}]
    result = CliRunner().invoke(cli.app, ["probe-targets", "--purpose", "chain"])
    assert result.exit_code == 1
    assert probe.load_reality_target(tmp_path) == direct
    trial = json.loads((tmp_path / "chain_reality_target.json").read_text())
    assert trial["selected"] is None
    assert trial["all_results"][0]["stage"] == "tcp"
