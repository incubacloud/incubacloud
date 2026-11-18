"""An S3 destination signs with the region it is set to.

wal-g and rclone, the continuous recovery's tools, sign every request with
a region, and providers such as OVH, Scaleway or AWS outside us-east-1
refuse one signed for another (``400 AuthorizationHeaderMalformed``). The
destination keeps the region (``s3_region``); empty, each reader keeps
what it did before: boto3 resolves its own, and continuous recovery signs
with ``auto`` behind an endpoint (Cloudflare R2) and ``us-east-1``
without one.
"""
from pathlib import Path
from unittest.mock import MagicMock, patch

import boto3
from botocore.paginate import Paginator
from lxml import etree

from odoo.exceptions import ValidationError
from odoo.http import Request

from odoo.addons.incubacloud.controllers._data_load import _routes_backends
from odoo.addons.incubacloud.models.backup_list_executor import BackupListExecutor
from odoo.addons.incubacloud.models.deploy_instance_executor import (
    DeployInstanceExecutor,
)
# The module, not its classes: a test class imported here would run here too.
from odoo.addons.incubacloud.tests import test_backup_dst_shell as _shell

# A real (offline-constructed) S3 client class to spec mocks against.
_S3_CLIENT_CLS = type(boto3.client(
    "s3", region_name="us-east-1",
    aws_access_key_id="x", aws_secret_access_key="y",
))

_COMPONENT = (
    Path(__file__).resolve().parent.parent
    / "static" / "src" / "components" / "backup_backend_detail"
)


def _env_lines(content):
    """The names of an environment file's lines, in order.

    :param str content: the file
    :rtype: list
    """
    return [line.split("=", 1)[0] for line in content.splitlines()]


def _empty_client():
    """A stand-in S3 client whose bucket answers and whose listing is empty.

    :return: the client
    """
    paginator = MagicMock(spec=Paginator)
    paginator.paginate.return_value = [{"Contents": []}]
    client = MagicMock(spec=_S3_CLIENT_CLS)
    client.get_paginator.return_value = paginator
    return client


