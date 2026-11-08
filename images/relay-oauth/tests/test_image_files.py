"""The image's fixed files: the plugin's configuration, supervisord's program and the Dockerfile."""

import ast
import json
import re
import urllib.parse

import pytest
from conftest import IMAGE_DIR

REPO = IMAGE_DIR.parent.parent
DEPLOY_EXECUTOR = REPO / "incubacloud" / "models" / "deploy_instance_executor.py"


def _dockerfile_args():
    """Return the Dockerfile's ``ARG NAME=default`` lines as a dict."""
    text = (IMAGE_DIR / "Dockerfile").read_text()
    return dict(re.findall(r"^ARG (\w+)=(\S*)$", text, flags=re.MULTILINE))


def test_plugin_never_logs_a_full_trace():
    """log_full_trace_on_failure = yes would write the access token to mail.log."""
    conf = json.loads((IMAGE_DIR / "sasl-xoauth2.conf").read_text())
    assert conf["log_full_trace_on_failure"] == "no"
    assert conf["always_log_to_syslog"] == "no"
    assert conf["log_to_syslog_on_failure"] == "yes"


def test_plugin_cannot_refresh_or_carry_the_secret():
    """The plugin's own refresh goes to a closed loopback port, with no client credentials."""
    conf = json.loads((IMAGE_DIR / "sasl-xoauth2.conf").read_text())
    # Both keys must exist (src/config.cc), and stay empty.
    assert conf["client_id"] == ""
    assert conf["client_secret"] == ""
    endpoint = urllib.parse.urlsplit(conf["token_endpoint"])
    assert endpoint.hostname == "127.0.0.1"
    assert endpoint.port == 9  # discard: nothing listens there in the image
    assert not {"proxy", "ca_bundle_file", "ca_certs_dir"} & set(conf)


def test_supervisord_program():
    """The renewer runs as root, always restarted, logging to supervisord's directory."""
    text = (IMAGE_DIR / "ic-relay-oauth.conf").read_text()
    assert "[program:ic-relay-oauth]" in text
    for line in ("command=/usr/local/bin/ic-relay-oauth run", "user=root",
                 "autostart=true", "autorestart=true"):
        assert line in text.splitlines()


def test_dockerfile_pins_the_plugin_by_commit():
    """The sasl-xoauth2 tag is checked against a full commit hash."""
    args = _dockerfile_args()
    assert re.fullmatch(r"release-\d+\.\d+", args["SX_TAG"])
    assert re.fullmatch(r"[0-9a-f]{40}", args["SX_COMMIT"])
    text = (IMAGE_DIR / "Dockerfile").read_text()
    assert 'test "$(git -C /src rev-parse HEAD)" = "${SX_COMMIT}"' in text


def test_dockerfile_only_xoauth2_towards_the_relay():
    """The final image asks the relay for XOAUTH2 only."""
    text = (IMAGE_DIR / "Dockerfile").read_text()
    assert "smtp_sasl_mechanism_filter = xoauth2" in text


def _core_relay_image(source):
    """Return the string ``SMTP_RELAY_IMAGE`` is assigned at the top level of ``source``.

    :param source: Python source of ``deploy_instance_executor.py``.
    :raises AssertionError: when the constant is not there or is not a string.
    """
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
        else:
            continue
        if any(isinstance(t, ast.Name) and t.id == "SMTP_RELAY_IMAGE" for t in targets):
            value = ast.literal_eval(node.value)
            assert isinstance(value, str), "SMTP_RELAY_IMAGE is not a string"
            return value
    raise AssertionError("SMTP_RELAY_IMAGE is not defined in the source")


def _base_image(args):
    """Return the ``image:version`` the Dockerfile's ARGs name."""
    return f"{args['DMS_IMAGE']}:{args['DMS_VERSION']}"


@pytest.mark.parametrize("source", [
    "SMTP_RELAY_IMAGE = 'a:1'",
    "SMTP_RELAY_IMAGE: str = 'a:1'",
    "X = 1\nSMTP_RELAY_IMAGE = (\n    'a:1'\n)\n",
])
def test_core_relay_image_is_read_from_the_syntax_tree(source):
    """The constant is found however it is written, not by a line's exact text."""
    assert _core_relay_image(source) == "a:1"


@pytest.mark.parametrize("source", [
    "",
    "OTHER = 'a:1'",
    "def f():\n    SMTP_RELAY_IMAGE = 'a:1'\n",
    "SMTP_RELAY_IMAGE = make()",
])
def test_core_relay_image_missing_or_not_a_string_is_an_error(source):
    """No constant at the top level, or one that is not a literal string, is a failure."""
    with pytest.raises((AssertionError, ValueError)):
        _core_relay_image(source)


def test_base_is_the_relay_image_core_deploys():
    """DMS_IMAGE:DMS_VERSION is core's SMTP_RELAY_IMAGE: the two never move apart.

    Skipped only outside a core checkout; inside one, a core without the
    constant, or one that names another release, fails.
    """
    if not DEPLOY_EXECUTOR.exists():
        pytest.skip("not in a core checkout")
    core = _core_relay_image(DEPLOY_EXECUTOR.read_text())
    assert _base_image(_dockerfile_args()) == core
