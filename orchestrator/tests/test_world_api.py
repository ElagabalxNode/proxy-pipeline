"""HTTP/CSRF and real Ansible command wiring over a simulated loopback SSH tunnel."""

import json
import os
import shutil
import socket
import subprocess
import threading
import urllib.parse
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import yaml

from test_bridge_apply import executable
from test_world_transaction import (
    FILES, PRIVATE, PUBLIC, FakePanel, FakeRemote, world,
)
from test_world_transaction import identity as identity_fixture
from test_world_transaction import options as options_fixture

identity = identity_fixture
options = options_fixture


@pytest.fixture
def api_env(identity, options):
    backend = FakePanel(world.new_payloads(identity))
    seen = []
    tokens = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, obj=None, *, code=200, success=True, cookie=None):
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(json.dumps({"success": success, "obj": obj,
                                        "msg": "secret-in-server-error"}).encode())

        def do_GET(self):
            seen.append(("GET", self.path))
            path = self.path.removeprefix(options["panel_path"])
            if path == "/csrf-token":
                token = f"token-{len(tokens)}"
                tokens.append(token)
                self.reply(token, cookie="session=ready; Path=/")
                return
            if "session=ready" not in self.headers.get("Cookie", ""):
                self.reply(code=401, success=False)
                return
            if path.endswith("/inbounds/list"):
                self.reply(backend.list())
            elif path.endswith("/server/getConfigJson"):
                self.reply(backend.config())
            elif path.endswith("/server/status"):
                self.reply({"xray": {"state": "running"}})
            else:
                self.reply(code=404, success=False)

        def do_POST(self):
            seen.append(("POST", self.path))
            path = self.path.removeprefix(options["panel_path"])
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if (not tokens or self.headers.get("X-CSRF-Token") != tokens[-1]
                    or "session=ready" not in self.headers.get("Cookie", "")):
                self.reply(code=403, success=False)
                return
            if path == "/login":
                parsed = urllib.parse.parse_qs(body.decode())
                self.reply(success=parsed.get("password") == [options["panel_pass"]])
                return
            try:
                if "/update/" in path:
                    backend.put(json.loads(body), int(path.rsplit("/", 1)[1]))
                elif path.endswith("/add"):
                    backend.put(json.loads(body))
                elif "/del/" in path:
                    backend.delete(int(path.rsplit("/", 1)[1]))
                else:
                    self.reply(code=404, success=False)
                    return
            except world.WorldError:
                self.reply(code=503, success=False)
                return
            self.reply()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}{options['panel_path']}"
    try:
        yield backend, world.Panel(base), server.server_port, seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_csrf_session_and_lost_http_response_reconcile_before_rollback(api_env, options, tmp_path):
    backend, panel, _, seen = api_env
    panel.login(options["panel_user"], options["panel_pass"])
    before = backend.list()
    backend.fail_at, backend.after_commit = 1, True
    with pytest.raises(world.WorldError, match="runtime restored") as error:
        world.transact({**options, "chain_port": 18443}, tmp_path, panel, FakeRemote())
    assert "secret-in-server-error" not in str(error.value)
    assert backend.rows == before
    writes = [path for method, path in seen if method == "POST" and "/update/" in path]
    assert len(writes) == 2  # one forward write, one recovery, never a blind retry
    assert not any("/del/" in path for _, path in seen)
    assert not (tmp_path / "runtime.json").exists()


def test_login_failure_is_sanitized_and_does_not_mutate(api_env):
    backend, panel, _, seen = api_env
    with pytest.raises(world.WorldError) as error:
        panel.login("admin", "wrong-password")
    assert "secret-in-server-error" not in str(error.value)
    assert not backend.calls


