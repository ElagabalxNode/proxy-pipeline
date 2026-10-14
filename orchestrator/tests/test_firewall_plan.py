"""WORLD firewall plan: separate chain/direct policies and refusals before ufw is touched."""

import importlib.util
from pathlib import Path

import pytest

FILES = Path(__file__).resolve().parents[2] / "ansible/roles/exit_firewall/files"


def load(name):
    spec = importlib.util.spec_from_file_location(name, FILES / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plan = load("firewall_plan")

CONTROLLER, BRIDGE = "203.0.113.7", "198.51.100.2"


@pytest.fixture
def options():
    return {"controller_ip": CONTROLLER, "bridge_ips": [BRIDGE], "direct_only": False,
            "direct_policy": "closed", "direct_cidrs": [], "admin_cidrs": [],
            "ssh_world": False, "ssh_port": 22,
            "reality_port": 443, "ss_port": 8388, "chain_port": 8443}


def sources(rules, port, proto="tcp"):
    """Who may reach a port: 'any' or the rule's source address."""
    found = set()
    for rule in rules:
        if rule[1] == f"{port}/{proto}":
            found.add("any")
        elif rule[1] == "from" and rule[6:9] == [str(port), "proto", proto]:
            found.add(rule[2])
    return found


def test_closed_policy_keeps_every_service_to_bridge_and_controller(options):
    rules = plan.build(options)["rules"]
    for port in (443, 8388, 8443):
        assert sources(rules, port) == {BRIDGE, CONTROLLER}
    assert sources(rules, 8388, "udp") == {BRIDGE, CONTROLLER}
    assert sources(rules, 22) == {BRIDGE, CONTROLLER}
    assert all(rule[0] == "allow" and rule[-2] == "comment" for rule in rules)


@pytest.mark.parametrize("policy, expected", [
    ("public", {"any"}),
    ("allowlist", {BRIDGE, CONTROLLER, "192.0.2.0/24", "2001:db8::5"}),
])
def test_direct_policy_never_widens_the_chain_port(options, policy, expected):
    options.update(direct_policy=policy, direct_cidrs=["192.0.2.9/24", "2001:db8::5/128"])
    result = plan.build(options)
    rules = result["rules"]
    assert sources(rules, 443) == expected
    assert sources(rules, 8388) == expected
    assert sources(rules, 8388, "udp") == expected
    assert sources(rules, 8443) == {BRIDGE, CONTROLLER}
    assert sources(rules, 8443, "udp") == set()
    assert sources(rules, 22) == {BRIDGE, CONTROLLER}
    assert result["summary"]["direct_policy"] == policy


def test_panel_port_is_never_opened_and_admins_get_only_ssh(options):
    options.update(admin_cidrs=["192.0.2.50", "2001:db8:1::/48"], panel_port=2053)
    rules = plan.build(options)["rules"]
    assert sources(rules, 22) == {BRIDGE, CONTROLLER, "192.0.2.50", "2001:db8:1::/48"}
    assert not any("2053" in arg for rule in rules for arg in rule)
    for port in (443, 8388, 8443):
        assert "192.0.2.50" not in sources(rules, port)


def test_ssh_world_and_duplicate_sources_produce_one_rule_each(options):
    options.update(ssh_world=True, admin_cidrs=[CONTROLLER, f"{CONTROLLER}/32"],
                   direct_policy="allowlist", direct_cidrs=[BRIDGE])
    rules = plan.build(options)["rules"]
    assert sources(rules, 22) == {"any"}
    assert len(rules) == len({tuple(rule[:-2]) for rule in rules})
    assert sources(rules, 443) == {BRIDGE, CONTROLLER}


def test_direct_only_is_explicit_and_has_no_bridge_rules(options):
    options.update(bridge_ips=[], direct_policy="public")
    with pytest.raises(plan.PlanError, match="direct-only"):
        plan.build(options)
    options["direct_only"] = True
    result = plan.build(options)
    assert sources(result["rules"], 8443) == {CONTROLLER}
    assert sources(result["rules"], 22) == {CONTROLLER}
    assert sources(result["rules"], 443) == {"any"}
    assert result["summary"]["bridges"] == []


@pytest.mark.parametrize("change, message", [
    ({"direct_policy": ""}, "no default"),
    ({"direct_policy": "open"}, "no default"),
    ({"direct_policy": "allowlist"}, "at least one"),
    ({"direct_only": True}, "conflicts"),
    ({"direct_only": "true", "bridge_ips": []}, "bridge IP is required"),
    ({"direct_only": True, "bridge_ips": []}, "without clients"),
    ({"controller_ip": ""}, "controller IP"),
    ({"controller_ip": "203.0.113.7 22"}, "controller IP"),
    ({"bridge_ips": ["198.51.100.0/24"]}, "bridge IP"),
    ({"bridge_ips": BRIDGE}, "must be a list"),
    ({"admin_cidrs": ["0.0.0.0/0"]}, "whole internet"),
    ({"direct_policy": "allowlist", "direct_cidrs": ["::/0"]}, "whole internet"),
    ({"direct_policy": "allowlist", "direct_cidrs": ["example.com"]}, "not an IP"),
    ({"chain_port": 443}, "distinct"),
    ({"chain_port": 22}, "distinct"),
    ({"ss_port": 0}, "port number"),
    ({"reality_port": "443; ufw disable"}, "port number"),
    ({"chain_port": True}, "port number"),
])
def test_unsafe_or_ambiguous_input_is_refused(options, change, message):
    options.update(change)
    with pytest.raises(plan.PlanError, match=message):
        plan.build(options)


def test_ipv6_controller_and_string_ports_are_supported(options):
    options.update(controller_ip="2001:db8::10", ssh_port="2222", chain_port="9443")
    rules = plan.build(options)["rules"]
    assert sources(rules, 2222) == {BRIDGE, "2001:db8::10"}
    assert sources(rules, 9443) == {BRIDGE, "2001:db8::10"}
