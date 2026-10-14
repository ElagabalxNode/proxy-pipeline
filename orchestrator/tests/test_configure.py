from pathlib import Path
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from pipelinectl import cli


@pytest.fixture
def configure_env(tmp_path, monkeypatch):
    monkeypatch.delenv("PROXY_PIPELINE_CHAIN_MODE", raising=False)
    monkeypatch.delenv("PROXY_PIPELINE_PANEL_PASS", raising=False)
    cfg = {"panel": {"pass": "test-only"}, "firewall": {"direct_policy": "public"}, "xray": {
        "bridge_release": {"version": "25.10.15", "archive_sha256": "a" * 64},
        "world_release": {"xui_version": "v2.6.0", "xray_version": "25.10.15",
                          "xray_sha256": "B" * 64},
    }}
    monkeypatch.setattr(cli, "_cfg", lambda: cfg)
    for name in ("terraform_dir", "ansible_dir", "state_dir"):
        monkeypatch.setattr(cli.paths, name, lambda cfg, n=name: tmp_path / n)
    state = Mock(return_value=[])
    outputs = Mock(return_value={"exit_ip": "203.0.113.1", "bridge_ip": "198.51.100.2"})
    inventory, playbook = Mock(), Mock()
    monkeypatch.setattr(cli.terraform, "terraform_state_list", state)
    monkeypatch.setattr(cli.terraform, "terraform_output_json", outputs)
    monkeypatch.setattr(cli.ansible_runner, "write_inventory", inventory)
    monkeypatch.setattr(cli.ansible_runner, "run_playbook", playbook)
    monkeypatch.setattr(cli.probe, "load_reality_target", lambda st: {
        "selected": {"sni": "test.example", "dest": "test.example:443"}
    })
    return cfg, state, outputs, inventory, playbook


@pytest.mark.parametrize("bridge_only", [True, False])
@pytest.mark.parametrize("mode", ["xhttp", "ss"])
def test_explicit_chain_reaches_ansible_without_changing_manual_ru_port(
    configure_env, monkeypatch, bridge_only, mode,
):
    _, state, _, _, playbook = configure_env
    # A stale provider in tfstate must not determine the manually selected RU.
    state.return_value = ["module.bridge.twc_server.bridge"]
    monkeypatch.setenv("PROXY_PIPELINE_CHAIN_MODE", "invalid-but-overridden")
    cli._do_configure(
        bridge_only=bridge_only, bridge_ip_override="198.51.100.8", chain_mode=mode,
    )
    extra = playbook.call_args.args[3]
    assert extra["bridge_chain_mode"] == mode
    assert extra["bridge_reality_port"] == 443
    if bridge_only:
        assert playbook.call_args.kwargs["limit"] == "bridge"
    state.assert_not_called()


def test_provider_defaults_and_chain_override_are_independent(configure_env, monkeypatch):
    _, state, _, _, _ = configure_env
    state.return_value = ["module.bridge.twc_server.bridge"]
    assert cli._bridge_transport(Path("unused")) == ("ss", 8443)
    monkeypatch.setenv("PROXY_PIPELINE_CHAIN_MODE", "xhttp")
    assert cli._bridge_transport(Path("unused")) == ("xhttp", 8443)
    assert cli._bridge_transport(Path("unused"), "ss", 443) == ("ss", 443)
    assert cli._bridge_chain_mode(Path("unused")) == "xhttp"
    state.side_effect = RuntimeError("no Terraform state yet")
    monkeypatch.delenv("PROXY_PIPELINE_CHAIN_MODE")
    assert cli._bridge_transport(Path("unused")) == ("xhttp", 443)


def test_cli_flag_over_env_and_port_over_config(configure_env, monkeypatch):
    cfg, _, _, _, playbook = configure_env
    cfg["xray"]["bridge_reality_port"] = 9443
    monkeypatch.setenv("PROXY_PIPELINE_CHAIN_MODE", "ss")
    runner = CliRunner()
    result = runner.invoke(cli.app, ["configure", "--bridge-only"])
    assert result.exit_code == 0, result.output
    assert playbook.call_args.args[3]["bridge_chain_mode"] == "ss"
    assert playbook.call_args.args[3]["bridge_reality_port"] == 9443
    result = runner.invoke(cli.app, [
        "configure", "--bridge-only", "--chain-mode", "xhttp", "--bridge-port", "443",
    ])
    assert result.exit_code == 0, result.output
    assert playbook.call_args.args[3]["bridge_chain_mode"] == "xhttp"
    assert playbook.call_args.args[3]["bridge_reality_port"] == 443