class TestTheRegionIsADestinationField(_shell._BackupDstCase):

    def test_it_is_empty_by_default(self):
        self.assertFalse(self.backend.s3_region)

    def test_it_is_stored_without_the_spaces_around_it(self):
        self.backend.write({"s3_region": " eu-west-par "})
        self.assertEqual(self.backend.s3_region, "eu-west-par")
        other = self.env["cloud.backup.backend"].create({
            "name": "bd-other", "backend_type": "s3", "s3_bucket": "bd-other",
            "s3_region": "fr-par\n",
        })
        self.assertEqual(other.s3_region, "fr-par")

    def test_it_refuses_what_would_break_an_environment_line(self):
        for value in ("eu west", "eu-west\nDST=x", "$HOME", "eu'west", "a#b"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                self.backend.write({"s3_region": value})

    def test_it_takes_the_providers_spellings(self):
        for value in ("us-east-1", "eu-west-par", "fr-par", "auto", "us-west-004"):
            with self.subTest(value=value):
                self.backend.write({"s3_region": value})
                self.assertEqual(self.backend.s3_region, value)


class TestTheBackupContainerGetsTheRegion(_shell._BackupDstCase):

    def _deploy(self):
        """A deploy executor of ``self.instance``."""
        job = self.env["cloud.job"].create({
            "job_type_id": self.env.ref("incubacloud.deploy_instance").id,
            "instance_id": self.instance.id,
            "host_id": self.host.id,
        })
        return DeployInstanceExecutor(job, self.host)

    def test_set_backup_env_names_it_for_boto3(self):
        self.backend.write({"s3_region": "eu-west-par"})
        content = self._deploy()._backup_env_content()
        self.assertIn("AWS_DEFAULT_REGION=eu-west-par\n", content)

    def test_empty_backup_env_names_none(self):
        content = self._deploy()._backup_env_content()
        self.assertNotIn("AWS_DEFAULT_REGION", content)


class TestTheArchivedPurgeGetsTheRegion(_shell._BackupDstCase):

    _purge = _shell.TestThePurgeEnvironmentKeepsItsLines._purge

    def test_set_the_purge_names_it_for_boto3(self):
        self.backend.write({"s3_region": "eu-west-par"})
        content = self._purge()._env_content()
        self.assertIn("AWS_DEFAULT_REGION=eu-west-par\n", content)
        self.assertEqual(_env_lines(content)[-1], "AWS_DEFAULT_REGION")

    def test_empty_the_purge_names_none(self):
        self.assertNotIn("AWS_DEFAULT_REGION", self._purge()._env_content())


class TestThePanelsS3ClientsGetTheRegion(_shell._BackupDstCase):
    """The connection test and the usage measures sign as the tools do."""

    def setUp(self):
        super().setUp()
        self.controller = _routes_backends.BackendsMixin()
        self.controller._sec = lambda: self.env["cloud.security.mixin"]


    def _regions(self):
        """The ``region_name`` each of the panel's S3 clients is made with.

        :return: ``{reader: region_name or None}``
        :rtype: dict
        """
        regions = {}
        fake_req = MagicMock(spec=Request)
        fake_req.env = self.env
        with patch.object(
            boto3, "client", return_value=_empty_client(),
        ) as make, patch.object(_routes_backends, "request", fake_req):
            self.controller.cloud_test_backup_backend(self.backend.id)
            regions["test"] = make.call_args.kwargs.get("region_name")
            self.backend._measure_usage()
            regions["usage"] = make.call_args.kwargs.get("region_name")
            self.backend._measure_prefix("boto3+s3://bd-bucket/backups/x")
            regions["prefix"] = make.call_args.kwargs.get("region_name")
        return regions

    def test_set_every_client_signs_with_it(self):
        self.backend.write({"s3_region": "eu-west-par"})
        self.assertEqual(self._regions(), {
            "test": "eu-west-par", "usage": "eu-west-par", "prefix": "eu-west-par",
        })

    def test_empty_boto3_resolves_its_own(self):
        self.assertEqual(self._regions(), {
            "test": None, "usage": None, "prefix": None,
        })


class TestTheBackupListingGetsTheRegion(_shell._BackupDstCase):

    def _region(self):
        """The ``region_name`` the listing's size pass makes its client with."""
        job = self.env["cloud.job"].create({
            "name": "backup_list", "host_id": self.host.id,
            "instance_id": self.instance.id,
            "job_type_id": self.env.ref("incubacloud.backup_list").id,
        })
        with patch.object(boto3, "client", return_value=_empty_client()) as make:
            BackupListExecutor(job, self.host)._populate_sizes_from_s3()
        return make.call_args.kwargs.get("region_name")

    def test_set_the_listing_signs_with_it(self):
        self.backend.write({"s3_region": "eu-west-par"})
        self.assertEqual(self._region(), "eu-west-par")

    def test_empty_boto3_resolves_its_own(self):
        self.assertIsNone(self._region())


class TestTheRoutesCarryTheRegion(_shell._BackupDstCase):

    def setUp(self):
        super().setUp()
        self.controller = _routes_backends.BackendsMixin()
        self.controller._sec = lambda: self.env["cloud.security.mixin"]

    def _call(self, method, *args):
        fake_req = MagicMock(spec=Request)
        fake_req.env = self.env
        with patch.object(_routes_backends, "request", fake_req):
            return getattr(self.controller, method)(*args)

    def test_saved_and_read_back(self):
        self._call("cloud_save_backup_backend", self.backend.id, {
            "s3_region": "eu-west-par",
        })
        self.assertEqual(self.backend.s3_region, "eu-west-par")
        data = self._call("cloud_get_backup_backend", self.backend.id)
        self.assertEqual(data["s3_region"], "eu-west-par")

    def test_created_with_it(self):
        data = self._call("cloud_create_backup_backend", {
            "name": "bd-ovh", "backend_type": "s3", "s3_bucket": "bd-ovh",
            "s3_endpoint_url": "https://s3.eu-west-par.io.cloud.ovh.net",
            "s3_region": "eu-west-par",
        })
        backend = self.env["cloud.backup.backend"].browse(data["id"])
        self.assertEqual(backend.s3_region, "eu-west-par")

    def test_empty_reads_as_empty(self):
        data = self._call("cloud_get_backup_backend", self.backend.id)
        self.assertEqual(data["s3_region"], "")


class TestTheStorageTabOffersTheRegion(_shell._BackupDstCase):
    """The field sits by the endpoint, for S3 only, with what it is for."""

    def _template(self):
        return etree.parse(str(_COMPONENT / "backup_backend_detail.xml"))

    def test_an_s3_destination_s_storage_shows_the_field(self):
        tree = self._template()
        inputs = tree.xpath("//input[@id='bbd-region']")
        self.assertEqual(len(inputs), 1)
        field = inputs[0]
        self.assertEqual(field.get("t-att-value"), "state.form.s3_region")
        self.assertIn("onInput('s3_region'", field.get("t-on-input"))
        # In the Storage tab, in the block an SFTP destination does not show.
        storage = field.xpath("ancestor::t[@t-if=\"state.tab === 'storage'\"]")
        self.assertTrue(storage)
        block = field.xpath("ancestor::div[@t-if='!isSftp']")
        self.assertTrue(block)
        # Beside the endpoint, in the same block.
        self.assertTrue(block[0].xpath(".//input[@id='bbd-endpoint']"))
        self.assertEqual(
            tree.xpath("//label[@for='bbd-region']/text()")[0].strip(), "Region",
        )

    def test_the_field_says_what_it_is_for(self):
        tree = self._template()
        field = tree.xpath("//input[@id='bbd-region']")[0]
        hint_id = field.get("aria-describedby")
        self.assertTrue(hint_id)
        hint = tree.xpath(f"//*[@id='{hint_id}']")
        self.assertEqual(len(hint), 1)
        text = " ".join(hint[0].itertext()).lower()
        self.assertIn("continuous recovery", text)
        self.assertIn("auto", text)

    def test_the_form_loads_and_saves_it(self):
        source = (_COMPONENT / "backup_backend_detail.js").read_text()
        for line in ('s3_region: "",', 's3_region: b.s3_region || "",'):
            self.assertTrue(line in source, line)


class TestAnImportKeepsTheRegion(_shell._BackupDstCase):

    _import = _shell.TestAnImportWithABackupValueRefused._import

    def test_backup_env_s_region_is_the_destination_s(self):
        answer = self._import(
            {"backup_dst": "boto3+s3://bd-import/backups/bd-proj/bd-imported"},
            "AWS_ACCESS_KEY_ID=AKIAIMPORT\nAWS_SECRET_ACCESS_KEY=bd\n"
            "AWS_DEFAULT_REGION=eu-west-par\nPASSPHRASE=bd\n",
        )
        self.assertTrue(answer["ok"], answer)
        instance = self.env["cloud.instance"].browse(answer["instance_id"])
        self.assertEqual(instance.backup_backend_id.s3_region, "eu-west-par")
