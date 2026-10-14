"""Pinned WORLD release: tfvars/pipeline consistency, archive proof, and the role's gate."""

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile

import pytest
import yaml
from typer.testing import CliRunner

from pipelinectl import cli, world_release
from test_bridge_apply import executable
from test_world_transaction import FILES, FakePanel, FakeRemote, world, world_remote
from test_world_transaction import options as options_fixture

options = options_fixture
XRAY = b"\x7fELF pinned xray build"
XRAY_SHA = hashlib.sha256(XRAY).hexdigest()


def make_archive(path, binaries=(("x-ui/bin/xray-linux-amd64", XRAY),)):
    with tarfile.open(path, "w:gz") as archive:
        for name, content in (("x-ui/x-ui", b"panel"), *binaries):
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def pinned(tmp_path, monkeypatch):
    source = tmp_path / "release.tar.gz"
    archive_sha = make_archive(source)
    cfg = {"xray": {"world_release": {
        "xui_version": "2.6.0", "xray_version": "25.10.15", "xray_sha256": XRAY_SHA}}}
    tfvars = {"xui_version": "v2.6.0", "xui_archive_sha256": archive_sha}
    monkeypatch.setattr(world_release.cherry, "tfvars", lambda cfg=None: tfvars)
    fetched = []

    def fetch(url, dest):
        fetched.append(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(source, dest)

    return cfg, tfvars, source, fetch, fetched


def test_verify_downloads_once_and_proves_both_pins(pinned, tmp_path):
    cfg, _, _, fetch, fetched = pinned
    cache = tmp_path / "artifacts"
    found = world_release.verify(cfg, cache, fetch=fetch)
    assert found["xray_sha256"] == XRAY_SHA and found["xui_version"] == "2.6.0"
    assert fetched == [
        "https://github.com/MHSanaei/3x-ui/releases/download/v2.6.0/x-ui-linux-amd64.tar.gz"]
    world_release.verify(cfg, cache, fetch=fetch)
    assert len(fetched) == 1  # a matching cached archive is reused
    # A cached file that no longer matches the pin is fetched again, never trusted.
    (cache / "x-ui-2.6.0-linux-amd64.tar.gz").write_bytes(b"tampered")
    world_release.verify(cfg, cache, fetch=fetch)
    assert len(fetched) == 2


@pytest.mark.parametrize("damage, message", [
    ("archive_sha", "differs from xui_archive_sha256"),
    ("xray_sha", "bundled Xray SHA256"),
    ("version", "3X-UI pin differs"),
    ("tfvars_missing", "xui_version in terraform.tfvars"),
    ("config_latest", "xray.world_release.xui_version"),
])
def test_verify_refuses_any_disagreement(pinned, tmp_path, damage, message):
    cfg, tfvars, _, fetch, _ = pinned
    if damage == "archive_sha":
        tfvars["xui_archive_sha256"] = "c" * 64
    elif damage == "xray_sha":
        cfg["xray"]["world_release"]["xray_sha256"] = "d" * 64
    elif damage == "version":
        tfvars["xui_version"] = "v2.6.1"
    elif damage == "tfvars_missing":
        del tfvars["xui_version"]
    else:
        cfg["xray"]["world_release"]["xui_version"] = "latest"
    with pytest.raises(ValueError, match=message):
        world_release.verify(cfg, tmp_path / "artifacts", fetch=fetch)


def test_archive_must_hold_exactly_one_xray(tmp_path):
    for name, binaries in (("none.tar.gz", ()), ("two.tar.gz", (
            ("x-ui/bin/xray-linux-amd64", XRAY), ("x-ui/bin/xray-linux-arm64", XRAY)))):
        make_archive(tmp_path / name, binaries)
        with pytest.raises(ValueError, match="exactly one"):
            world_release.inspect_archive(tmp_path / name)
    (tmp_path / "broken.tar.gz").write_bytes(b"not an archive")
    with pytest.raises(ValueError, match="not a readable"):
        world_release.inspect_archive(tmp_path / "broken.tar.gz")


def test_world_release_command(pinned, tmp_path, monkeypatch):
    cfg, tfvars, source, fetch, fetched = pinned
    monkeypatch.setattr(cli, "_cfg", lambda: cfg)
    monkeypatch.setattr(cli.paths, "state_dir", lambda cfg: tmp_path / "state")
    monkeypatch.setattr(world_release, "download", fetch)
    runner = CliRunner()
    offline = runner.invoke(cli.app, ["world-release"])
    assert offline.exit_code == 0 and "agree" in offline.output and not fetched
    shown = runner.invoke(cli.app, ["world-release", "--archive", str(source)])
    assert shown.exit_code == 0 and XRAY_SHA in shown.output
    assert tfvars["xui_archive_sha256"] in shown.output
    local = runner.invoke(cli.app, ["world-release", "--verify", "--archive", str(source)])
    assert local.exit_code == 0, local.output
    assert not fetched and not (tmp_path / "state/artifacts").exists()
    tfvars["xui_archive_sha256"] = "c" * 64
    bad = runner.invoke(cli.app, ["world-release", "--verify", "--archive", str(source)])
    assert bad.exit_code == 1 and "refuse to install" in bad.output


def test_transaction_refuses_an_unpinned_binary_before_any_write(options, tmp_path):
    panel, remote = FakePanel(), FakeRemote()  # FakeRemote reports sha256 "aaaa…"
    with pytest.raises(world.WorldError, match="differs from the pinned release"):
        world.transact({**options, "xray_sha256": "b" * 64, "xui_version": "2.6.0"},
                       tmp_path, panel, remote)
    assert not panel.calls and not (tmp_path / "runtime.json").exists()
    assert not (tmp_path / ".world-transaction.json").exists()
    world.transact({**options, "xray_sha256": "a" * 64, "xui_version": "2.6.0"},
                   tmp_path, panel, remote)
    assert json.loads((tmp_path / "runtime.json").read_text())["world_xui_version"] == "2.6.0"


PANEL = 'import sys\nassert sys.argv[1:] == ["-v"]\nprint("{version}")\n'
ENGINE = 'import sys\nassert sys.argv[1:] == ["version"]\nprint("Xray {version} (fixture) abc")\n'


@pytest.fixture
def node(tmp_path):
    assert shutil.which("ansible-playbook"), "install orchestrator[dev] for Ansible regression tests"
    shutil.copytree(FILES.parent, tmp_path / "roles/exit_xui_reality")
    executable(tmp_path / "x-ui", PANEL.format(version="2.6.0"))
    executable(tmp_path / "xray", ENGINE.format(version="25.10.15"))
    digest = hashlib.sha256((tmp_path / "xray").read_bytes()).hexdigest()
    variables = {
        "world_remote_helper": str(FILES / "world_remote.py"),
        "world_binary": str(tmp_path / "xray"), "world_panel_binary": str(tmp_path / "x-ui"),
        "world_xui_version": "2.6.0", "world_xray_version": "25.10.15", "world_xray_sha256": digest,
    }
    (tmp_path / "play.yml").write_text(yaml.safe_dump([{
        "hosts": "localhost", "connection": "local", "gather_facts": False, "vars": variables,
        "tasks": [{"ansible.builtin.import_role": {
            "name": "exit_xui_reality", "tasks_from": "release"}}],
    }]))
    (tmp_path / "ansible.cfg").write_text("[defaults]\nretry_files_enabled = False\n")
    return tmp_path


def run_gate(root, **overrides):
    (root / "overrides.json").write_text(json.dumps(overrides))
    return subprocess.run(
        ["ansible-playbook", "-i", "localhost,", "-e", f"@{root / 'overrides.json'}",
         "-e", f"ansible_python_interpreter={sys.executable}", str(root / "play.yml")],
        cwd=root, capture_output=True, text=True, timeout=90,
        env={**os.environ, "ANSIBLE_CONFIG": str(root / "ansible.cfg"),
             "ANSIBLE_LOCAL_TEMP": str(root / "ansible-tmp"), "ANSIBLE_NOCOLOR": "1"},
    )


def test_role_gate_accepts_only_the_pinned_release(node):
    ok = run_gate(node)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    executable(node / "x-ui", PANEL.format(version="v2.6.0"))  # a tag-style answer is the same pin
    assert run_gate(node).returncode == 0
    for overrides, shown in (
        ({"world_xui_version": "2.6.1"}, "Installed: 3X-UI v2.6.0"),
        ({"world_xray_version": "25.10.1"}, "Xray 25.10.15 (fixture)"),
        ({"world_xray_sha256": "e" * 64}, "Xray SHA256"),
    ):
        result = run_gate(node, **overrides)
        assert result.returncode != 0
        assert "nothing was changed" in result.stdout and shown in result.stdout
    for overrides in ({"world_xui_version": ""}, {"world_xray_sha256": "latest"}):
        result = run_gate(node, **overrides)
        assert result.returncode != 0 and "Set xray.world_release" in result.stdout


def test_remote_release_reports_versions_and_binary_checksum(tmp_path):
    executable(tmp_path / "x-ui", PANEL.format(version="2.6.0"))
    executable(tmp_path / "xray", ENGINE.format(version="25.10.15"))
    found = world_remote.execute({"action": "release", "binary": str(tmp_path / "xray"),
                                  "panel_binary": str(tmp_path / "x-ui")})
    assert found["xui"] == "2.6.0"
    assert found["xray"]["version"].startswith("Xray 25.10.15 ")
    assert found["xray"]["sha256"] == hashlib.sha256((tmp_path / "xray").read_bytes()).hexdigest()