SSH = r'''
import json, select, shlex, socket, socketserver, subprocess, sys
args = sys.argv[1:]
assert "root@192.0.2.10" in args
if "-N" not in args:
    command = shlex.split(args[-1])
    data = sys.stdin.buffer.read()
    request = json.loads(data)
    if request["action"] == "ports":
        # Only process ownership is simulated here; exercise the TCP check for every port.
        for port in request["ports"]:
            with socket.create_connection(("127.0.0.1", port)):
                pass
        print('{"ok": true}')
        sys.exit(0)
    sys.exit(subprocess.run(command, input=data).returncode)
host, port, target, target_port = args[args.index("-L") + 1].split(":")
assert host == target == "127.0.0.1"
class Forward(socketserver.BaseRequestHandler):
    def handle(self):
        with socket.create_connection((target, int(target_port))) as remote:
            while True:
                ready, _, _ = select.select([self.request, remote], [], [], 5)
                if not ready:
                    return
                for source in ready:
                    data = source.recv(65536)
                    if not data:
                        return
                    (remote if source is self.request else self.request).sendall(data)
class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
with Server((host, int(port)), Forward) as server:
    server.serve_forever()
'''


def test_ansible_transaction_uses_tunnel_and_hides_credentials(api_env, identity, options, tmp_path):
    backend, _, api_port, _ = api_env
    executable(tmp_path / "ssh", SSH)
    executable(tmp_path / "xray", f'''
import json, sys
if sys.argv[1] == "x25519":
    print("PrivateKey: {PRIVATE}\\nPassword: {PUBLIC}")
elif sys.argv[1] == "version":
    print("Xray 25.10.15 (fixture)")
else:
    json.load(open(sys.argv[-1]))
''')
    roles = tmp_path / "roles"
    shutil.copytree(FILES.parent, roles / "exit_xui_reality")
    shutil.copytree(FILES.parents[1] / "bridge_xray_relay/files", roles / "bridge_xray_relay/files")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (tmp_path / "ansible.cfg").write_text("[defaults]\nretry_files_enabled = False\n")
    with ExitStack() as stack:
        for row, key in zip(backend.rows, ("reality_port", "ss_port", "chain_port")):
            sock = stack.enter_context(socket.socket())
            sock.bind(("127.0.0.1", 0))
            sock.listen(128)
            row["port"] = sock.getsockname()[1]
            options[key] = row["port"]
        variables = {
            "ansible_host": "192.0.2.10", "ansible_user": "root", "ansible_become": False,
            "ansible_ssh_private_key_file": str(tmp_path / "unused-ssh-key"),
            "pipeline_state_dir": str(state_dir), "world_remote_helper": str(FILES / "world_remote.py"),
            "world_binary": str(tmp_path / "xray"), "panel_request_prefix": options["panel_path"],
            **{k: v for k, v in options.items() if k not in ("exit_ip", "panel_path")},
            "panel_port": api_port,
        }
        (tmp_path / "play.yml").write_text(yaml.safe_dump([{
            "hosts": "localhost", "connection": "local", "gather_facts": False,
            "vars": variables, "tasks": [{"ansible.builtin.import_role": {
                "name": "exit_xui_reality", "tasks_from": "transaction",
            }}],
        }]))
        env = {**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
               "ANSIBLE_CONFIG": str(tmp_path / "ansible.cfg"),
               "ANSIBLE_LOCAL_TEMP": str(tmp_path / "ansible-tmp"), "ANSIBLE_NOCOLOR": "1"}
        for _ in range(2):
            proc = subprocess.run(
                ["ansible-playbook", "-i", "localhost,", "-vv", "--diff", str(tmp_path / "play.yml")],
                env=env, capture_output=True, text=True, timeout=90,
            )
            assert proc.returncode == 0, proc.stdout + proc.stderr
            assert PRIVATE not in proc.stdout + proc.stderr
            assert options["panel_pass"] not in proc.stdout + proc.stderr
        assert not backend.calls
        runtime = json.loads((state_dir / "runtime.json").read_text())
        assert runtime["uuid"] == identity["uuid"]
        assert runtime["exit_ip"] == "192.0.2.10"
        assert "panel_pass" not in runtime
        assert runtime["world_xray"]["version"] == "Xray 25.10.15 (fixture)"
