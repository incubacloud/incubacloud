#!/usr/bin/env python3
"""Render every variant of the hardening firewall ruleset to files.

The playbook writes one ``/etc/nftables.conf`` for every OS its
preflight admits, and each of those ships a different ``nft``. CI
parses what this renders with each of them, in that OS's container.
Kept free of Odoo so it runs anywhere with PyYAML and Jinja2; it
renders the template exactly as the module's own tests do.

Usage: ``render_firewall_ruleset.py <output directory>``
"""
import pathlib
import sys

import yaml
from jinja2 import Environment

PLAYBOOK = (
    pathlib.Path(__file__).resolve().parents[2]
    / "ansible" / "playbooks" / "host_hardening.yml"
)
BASE = {"ssh_port": 22222, "ic_effective_allowlist": "203.0.113.7, 198.51.100.4"}
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
#: Every shape the template can take: reached directly, with the
#: per-source cap, behind a CDN (with and without an IPv6 range), and
#: behind a CDN with a cap left on the host.
VARIANTS = {
    "direct": {},
    "direct_rate": {"ic_http_conn_rate": 50},
    "cdn": CDN,
    "cdn_rate": CDN | {"ic_http_conn_rate": 50},
    "cdn_v4_only": CDN_V4_ONLY,
}


def ruleset_template():
    """Return the ruleset the playbook's copy task writes, untouched."""
    for play in yaml.safe_load(PLAYBOOK.read_text()):
        for task in play.get("tasks") or []:
            copy = task.get("ansible.builtin.copy") or {}
            if "table inet filter" in copy.get("content", ""):
                return copy["content"]
    raise SystemExit(f"no ruleset task in {PLAYBOOK}")


def main(out_dir):
    """Write one ``<variant>.nft`` per variant into *out_dir*."""
    out = pathlib.Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    # trim_blocks as Ansible renders copy content; autoescape stays off
    # because this is an nftables ruleset, not HTML.
    template = Environment(  # nosec B701
        trim_blocks=True, autoescape=False,
    ).from_string(ruleset_template())
    for name, extra in VARIANTS.items():
        (out / f"{name}.nft").write_text(template.render(**BASE, **extra))
        print(f"rendered {name}.nft")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(sys.argv[1])
