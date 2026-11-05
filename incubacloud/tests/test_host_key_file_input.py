"""The SSH private key field of the host form is a file picker.

``onKeyFileChange`` reads ``ev.target.files``; on a plain text input that
is ``null``, so typing in the field raised a TypeError. The attribute was
lost once in a template reformat, so check the template structurally.
"""
import os
import xml.etree.ElementTree as ET

from odoo.modules.module import get_module_path
from odoo.tests.common import BaseCase


class TestHostKeyFileInput(BaseCase):
    """The ``onKeyFileChange`` input must be ``type="file"``."""

    def test_key_input_is_file_picker(self):
        """Every input wired to ``onKeyFileChange`` declares type=file."""
        path = os.path.join(
            get_module_path("incubacloud"),
            "static/src/components/host_detail/host_detail.xml",
        )
        tree = ET.parse(path)  # nosec B314 — this repo's own template
        inputs = [
            el for el in tree.iter("input")
            if el.get("t-on-change") == "onKeyFileChange"
        ]
        self.assertTrue(inputs, "key file input not found")
        for el in inputs:
            self.assertEqual(el.get("type"), "file")
