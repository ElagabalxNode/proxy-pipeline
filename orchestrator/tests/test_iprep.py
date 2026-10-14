from __future__ import annotations

from pipelinectl import iprep

PREVIOUS_TENANT = {  # what InternetDB showed for 84.32.104.161 on 2026-09-12 (NJRat C2 before us)
    "ip": "84.32.104.161",
    "ports": [22, 111, 137, 1099, 2078, 2087, 8888],
    "tags": [],
    "hostnames": ["ip-84-32-104-161.002.ptr.cherryservers.net"],
    "cpes": ["cpe:/a:openbsd:openssh:10.2p1"],
    "vulns": [],
}


def test_previous_tenant_ports_are_dirt() -> None:
    clean, reason = iprep.assess(PREVIOUS_TENANT)
    assert not clean and "ports [111, 137, 1099, 2078, 2087, 8888]" in reason


def test_only_our_ports_is_clean() -> None:
    clean, reason = iprep.assess(
        {"ports": [22, 443, 8443], "tags": [], "hostnames": ["x.ptr.cherryservers.net"]}
    )
    assert clean and "our own ports" in reason


def test_no_record_is_clean() -> None:
    assert iprep.assess(None) == (True, "no InternetDB record (never scanned or scrubbed)")


def test_bad_tags_and_foreign_hostnames() -> None:
    clean, reason = iprep.assess(
        {"ports": [22], "tags": ["Malware", "cloud"], "hostnames": ["holdmy4.myrightel.xyz"]}
    )
    assert not clean and "tags ['malware']" in reason and "holdmy4.myrightel.xyz" in reason


def test_allowed_ports_override() -> None:
    clean, _ = iprep.assess({"ports": [22, 9090]}, allowed_ports=frozenset({22, 9090}))
    assert clean


def test_single_stray_port_is_tolerated_but_two_are_not() -> None:
    clean, reason = iprep.assess({"ports": [22, 111], "tags": [], "hostnames": []})
    assert clean and "stray port [111]" in reason
    clean, _ = iprep.assess({"ports": [22, 111, 8888], "tags": [], "hostnames": []})
    assert not clean
