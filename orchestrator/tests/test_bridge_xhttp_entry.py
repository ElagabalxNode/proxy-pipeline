"""Client VLESS + XHTTP entry on RU (loopback, behind the future HTTPS proxy) and the whole
variant-2 data path on loopback with the real Xray:

    client Xray -- VLESS+XHTTP --> RU -- VLESS+XHTTP+REALITY (own chain identity) --> WORLD --> target

TLS termination by the reverse proxy is the only hop not exercised here.
"""

import json
import shutil
import socket
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import yaml
from jinja2 import Environment, StrictUndefined

from test_bridge_apply import apply_env as apply_env_fixture
from test_bridge_apply import commands, run_apply
from test_bridge_config import (
    PASSWORD, TEMPLATE, free_port, http_via_socks, render_bridge, running_xray,
)
from test_bridge_config import xray_binary as xray_binary_fixture
from test_bridge_support import support
from test_world_transaction import FakePanel, world

apply_env = apply_env_fixture
xray_binary = xray_binary_fixture

XHTTP_UUID = "0b7f3d2e-5c1a-4e8f-9a6b-2d4c6e8f0a1b"
XHTTP_PATH = "/a1b2c3d4e5f6a7b8c9d0e1f2"


def render(mode="xhttp", **extra):
    env = Environment(undefined=StrictUndefined)
    env.filters["to_json"] = json.dumps
    base = dict(
        bridge_chain_mode=mode, bridge_dns_servers=["77.88.8.8"],
        bridge_client_uuid="e11fd630-493a-4181-b514-8e462ce601e5",
        bridge_reality_dest="example.com:443", bridge_reality_sni="example.com",
        bridge_private_key="AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE",
        bridge_short_id="aabbccdd", bridge_ss_method="2022-blake3-aes-128-gcm",
        bridge_ss_password=PASSWORD, ss_method="2022-blake3-aes-128-gcm", ss_password=PASSWORD,
        ss_port=8388, exit_ip="127.0.0.1", chain_port=8443,
        chain_uuid="6aac169a-291c-49d8-85fc-cf3c4e22e7f2", exit_reality_sni="example.org",
        exit_public_key="AgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgI", exit_short_id="11223344",
        bridge_xhttp_enabled=True, bridge_xhttp_port=10443,
        bridge_xhttp_uuid=XHTTP_UUID, bridge_xhttp_path=XHTTP_PATH,
    )
    return json.loads(env.from_string(TEMPLATE.read_text()).render(**{**base, **extra}))


@pytest.mark.parametrize("mode", ["xhttp", "ss"])
def test_xhttp_entry_is_loopback_flowless_and_routed_only_to_world(mode):
    cfg = render(mode)
    inbounds = {i["tag"]: i for i in cfg["inbounds"]}
    entry = inbounds["vless-xhttp-in"]
    assert entry["listen"] == "127.0.0.1"
    assert entry["streamSettings"] == {
        "network": "xhttp", "security": "none",
        "xhttpSettings": {"mode": "auto", "path": XHTTP_PATH}}
    assert render(mode, bridge_xhttp_mode="packet-up")["inbounds"][0]["streamSettings"][
        "xhttpSettings"]["mode"] == "packet-up"
    client = entry["settings"]["clients"][0]
    assert client["flow"] == "" and entry["settings"]["decryption"] == "none"
    assert client["id"] != inbounds["vless-reality-in"]["settings"]["clients"][0]["id"]
    rules = cfg["routing"]["rules"]
    chain = [r for r in rules if r["outboundTag"] == "chain-to-exit"]
    private = next(r for r in rules if "geoip:private" in r.get("ip", []))
    assert len(chain) == 1 and "vless-xhttp-in" in chain[0]["inboundTag"]
    assert private["outboundTag"] == "block" and private["inboundTag"] == chain[0]["inboundTag"]
    assert rules.index(private) < rules.index(chain[0])
    direct = [r for r in rules if r["outboundTag"] == "direct"]
    assert [r["inboundTag"] for r in direct] == [["probe-direct-in"]]
    assert cfg["outbounds"][0]["protocol"] == "blackhole"
    if mode == "xhttp":
        user = next(o for o in cfg["outbounds"] if o["tag"] == "chain-to-exit")["settings"]["vnext"][0]["users"][0]
        assert user["id"] != client["id"] and user["flow"] == ""


def test_disabled_entry_leaves_the_config_exactly_as_before():
    assert render(bridge_xhttp_enabled=False) == render_bridge("xhttp")
    assert "vless-xhttp-in" not in json.dumps(render(bridge_xhttp_enabled=False))