@pytest.mark.parametrize("mode", ["raw", "tls", "", "xhttpp"])
def test_invalid_env_fails_before_inventory_or_remote_actions(configure_env, monkeypatch, mode):
    _, state, outputs, inventory, playbook = configure_env
    monkeypatch.setenv("PROXY_PIPELINE_CHAIN_MODE", mode)
    result = CliRunner().invoke(cli.app, ["configure", "--bridge-only"])
    assert result.exit_code != 0
    assert "chain mode must be" in result.output
    for action in (state, outputs, inventory, playbook):
        action.assert_not_called()


@pytest.mark.parametrize("port", [0, 65536, -1, True, "wrong", 443.5])
def test_invalid_config_port_has_no_side_effects(configure_env, port):
    cfg, _, outputs, inventory, playbook = configure_env
    cfg["xray"]["bridge_reality_port"] = port
    result = CliRunner().invoke(cli.app, ["configure", "--bridge-only"])
    assert result.exit_code != 0
    for action in (outputs, inventory, playbook):
        action.assert_not_called()


def test_invalid_flag_does_not_fall_back_to_valid_env(configure_env, monkeypatch):
    _, _, _, inventory, playbook = configure_env
    monkeypatch.setenv("PROXY_PIPELINE_CHAIN_MODE", "ss")
    result = CliRunner().invoke(cli.app, ["configure", "--chain-mode", "raw"])
    assert result.exit_code != 0
    inventory.assert_not_called()
    playbook.assert_not_called()


@pytest.mark.parametrize("release", [None, {}, {"version": "latest"},
                                     {"version": "25.10.15", "archive_sha256": "bad"}])
@pytest.mark.parametrize("bridge_only", [True, False])
def test_release_pin_required_before_inventory_or_world_play(configure_env, release, bridge_only):
    cfg, _, _, inventory, playbook = configure_env
    cfg["xray"]["bridge_release"] = release
    result = CliRunner().invoke(cli.app, ["configure"] + (["--bridge-only"] if bridge_only else []))
    assert result.exit_code != 0
    assert "bridge_release" in result.output
    inventory.assert_not_called()
    playbook.assert_not_called()


@pytest.mark.parametrize("only", ["exit", "firewall"])
def test_release_pin_not_required_for_unrelated_plays(configure_env, only):
    cfg, _, outputs, _, playbook = configure_env
    cfg["xray"].pop("bridge_release")
    if only == "exit":
        outputs.return_value["bridge_ip"] = ""
    cli._do_configure(exit_firewall_only=only == "firewall", direct_only=only == "exit")
    assert "bridge_xray_version" not in playbook.call_args.args[3]


def test_release_pin_and_offline_archive_reach_ansible(configure_env, tmp_path):
    cfg, _, _, inventory, playbook = configure_env
    release = cfg["xray"]["bridge_release"]
    release["archive_path"] = str(tmp_path / "missing.zip")
    result = CliRunner().invoke(cli.app, ["configure"])
    assert result.exit_code != 0
    inventory.assert_not_called()
    archive = tmp_path / "Xray release.zip"
    archive.write_bytes(b"checksum is checked by Ansible before delivery")
    release["archive_path"] = str(archive)
    release["archive_sha256"] = "A" * 64
    cli._do_configure(bridge_only=True)
    extra = playbook.call_args.args[3]
    assert extra["bridge_xray_version"] == "25.10.15"
    assert extra["bridge_xray_archive_sha256"] == "a" * 64
    assert extra["bridge_xray_archive_path"] == str(archive)


def test_explicit_bootstrap_only_reaches_world_configure(configure_env):
    _, _, _, inventory, playbook = configure_env
    runner = CliRunner()
    result = runner.invoke(cli.app, ["configure", "--bootstrap-panel", "--bridge-only"])
    assert result.exit_code != 0
    inventory.assert_not_called()
    result = runner.invoke(cli.app, ["configure", "--bootstrap-panel"])
    assert result.exit_code == 0, result.output
    assert playbook.call_args.args[3]["panel_bootstrap"] is True
    result = runner.invoke(cli.app, ["configure"])
    assert result.exit_code == 0, result.output
    assert playbook.call_args.args[3]["panel_bootstrap"] is False


def test_bridge_only_refuses_uncommitted_world_state(configure_env):
    cfg, _, _, inventory, playbook = configure_env
    state = cli.paths.state_dir(cfg)
    state.mkdir()
    (state / ".world-transaction.json").write_text("{}")
    result = CliRunner().invoke(cli.app, ["configure", "--bridge-only"])
    assert result.exit_code != 0
    assert "unfinished WORLD transaction" in result.output
    inventory.assert_not_called()
    playbook.assert_not_called()


