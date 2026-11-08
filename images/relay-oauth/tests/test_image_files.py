"""The image's fixed files: the plugin's configuration, supervisord's program and the Dockerfile."""

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


def test_base_is_the_relay_image_core_deploys():
    """DMS_IMAGE:DMS_VERSION is core's SMTP_RELAY_IMAGE: the two never move apart."""
    if not DEPLOY_EXECUTOR.exists():
        pytest.skip("not in a core checkout")
    found = re.search(r'^SMTP_RELAY_IMAGE = "([^"]+)"$', DEPLOY_EXECUTOR.read_text(),
                      flags=re.MULTILINE)
    if not found:
        pytest.skip("this core has no SMTP_RELAY_IMAGE yet (the relay pin, core 1.0.218)")
    args = _dockerfile_args()
    assert f"{args['DMS_IMAGE']}:{args['DMS_VERSION']}" == found.group(1)