def test_live_identity_reads_back_the_xhttp_entry_and_rejects_other_layouts(tmp_path):
    path = tmp_path / "config.json"
    cfg = render()
    path.write_text(json.dumps(cfg))
    found = support.read_identity(path, "live")
    assert (found["xhttp_uuid"], found["xhttp_path"]) == (XHTTP_UUID, XHTTP_PATH)
    path.write_text(json.dumps(render(bridge_xhttp_enabled=False)))
    assert "xhttp_uuid" not in support.read_identity(path, "live")

    def broken(change):
        damaged = render()
        change(next(i for i in damaged["inbounds"] if i["tag"] == "vless-xhttp-in"))
        path.write_text(json.dumps(damaged))
        with pytest.raises(ValueError, match="refusing key rotation"):
            support.read_identity(path, "live")

    broken(lambda e: e.update(listen="0.0.0.0"))  # a public plain-HTTP VLESS listener
    broken(lambda e: e["settings"]["clients"][0].update(flow="xtls-rprx-vision"))
    broken(lambda e: e["settings"]["clients"].append(dict(e["settings"]["clients"][0])))
    broken(lambda e: e["streamSettings"]["xhttpSettings"].update(path="/short"))
    broken(lambda e: e["settings"]["clients"][0].update(id="e11fd630-493a-4181-b514-8e462ce601e5"))


def test_apply_publishes_the_xhttp_entry_and_keeps_it_on_repeat(apply_env):
    root, _, _ = apply_env
    play = yaml.safe_load((root / "play.yml").read_text())
    with socket.socket() as listener:  # stands in for Xray's loopback XHTTP listener
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        play[0]["vars"].update(bridge_xhttp_enabled=True, bridge_xhttp_uuid=XHTTP_UUID,
                               bridge_xhttp_path=XHTTP_PATH,
                               bridge_xhttp_port=listener.getsockname()[1])
        (root / "play.yml").write_text(yaml.safe_dump(play))
        first = run_apply(root)
        assert first.returncode == 0, first.stdout + first.stderr
        assert XHTTP_UUID not in first.stdout + first.stderr
        config = json.loads((root / "config.json").read_text())
        assert support.read_identity(root / "config.json", "live")["xhttp_path"] == XHTTP_PATH
        assert any(i["tag"] == "vless-xhttp-in" for i in config["inbounds"])
        runtime = json.loads((root / "bridge_runtime.json").read_text())
        assert runtime["bridge_xhttp_uuid"] == XHTTP_UUID and runtime["bridge_xhttp_mode"] == "auto"
        backup = support.read_identity(root / "bridge_identity.json", "backup")
        assert (backup["xhttp_uuid"], backup["xhttp_path"]) == (XHTTP_UUID, XHTTP_PATH)
        restarts = commands(root).count("restart")
        assert run_apply(root).returncode == 0
        assert commands(root).count("restart") == restarts
    # Listener gone: activation must fail and the previous config/state must come back.
    saved = (root / "config.json").read_bytes()
    play[0]["vars"]["bridge_xhttp_path"] = "/ffffffffffffffffffffffff"
    (root / "play.yml").write_text(yaml.safe_dump(play))
    failed = run_apply(root)
    assert failed.returncode != 0 and "Bridge apply failed" in failed.stdout
    assert (root / "config.json").read_bytes() == saved


