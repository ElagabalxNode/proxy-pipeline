"""Controller-side WORLD firewall plan: validated inputs in, ufw rule arguments out.

Chain and direct ports have separate policies. Nothing is opened for the panel: its API
and UI are reached through SSH. Every refusal happens here, before ufw is touched.
"""

from __future__ import annotations

import ipaddress
import json
import sys

POLICIES = ("public", "allowlist", "closed")


class PlanError(Exception):
    pass


def host(value, label):
    try:
        return str(ipaddress.ip_address(str(value).strip()))
    except ValueError:
        raise PlanError(f"{label} is not an IP address: {value!r}") from None


def network(value, label):
    try:
        net = ipaddress.ip_network(str(value).strip(), strict=False)
    except ValueError:
        raise PlanError(f"{label} is not an IP address or CIDR: {value!r}") from None
    if net.prefixlen == 0:
        raise PlanError(f"{label} must not cover the whole internet; use the explicit public option")
    return str(net.network_address) if net.num_addresses == 1 else str(net)


def port(value, label):
    if isinstance(value, bool) or not str(value).isdecimal() or not 1 <= int(value) <= 65535:
        raise PlanError(f"{label} must be a port number")
    return int(value)


def addresses(values, label, parse):
    if not isinstance(values, list):
        raise PlanError(f"{label} must be a list")
    return [parse(v, label) for v in values]


def build(options):
    controller = host(options.get("controller_ip", ""), "controller IP (source of the SSH session)")
    bridges = addresses(options.get("bridge_ips", []), "bridge IP", host)
    direct_only = options.get("direct_only") is True
    policy = options.get("direct_policy")
    if policy not in POLICIES:
        raise PlanError("direct policy must be public, allowlist or closed; no default is applied")
    if direct_only and bridges:
        raise PlanError("direct-only mode conflicts with a bridge IP")
    if not direct_only and not bridges:
        raise PlanError("bridge IP is required; without a bridge select the explicit direct-only mode")
    if direct_only and policy == "closed":
        raise PlanError("direct-only with a closed direct policy leaves WORLD without clients")
    clients = addresses(options.get("direct_cidrs", []), "direct CIDR", network)
    if policy == "allowlist" and not clients:
        raise PlanError("allowlist direct policy needs at least one direct CIDR")
    admins = addresses(options.get("admin_cidrs", []), "admin CIDR", network)
    ssh = port(options.get("ssh_port", 22), "SSH port")
    reality, ss, chain = (port(options.get(k), k) for k in ("reality_port", "ss_port", "chain_port"))
    if len({ssh, reality, ss, chain}) != 4:
        raise PlanError("SSH, direct and chain ports must be distinct")

    rules, seen = [], set()

    def allow(source, number, proto, comment):
        if (source, number, proto) in seen or (None, number, proto) in seen:
            return
        seen.add((source, number, proto))
        target = ["allow", f"{number}/{proto}"] if source is None else [
            "allow", "from", source, "to", "any", "port", str(number), "proto", proto]
        rules.append(target + ["comment", comment])

    if options.get("ssh_world") is True:
        allow(None, ssh, "tcp", "ssh world")
    allow(controller, ssh, "tcp", "controller ssh")
    for source in admins:
        allow(source, ssh, "tcp", "admin ssh")
    for source in bridges:
        allow(source, ssh, "tcp", "bridge reachability check")

    # SS2022 carries UDP natively; REALITY and the XHTTP chain are TCP only.
    direct = [(reality, "tcp"), (ss, "tcp"), (ss, "udp")]
    if policy == "public":
        for number, proto in direct:
            allow(None, number, proto, "direct public")
    for source in bridges:
        allow(source, chain, "tcp", "bridge chain")
        for number, proto in direct:
            allow(source, number, proto, "bridge")
    allow(controller, chain, "tcp", "controller validate")
    for number, proto in direct:
        allow(controller, number, proto, "controller validate")
    if policy == "allowlist":
        for source in clients:
            for number, proto in direct:
                allow(source, number, proto, "direct allowlist")

    return {
        "rules": rules,
        "summary": {
            "controller": controller, "bridges": bridges, "direct_only": direct_only,
            "direct_policy": policy, "direct_ports": [reality, ss], "chain_port": chain,
            "direct_sources": {"public": ["any"], "allowlist": clients, "closed": []}[policy],
            "admin_sources": admins, "ssh_world": options.get("ssh_world") is True,
        },
    }


def main():
    try:
        print(json.dumps(build(json.load(sys.stdin))))
    except PlanError as exc:
        print(f"WORLD firewall plan refused: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
