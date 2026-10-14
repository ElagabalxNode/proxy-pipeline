"""Pre-change backups: contents, permissions, consistency of the panel database, verification."""

import json
import sqlite3
import subprocess
import sys
import tarfile
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from pipelinectl import backup, cli


@pytest.fixture
def site(tmp_path, monkeypatch):
    root, node = tmp_path / "repo", tmp_path / "node"
    state = root / "state"
    for path, text in (
        (root / "config/pipeline.yaml", "panel: {}\n"), (root / "terraform/terraform.tfstate", "{}"),
        (root / ".client-links", "vless://secret"), (state / "runtime.json", '{"exit_ip": "192.0.2.10"}'),
        (state / "bridge_runtime.json", '{"bridge_ip": "198.51.100.2", "bridge_ssh_user": "root"}'),
        (state / "probe/probe.jsonl", "bulky"), (state / ".rotate.lock", ""),
        (state / ".login.json", ""),  # empty scratch left by an old role: not backup damage
        (node / "etc/ssh/sshd_config", "PasswordAuthentication yes\n"),
        (node / "etc/ssh/sshd_config.d/50-cloud.conf", "MaxStartups 10:30:100\n"),
        (node / "xray/config.json", '{"inbounds": []}'),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    database = node / "x-ui.db"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE inbounds(id INTEGER, remark TEXT)")
        conn.execute("INSERT INTO inbounds VALUES (1, 'VLESS-Reality-Vision')")
    monkeypatch.setitem(backup.NODES, "exit", {
        "files": [str(node / "etc/ssh/sshd_config"), str(node / "etc/ssh/sshd_config.d/*"),
                  str(node / "xray/config.json"), str(node / "etc/nginx/nginx.conf")],
        "sqlite": [str(database)],
    })
    monkeypatch.setitem(backup.NODES, "bridge", {"files": [str(node / "xray/config.json")]})

    def runner(cmd, **kwargs):
        assert cmd[0] == "ssh" and "BatchMode=yes" in cmd
        if cmd[-2].endswith("@198.51.100.2"):
            return SimpleNamespace(returncode=255, stdout=b"", stderr=b"ssh: connect timed out\n")
        return subprocess.run(["bash", "-c", cmd[-1].replace("python3", sys.executable, 1)], **kwargs)

    nodes = [("192.0.2.10", "root", tmp_path / "key", "exit"),
             ("198.51.100.2", "root", tmp_path / "key", "bridge")]
    return root, state, node, nodes, runner


def names(path):
    with tarfile.open(path) as archive:
        return set(archive.getnames())


def test_backup_contents_permissions_and_partial_failure(site):
    root, state, node, nodes, runner = site
    target, manifest = backup.create(root, state, nodes, runner=runner, now=0)
    assert target == root / "backups/19700101T000000Z"
    assert target.stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in target.iterdir())
    assert names(target / "local.tar") == {
        "config/pipeline.yaml", "terraform/terraform.tfstate", ".client-links",
        "state/runtime.json", "state/bridge_runtime.json", "state/.login.json"}
    prefix = str(node).lstrip("/")
    assert names(target / "exit.tar") == {
        f"{prefix}/etc/ssh/sshd_config", f"{prefix}/etc/ssh/sshd_config.d/50-cloud.conf",
        f"{prefix}/xray/config.json", f"{prefix}/x-ui.db", "MISSING.json"}
    assert manifest["missing"]["exit"] == [str(node / "etc/nginx/nginx.conf")]
    assert manifest["failed"] == {"bridge": "ssh: connect timed out"}
    assert not (target / "bridge.tar").exists()
    assert backup.verify(target) == ["bridge: not backed up (ssh: connect timed out)"]
    with pytest.raises(FileExistsError):  # an existing backup is never overwritten
        backup.create(root, state, nodes, runner=runner, now=0)


def test_database_copy_is_a_consistent_sqlite_file(site, tmp_path):
    root, state, node, nodes, runner = site
    target, _ = backup.create(root, state, nodes[:1], runner=runner, now=0)
    with tarfile.open(target / "exit.tar") as archive:
        member = next(m for m in archive.getnames() if m.endswith("x-ui.db"))
        (tmp_path / "restored.db").write_bytes(archive.extractfile(member).read())
    with sqlite3.connect(tmp_path / "restored.db") as conn:
        assert conn.execute("SELECT remark FROM inbounds").fetchall() == [("VLESS-Reality-Vision",)]
    assert backup.verify(target) == []


@pytest.mark.parametrize("damage, message", [
    ("flip", "differs from the manifest"), ("truncate", "unreadable"),
    ("delete", "unreadable"), ("manifest", "manifest unreadable"),
])
def test_verify_detects_damage(site, damage, message):
    root, state, _, nodes, runner = site
    target, _ = backup.create(root, state, nodes[:1], runner=runner, now=0)
    archive = target / "exit.tar"
    if damage == "flip":
        data = bytearray(archive.read_bytes())
        data[700] ^= 0xFF  # inside the first member's data block
        archive.write_bytes(bytes(data))
    elif damage == "truncate":
        archive.write_bytes(archive.read_bytes()[:300])
    elif damage == "delete":
        archive.unlink()
    else:
        (target / "manifest.json").write_text("{")
    assert any(message in problem for problem in backup.verify(target))


def test_verify_checks_database_and_json_readability(site):
    root, state, node, nodes, runner = site
    (node / "x-ui.db").write_bytes(b"SQLite format 3\x00" + b"garbage" * 200)
    (node / "xray/config.json").write_text("{ truncated")
    # The online backup would refuse this file on the node; ship it as a plain file instead.
    backup.NODES["exit"]["files"].append(backup.NODES["exit"]["sqlite"].pop())
    target, _ = backup.create(root, state, nodes[:1], runner=runner, now=0)
    problems = backup.verify(target)
    assert any("x-ui.db" in p and ("SQLite" in p or "integrity" in p) for p in problems), problems
    assert any("config.json: not valid JSON" in p for p in problems)


def test_backup_command(site, monkeypatch):
    root, state, _, nodes, runner = site
    monkeypatch.setattr(cli, "_cfg", lambda: {})
    monkeypatch.setattr(cli.paths, "state_dir", lambda cfg: state)
    monkeypatch.setattr(cli.paths, "repo_root", lambda: root)
    create = backup.create
    monkeypatch.setattr(cli.backup, "create", lambda r, s, n: create(r, s, n, runner=runner))
    cli_runner = CliRunner()
    local = cli_runner.invoke(cli.app, ["backup", "--node", "local"])
    assert local.exit_code == 0, local.output
    assert "verified" in local.output
    saved = sorted((root / "backups").iterdir())[-1]
    assert sorted(p.name for p in saved.iterdir()) == ["local.tar", "manifest.json"]
    again = cli_runner.invoke(cli.app, ["backup", "--verify", str(saved)])
    assert again.exit_code == 0 and "verified" in again.output
    (saved / "local.tar").write_bytes(b"")
    broken = cli_runner.invoke(cli.app, ["backup", "--verify", str(saved)])
    assert broken.exit_code == 2 and "PROBLEM" in broken.output
    assert json.loads((saved / "manifest.json").read_text())["git"].keys() == {"sha", "dirty"}