def test_firewall_policy_and_bridge_reach_every_world_play(configure_env):
    cfg, _, _, _, playbook = configure_env
    cfg["firewall"] = {"direct_policy": "Allowlist", "direct_cidrs": ["192.0.2.0/24", "2001:db8::1"],
                       "admin_cidrs": ["192.0.2.50"], "ssh_world": True}
    expected = {
        "exit_fw_bridge_ip": "198.51.100.2", "exit_fw_direct_only": False,
        "exit_fw_direct_policy": "allowlist",
        "exit_fw_direct_cidrs": ["192.0.2.0/24", "2001:db8::1"],
        "exit_fw_extra_admin_cidrs": ["192.0.2.50"], "exit_fw_ssh_world": True,
    }
    cli._do_configure()
    extra = playbook.call_args.args[3]
    assert playbook.call_args.args[1].name == "site.yml"
    assert {key: extra[key] for key in expected} == expected
    cli._do_configure(exit_firewall_only=True, bridge_ip_override="198.51.100.8")
    extra = playbook.call_args.args[3]
    assert playbook.call_args.args[1].name == "exit_firewall.yml"
    assert playbook.call_args.kwargs["limit"] == "exit"
    assert extra["exit_fw_bridge_ip"] == "198.51.100.8"
    assert extra["chain_port"] == 8443 and "panel_port" not in extra
    cli._do_configure(bridge_only=True)
    assert not any(key.startswith("exit_fw") for key in playbook.call_args.args[3])


@pytest.mark.parametrize("firewall", [
    None, {}, {"direct_policy": ""}, {"direct_policy": "open"}, "public",
    {"direct_policy": "allowlist"}, {"direct_policy": "allowlist", "direct_cidrs": "192.0.2.1"},
    {"direct_policy": "public", "admin_cidrs": ["0.0.0.0/0"]},
    {"direct_policy": "public", "admin_cidrs": ["admin.example"]},
    {"direct_policy": "closed", "ssh_world": "no"},
])
@pytest.mark.parametrize("firewall_only", [True, False])
def test_firewall_choice_is_required_before_inventory(configure_env, firewall, firewall_only):
    cfg, _, _, inventory, playbook = configure_env
    cfg["firewall"] = firewall
    result = CliRunner().invoke(
        cli.app, ["configure"] + (["--exit-firewall-only"] if firewall_only else []))
    assert result.exit_code != 0
    assert "firewall" in result.output
    inventory.assert_not_called()
    playbook.assert_not_called()
    # RU-only work does not depend on the WORLD firewall choice.
    assert CliRunner().invoke(cli.app, ["configure", "--bridge-only"]).exit_code == 0


def test_missing_bridge_needs_explicit_direct_only(configure_env):
    cfg, _, outputs, inventory, playbook = configure_env
    outputs.return_value["bridge_ip"] = ""
    runner = CliRunner()
    for args in (["configure"], ["configure", "--exit-firewall-only"]):
        result = runner.invoke(cli.app, args)
        assert result.exit_code != 0
        assert "--bridge-ip" in result.output and "direct-only" in result.output
    inventory.assert_not_called()
    playbook.assert_not_called()

    result = runner.invoke(cli.app, ["configure", "--direct-only"])
    assert result.exit_code == 0, result.output
    assert playbook.call_args.args[1].name == "exit_only.yml"
    extra = playbook.call_args.args[3]
    assert extra["exit_fw_direct_only"] is True and extra["exit_fw_bridge_ip"] == ""
    assert "bridge_xray_version" not in extra

    cfg["firewall"]["direct_only"] = True
    result = runner.invoke(cli.app, ["configure", "--exit-firewall-only"])
    assert result.exit_code == 0, result.output
    assert playbook.call_args.args[3]["exit_fw_direct_only"] is True


@pytest.mark.parametrize("case", ["terraform-bridge", "manual-bridge", "recorded-bridge",
                                  "closed", "bridge-only"])
