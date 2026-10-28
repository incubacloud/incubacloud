#!/usr/bin/env python3
"""Render every variant of the hardening firewall ruleset to files.

The playbook writes one ``/etc/nftables.conf`` for every OS its
preflight admits, and each of those ships a different ``nft``. CI
parses what this renders with each of them, in that OS's container.
Kept free of Odoo so it runs anywhere with PyYAML and Jinja2; the
module's own tests render the template through it.

The SSH lists the template reads are the facts the play's "Build the
effective SSH allowlist" task sets. They are evaluated here from that
task, from the configured list and the address the play connects from,
so what CI parses is what that task makes of them.

Usage: ``render_firewall_ruleset.py <output directory>``
"""
import pathlib
import sys

import yaml
from jinja2 import Environment
from jinja2.nativetypes import NativeEnvironment

PLAYBOOK = (
    pathlib.Path(__file__).resolve().parents[2]
    / "ansible" / "playbooks" / "host_hardening.yml"
)
#: The task whose facts hold the SSH allowlist.
ALLOWLIST_TASK = "Build the effective SSH allowlist"
BASE = {"ssh_port": 22222, "ssh_allowlist": "203.0.113.7, 198.51.100.4"}
CDN = {
    "ic_http_allowed_ranges": "203.0.113.0/24, 2001:db8::/32",
    "ic_http_allowed_ranges_v4": "203.0.113.0/24",
    "ic_http_allowed_ranges_v6": "2001:db8::/32",
}
CDN_V4_ONLY = {
    "ic_http_allowed_ranges": "203.0.113.0/24",
    "ic_http_allowed_ranges_v4": "203.0.113.0/24",
    "ic_http_allowed_ranges_v6": "",
}


def _connection(address):
    """Return ``ic_ssh_connection`` as the play registers it, from *address*.

    :param str address: the address the play connects from
    :rtype: dict
    """
    return {"stdout": f"{address} 51234 192.0.2.1 22222"}


#: Every shape the template can take: reached directly, with the
#: per-source cap, behind a CDN (with and without an IPv6 range), and
#: behind a CDN with a cap left on the host. ``direct_catchall`` is the
#: SSH list when no operator address is known: the catch-all overlaps
#: the control address, which the set only loads with ``auto-merge``.
#: The ``ssh_v6`` variants put IPv6 in the SSH list -- alone, among IPv4
#: addresses and networks, beside the catch-all, and as the address the
#: play connects from: an operator's IPv6 address in a host's list failed
#: its hardening (27-oct-2026).
VARIANTS = {
    "direct": {},
    "direct_catchall": {
        "ssh_allowlist": "0.0.0.0/0",
        "ic_ssh_connection": _connection("203.0.113.7"),
    },
    "direct_ssh_v6": {"ssh_allowlist": "2001:db8::7"},
    "direct_ssh_v6_mixed": {
        "ssh_allowlist":
            "203.0.113.7, 2001:db8::7, 198.51.100.0/24, 2001:db8:1::/48",
    },
    "direct_ssh_v6_catchall": {
        "ssh_allowlist": "0.0.0.0/0, 2001:db8::7",
        "ic_ssh_connection": _connection("203.0.113.7"),
    },
    "direct_ssh_v6_control": {
        "ssh_allowlist": "203.0.113.7",
        "ic_ssh_connection": _connection("2001:db8::99"),
    },
    "direct_rate": {"ic_http_conn_rate": 50},
    "cdn": CDN,
    "cdn_rate": CDN | {"ic_http_conn_rate": 50},
    "cdn_v4_only": CDN_V4_ONLY,
}


def _tasks():
    """Return the play's tasks, as the playbook has them."""
    return yaml.safe_load(PLAYBOOK.read_text())[0].get("tasks") or []


def ruleset_template():
    """Return the ruleset the playbook's copy task writes, untouched."""
    for task in _tasks():
        copy = task.get("ansible.builtin.copy") or {}
        if "table inet filter" in copy.get("content", ""):
            return copy["content"]
    raise SystemExit(f"no ruleset task in {PLAYBOOK}")


def allowlist_facts(variables):
    """Return the facts the allowlist task sets, given the play's *variables*.

    Its ``vars`` are evaluated in order, then its facts, with native
    types as Ansible templates them: a template that renders nothing (a
    family with no address) is ``None``, as ansible-core 2.21 makes it.

    :param dict variables: ``ssh_allowlist``, and ``ic_ssh_connection``
        when the play knows where it connects from
    :return: fact name to value
    :rtype: dict
    """
    task = next(t for t in _tasks() if t.get("name") == ALLOWLIST_TASK)
    env = NativeEnvironment()
    context = {"ic_ssh_connection": {"stdout": ""}} | dict(variables)

    def evaluate(expression):
        """Template *expression* against the context built so far."""
        return env.from_string(expression).render(**context)

    for name, expression in (task.get("vars") or {}).items():
        context[name] = evaluate(expression)
    return {
        name: evaluate(expression)
        for name, expression in task["ansible.builtin.set_fact"].items()
    }


def render(variables):
    """Return the ruleset the playbook writes for *variables*.

    :param dict variables: the play's variables (``ssh_port``,
        ``ssh_allowlist``, the CDN and rate ones); the allowlist facts
        are worked out from them
    :rtype: str
    """
    # trim_blocks as Ansible renders copy content; autoescape stays off
    # because this is an nftables ruleset, not HTML.
    return Environment(  # nosec B701
        trim_blocks=True, autoescape=False,
    ).from_string(ruleset_template()).render(
        **variables, **allowlist_facts(variables),
    )


def main(out_dir):
    """Write one ``<variant>.nft`` per variant into *out_dir*."""
    out = pathlib.Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for name, extra in VARIANTS.items():
        (out / f"{name}.nft").write_text(render(BASE | extra))
        print(f"rendered {name}.nft")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(sys.argv[1])