@pytest.mark.parametrize("mode", ["xhttp", "ss"])
def test_real_xray_accepts_the_entry(xray_binary, tmp_path, mode):
    cfg = render(mode)
    cfg["log"] = {"loglevel": "warning"}
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    result = subprocess.run(
        [str(xray_binary), "run", "-test", "-config", str(tmp_path / "config.json")],
        env={"XRAY_LOCATION_ASSET": str(xray_binary.parent), "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def keypair(binary):
    out = subprocess.run([str(binary), "x25519"], capture_output=True, text=True, check=True).stdout
    return support.parse_x25519(out)


def test_whole_path_client_xhttp_to_ru_to_world_over_reality_fails_closed(xray_binary, tmp_path):
    if not shutil.which("openssl"):
        pytest.skip("openssl is needed for the local REALITY target")
    hits = []

    class Target(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            body = b"world-test-response"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    target = ThreadingHTTPServer(("127.0.0.1", 0), Target)
    threading.Thread(target=target.serve_forever, daemon=True).start()

    # The site REALITY borrows its handshake from: any TLS 1.3 server answering for the SNI.
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
         "-nodes", "-days", "1", "-subj", "/CN=dest.test", "-keyout", str(tmp_path / "dest.key"),
         "-out", str(tmp_path / "dest.crt")], check=True, capture_output=True)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.load_cert_chain(tmp_path / "dest.crt", tmp_path / "dest.key")
    context.set_alpn_protocols(["h2", "http/1.1"])
    dest = socket.create_server(("127.0.0.1", 0))

    def serve_dest():
        while True:
            try:
                conn, _ = dest.accept()
            except OSError:
                return
            def handshake(sock):
                try:
                    with context.wrap_socket(sock, server_side=True) as tls:
                        tls.recv(1)
                except (OSError, ssl.SSLError):
                    pass
            threading.Thread(target=handshake, args=(conn,), daemon=True).start()
    threading.Thread(target=serve_dest, daemon=True).start()

    used = {target.server_address[1], dest.getsockname()[1]}

    def port():
        while (number := free_port()) in used:
            pass
        used.add(number)
        return number

    direct_keys, chain_keys = keypair(xray_binary), keypair(xray_binary)
    assert direct_keys["private_key"] != chain_keys["private_key"]
    identity = {
        "exit_ip": "127.0.0.1", "uuid": "e11fd630-493a-4181-b514-8e462ce601e5",
        "chain_uuid": "6aac169a-291c-49d8-85fc-cf3c4e22e7f2", **direct_keys, "short_id": "aabbccdd",
        "reality_sni": "direct.test", "reality_dest": f"127.0.0.1:{dest.getsockname()[1]}",
        "ss_method": "2022-blake3-aes-128-gcm", "ss_password": PASSWORD,
        "chain_path": "/chain-relay-test", "chain_private_key": chain_keys["private_key"],
        "chain_public_key": chain_keys["public_key"], "chain_short_id": "0123456789abcdef",
        "chain_reality_sni": "dest.test", "chain_reality_dest": f"127.0.0.1:{dest.getsockname()[1]}",
        "reality_port": port(), "ss_port": port(), "chain_port": port(),
    }
    world.validate_runtime(identity)
    # WORLD exactly as the managed-inbound transaction would create it on an empty panel.
    world_cfg = world.full_candidate(FakePanel().config(), {}, world.new_payloads(identity))
    for inbound in world_cfg["inbounds"]:
        inbound["listen"] = "127.0.0.1"
    world_cfg["dns"] = {"hosts": {"target.test": "127.0.0.1"}}
    world_cfg["outbounds"][0]["settings"] = {"domainStrategy": "UseIPv4"}

    ru = render(
        "xhttp", chain_port=identity["chain_port"], chain_path=identity["chain_path"],
        exit_public_key=identity["chain_public_key"], exit_short_id=identity["chain_short_id"],
        exit_reality_sni=identity["chain_reality_sni"],
    )
    ru["log"] = {"loglevel": "info"}
    ru["dns"] = {"hosts": {"target.test": "127.0.0.1"}}
    next(o for o in ru["outbounds"] if o["tag"] == "direct")["settings"] = {"domainStrategy": "UseIPv4"}
    ports = {}
    for inbound in ru["inbounds"]:
        inbound.update(listen="127.0.0.1", port=port())
        ports[inbound["tag"]] = inbound["port"]

    def client(uuid, path, socks):
        return {
            "log": {"loglevel": "info"},
            "inbounds": [{"listen": "127.0.0.1", "port": socks, "protocol": "socks",
                          "settings": {"auth": "noauth"}}],
            "outbounds": [{"protocol": "vless", "settings": {"vnext": [{
                "address": "127.0.0.1", "port": ports["vless-xhttp-in"],
                "users": [{"id": uuid, "encryption": "none", "flow": ""}]}]},
                "streamSettings": {"network": "xhttp", "security": "none",
                                   "xhttpSettings": {"mode": "stream-up", "path": path}}}],
        }

    user, stranger, lost = port(), port(), port()
    target_port = target.server_address[1]
    try:
        with running_xray(xray_binary, world_cfg, tmp_path / "world.json", identity["chain_port"]) as world_proc, \
                running_xray(xray_binary, ru, tmp_path / "ru.json", ports["vless-xhttp-in"]), \
                running_xray(xray_binary, client(XHTTP_UUID, XHTTP_PATH, user), tmp_path / "client.json", user), \
                running_xray(xray_binary, client("11111111-2222-4333-8444-555555555555", XHTTP_PATH, stranger),
                             tmp_path / "stranger.json", stranger), \
                running_xray(xray_binary, client(XHTTP_UUID, "/000000000000000000000000", lost),
                             tmp_path / "lost.json", lost):
            assert b"world-test-response" in http_via_socks(user, "target.test", target_port)
            assert len(hits) == 1
            # Wrong client UUID, wrong path, and a private destination never produce traffic.
            assert not http_via_socks(stranger, "target.test", target_port)
            assert not http_via_socks(lost, "target.test", target_port)
            assert not http_via_socks(user, "127.0.0.1", target_port, domain=False, host="127.0.0.1")
            assert len(hits) == 1
            world_proc.terminate()
            world_proc.wait(timeout=5)
            # WORLD is gone: the request fails; RU does not serve it from its own address,
            # although RU itself can still reach the target through the diagnostic inbound.
            assert not http_via_socks(user, "target.test", target_port)
            assert len(hits) == 1
            assert b"world-test-response" in http_via_socks(ports["probe-direct-in"], "target.test", target_port)
            assert len(hits) == 2
    finally:
        target.shutdown()
        target.server_close()
        dest.close()
