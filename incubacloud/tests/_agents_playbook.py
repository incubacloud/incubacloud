"""Render the files host_observability.yml writes, as Ansible would.

Used by the Odoo tests of the agents' configuration and, standalone, by
``tests/shell/fluent_bit_lines.bats``, which runs the real Fluent Bit image
on the rendered files::

    python3 _agents_playbook.py OUTDIR

writes ``fluent-bit.yaml``, ``classify.lua`` and ``panel.lua`` there, with
the Loki output sent to a stand-in on ``loki:3100``.

Only what the templates use is reproduced: Jinja with Ansible's
``trim_blocks`` and the filters ``bool``, ``to_json`` and ``urlsplit``.
No Odoo import, so the script runs without it.
"""
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from jinja2 import Environment

_HERE = os.path.dirname(os.path.abspath(__file__))
PLAYBOOKS = os.path.join(os.path.dirname(_HERE), "ansible", "playbooks")
PLAYBOOK = os.path.join(PLAYBOOKS, "host_observability.yml")
CLASSIFY_LUA = os.path.join(PLAYBOOKS, "files", "fluent-bit", "classify.lua")

#: The panel this rendering describes: what F0's lines carry.
PANEL_FINGERPRINT = "SHA256:aya+WZfI0ITp74XMk0+4tbzGPANSnRr7f4Xa1l6JSCQ"


def _bool(value):
    """Ansible's ``bool`` filter, for the values the templates hand it.

    :param value: a boolean or its text.
    :rtype: bool
    """
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _to_json(value, ensure_ascii=True, **_kwargs):
    """Ansible's ``to_json`` filter.

    :param value: anything JSON can hold.
    :param bool ensure_ascii: escape what is not ASCII.
    :rtype: str
    """
    return json.dumps(value, ensure_ascii=ensure_ascii)


def _urlsplit(value, query=""):
    """Ansible's ``urlsplit`` filter, one part at a time.

    :param str value: a URL.
    :param str query: ``hostname``, ``port``, ``scheme`` or ``path``.
    :return: that part, None for a port the URL does not name.
    """
    return getattr(urlsplit(value), query)


def environment():
    """Return a Jinja environment that renders as Ansible does here.

    :rtype: jinja2.Environment
    """
    env = Environment(trim_blocks=True, autoescape=False)  # nosec B701
    env.filters.update(bool=_bool, to_json=_to_json, urlsplit=_urlsplit)
    return env


def tasks():
    """Return the playbook's tasks, those inside blocks included.

    :return: every task dict, in order.
    :rtype: list[dict]
    """
    with open(PLAYBOOK, encoding="utf-8") as fh:
        top = yaml.safe_load(fh)[0]["tasks"]
    flat = []
    for task in top:
        flat.append(task)
        for key in ("block", "rescue", "always"):
            flat.extend(task.get(key) or [])
    return flat


def task(name):
    """Return the task called *name*.

    :param str name: the task's name.
    :rtype: dict
    :raises StopIteration: when there is none.
    """
    return next(t for t in tasks() if t.get("name") == name)


def render(name, **variables):
    """Render the content of the ``copy`` task called *name*.

    :param str name: the task's name.
    :param variables: the playbook's variables it reads.
    :return: the file the task would write.
    :rtype: str
    """
    content = task(name)["ansible.builtin.copy"]["content"]
    return environment().from_string(content).render(**variables)


def defaults(**overrides):
    """Return a host's variables as the executor and the facts give them.

    :param overrides: values to change.
    :rtype: dict
    """
    values = {
        "ic_obs_dir": "/home/incubacloud/observability",
        "ic_host_name": "host-x",
        "ic_host_id": "7",
        "ic_account": "acct",
        "ic_remote_write_user": "acct",
        "ic_remote_write_token": "s3cret-token",
        "ic_logs_url": "https://m.example.com/lw/loki/api/v1/push",
        "ic_write_url": "https://m.example.com/w/api/v1/write",
        "ic_panel_key_fingerprints": [PANEL_FINGERPRINT],
        "ic_panel_ssh_user": "incubacloud",
        "ansible_user_uid": 1000,
        "ic_fluent_bit": True,
        "ic_fluent_bit_buffer": "64M",
        "ic_fluent_bit_mem_limit": "128m",
        "ic_fluent_bit_image": "fluent/fluent-bit:5.1.5",
        "ic_node_exporter_image": "node-exporter",
        "ic_cadvisor_image": "cadvisor",
        "ic_vmagent_image": "vmagent",
        "ic_has_traefik": False,
        "ic_instances": [],
    }
    values.update(overrides)
    return values


def fluent_bit_files(**overrides):
    """Return Fluent Bit's three files as the playbook writes them.

    :param overrides: variables to change (see :func:`defaults`).
    :return: ``{file name: content}``.
    :rtype: dict
    """
    variables = defaults(**overrides)
    classify = Path(CLASSIFY_LUA).read_text(encoding="utf-8")
    return {
        "fluent-bit.yaml": render(
            "Write the security log collector's configuration", **variables,
        ),
        "panel.lua": render(
            "Write what the classification knows about the panel", **variables,
        ),
        "classify.lua": classify,
    }


def main(outdir):
    """Write Fluent Bit's files into *outdir*, sending to ``loki:3100``.

    :param str outdir: an existing directory.
    """
    files = fluent_bit_files(ic_logs_url="http://loki:3100/loki/api/v1/push")
    for name, content in files.items():
        with open(os.path.join(outdir, name), "w", encoding="utf-8") as fh:
            fh.write(content)


if __name__ == "__main__":
    main(sys.argv[1])