def test_direct_only_never_overrides_a_known_bridge(configure_env, case):
    cfg, _, outputs, inventory, playbook = configure_env
    args = ["configure", "--direct-only"]
    if case != "terraform-bridge":
        outputs.return_value["bridge_ip"] = ""
    if case == "manual-bridge":
        args += ["--bridge-ip", "198.51.100.8"]
    elif case == "recorded-bridge":
        state = cli.paths.state_dir(cfg)
        state.mkdir()
        (state / "bridge_runtime.json").write_text('{"bridge_ip": "198.51.100.9"}')
    elif case == "closed":
        cfg["firewall"]["direct_policy"] = "closed"
    elif case == "bridge-only":
        args += ["--bridge-only"]
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code != 0, result.output
    inventory.assert_not_called()
    playbook.assert_not_called()


def test_firewall_rollback_runs_alone_and_needs_no_policy(configure_env):
    cfg, _, outputs, inventory, playbook = configure_env
    cfg.pop("firewall")
    cfg["xray"].pop("bridge_release")
    outputs.return_value["bridge_ip"] = ""
    runner = CliRunner()
    for bad in (["--exit-firewall-rollback", "../etc"],
                ["--exit-firewall-rollback", "latest", "--bridge-only"],
                ["--exit-firewall-rollback", "latest", "--exit-firewall-only"],
                ["--exit-firewall-rollback", "latest", "--bootstrap-panel"]):
        assert runner.invoke(cli.app, ["configure", *bad]).exit_code != 0
    inventory.assert_not_called()
    for name in ("latest", "20261013T101500Z", "20261013T101500Z-1"):
        result = runner.invoke(cli.app, ["configure", "--exit-firewall-rollback", name])
        assert result.exit_code == 0, result.output
        assert playbook.call_args.args[1].name == "exit_firewall.yml"
        assert playbook.call_args.args[3] == {"exit_fw_action": "rollback", "exit_fw_snapshot": name}
        assert playbook.call_args.kwargs["limit"] == "exit"


def test_preflight_validates_inputs_without_touching_nodes(configure_env, monkeypatch):
    cfg, _, outputs, inventory, playbook = configure_env
    monkeypatch.setattr(cli.probe, "load_reality_target", Mock(side_effect=FileNotFoundError))
    runner = CliRunner()
    outputs.return_value["exit_ip"] = ""  # a rotation checks inputs before the Exit exists
    for args in (["configure", "--preflight"], ["configure", "--preflight", "--bridge-only"]):
        result = runner.invoke(cli.app, args)
        assert result.exit_code == 0, result.output
        assert "preflight OK" in result.output
    cfg["panel"]["pass"] = ""
    assert runner.invoke(cli.app, ["configure", "--preflight"]).exit_code != 0
    assert runner.invoke(cli.app, ["configure", "--preflight", "--bridge-only"]).exit_code == 0
    cfg["panel"]["pass"] = "test-only"
    for key, section in (("firewall", cfg), ("bridge_release", cfg["xray"])):
        saved = section.pop(key)
        result = runner.invoke(cli.app, ["configure", "--preflight"])
        assert result.exit_code != 0 and key in result.output
        section[key] = saved
    inventory.assert_not_called()
    playbook.assert_not_called()


def test_world_release_pin_reaches_only_world_configure(configure_env):
    cfg, _, _, _, playbook = configure_env
    cli._do_configure()
    extra = playbook.call_args.args[3]
    assert extra["world_xui_version"] == "2.6.0"
    assert extra["world_xray_version"] == "25.10.15"
    assert extra["world_xray_sha256"] == "b" * 64
    for kwargs in ({"bridge_only": True}, {"exit_firewall_only": True},
                   {"exit_firewall_rollback": "latest"}):
        cli._do_configure(**kwargs)
        assert not any(key.startswith("world_") for key in playbook.call_args.args[3])


@pytest.mark.parametrize("release", [
    None, {}, "2.6.0",
    {"xui_version": "latest", "xray_version": "25.10.15", "xray_sha256": "b" * 64},
    {"xui_version": "2.6.0", "xray_version": "", "xray_sha256": "b" * 64},
    {"xui_version": "2.6.0", "xray_version": "25.10.15", "xray_sha256": "short"},
])
def test_world_release_pin_required_before_inventory_or_world_play(configure_env, release):
    cfg, _, _, inventory, playbook = configure_env
    cfg["xray"]["world_release"] = release
    runner = CliRunner()
    for args in (["configure"], ["configure", "--preflight"], ["configure", "--bootstrap-panel"]):
        result = runner.invoke(cli.app, args)
        assert result.exit_code != 0
        assert "world_release" in result.output
    inventory.assert_not_called()
    playbook.assert_not_called()
    for args in (["configure", "--bridge-only"], ["configure", "--exit-firewall-only"]):
        assert runner.invoke(cli.app, args).exit_code == 0
