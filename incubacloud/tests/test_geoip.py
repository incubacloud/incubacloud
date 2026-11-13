"""The country of an address, from Odoo's GeoIP database or DB-IP's.

``models/geoip.py`` (plan #84, cut C3, B4 of Fable's review). No test
reaches the network: ``requests.get`` answers what each test gives it,
and the databases are built here, small MaxMind files with a few
networks, by :func:`_mmdb`.
"""
import gzip
import ipaddress
import os
import shutil
import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

from odoo.tests.common import TransactionCase
from odoo.tools import config

from odoo.addons.incubacloud.models import geoip

NOV = date(2026, 11, 13)
URL_NOV = "https://download.db-ip.com/free/dbip-country-lite-2026-11.mmdb.gz"
URL_OCT = "https://download.db-ip.com/free/dbip-country-lite-2026-10.mmdb.gz"
_GET = "odoo.addons.incubacloud.models.geoip.requests.get"

# MaxMind DB data types (MaxMind DB format, version 2.0).
_UTF8, _MAP, _UINT16, _UINT32, _UINT64, _ARRAY, _BOOL = 2, 7, 5, 6, 9, 11, 14


def _control(kind, size):
    """Return the control byte(s) of a value of *kind* and *size*.

    :param int kind: the data type; above 7 it is an extended type.
    :param int size: the value's size field.
    :rtype: bytes
    """
    if size < 29:
        first, extra = size, b""
    elif size < 285:
        first, extra = 29, bytes([size - 29])
    else:
        first, extra = 30, (size - 285).to_bytes(2, "big")
    if kind <= 7:
        return bytes([(kind << 5) | first]) + extra
    return bytes([first, kind - 7]) + extra


def _encode(value):
    """Encode *value* in the MaxMind DB data format.

    :param value: str, bool, dict, list, or ``(type, int)`` for an
        unsigned integer of that type.
    :rtype: bytes
    """
    if isinstance(value, bool):
        return _control(_BOOL, int(value))
    if isinstance(value, str):
        raw = value.encode()
        return _control(_UTF8, len(raw)) + raw
    if isinstance(value, dict):
        return _control(_MAP, len(value)) + b"".join(
            _encode(key) + _encode(item) for key, item in value.items()
        )
    if isinstance(value, list):
        return _control(_ARRAY, len(value)) + b"".join(map(_encode, value))
    kind, number = value
    raw = number.to_bytes((number.bit_length() + 7) // 8, "big")
    return _control(kind, len(raw)) + raw


def _mmdb(networks, database_type="DBIP-Country-Lite"):
    """Build an IPv6 MaxMind database with 24-bit records.

    IPv4 networks sit under ``::/96``, where readers look them up.

    :param dict networks: ``{"8.8.8.0/24": "US", ...}``.
    :param str database_type: the metadata's ``database_type``.
    :rtype: bytes
    """
    data = b""
    nodes = [[None, None]]
    for text, iso in networks.items():
        network = ipaddress.ip_network(text)
        width, number = network.max_prefixlen, int(network.network_address)
        bits = [(number >> (width - 1 - i)) & 1 for i in range(network.prefixlen)]
        if network.version == 4:
            bits = [0] * 96 + bits
        offset = len(data)
        data += _encode({"country": {"iso_code": iso, "names": {"en": iso}}})
        node = 0
        for bit in bits[:-1]:
            if nodes[node][bit] is None:
                nodes.append([None, None])
                nodes[node][bit] = len(nodes) - 1
            node = nodes[node][bit]
        nodes[node][bits[-1]] = ("data", offset)
    count = len(nodes)

    def record(item):
        """Return a record's value: a node, no data, or a data pointer."""
        if item is None:
            return count
        if isinstance(item, tuple):
            return count + 16 + item[1]
        return item

    tree = b"".join(
        record(left).to_bytes(3, "big") + record(right).to_bytes(3, "big")
        for left, right in nodes
    )
    metadata = {
        "binary_format_major_version": (_UINT16, 2),
        "binary_format_minor_version": (_UINT16, 0),
        "build_epoch": (_UINT64, 1793501259),
        "database_type": database_type,
        "description": {"en": "test"},
        "ip_version": (_UINT16, 6),
        "languages": ["en"],
        "node_count": (_UINT32, count),
        "record_size": (_UINT16, 24),
    }
    return (
        tree + bytes(16) + data + b"\xab\xcd\xefMaxMind.com"
        + _encode(metadata)
    )


def _response(status=200, body=b"", headers=None, chunks=None):
    """Return a streamed ``requests.Response`` stand-in.

    :param int status: its status code.
    :param bytes body: its body, sent in one chunk unless *chunks*.
    :param dict headers: its headers.
    :param list chunks: the chunks ``iter_content`` yields.
    """
    response = MagicMock(spec=requests.Response)
    response.status_code = status
    response.headers = dict(headers or {})
    response.iter_content.return_value = iter(
        chunks if chunks is not None else [body],
    )
    return response


class TestGeoip(TransactionCase):
    """Odoo's database first, else DB-IP's, downloaded within its caps."""

    def setUp(self):
        """No Odoo database unless a test gives one, and no reader held."""
        super().setUp()
        self.settings = self.env["cloud.settings"]._get_system()
        self.settings.security_geoip = True
        self.tmp = tempfile.mkdtemp(prefix="test-geoip-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.startPatcher(patch.dict(config.options.maps[0], {
            "geoip_country_db": os.path.join(self.tmp, "missing.mmdb"),
        }))
        self.addCleanup(geoip._READERS.clear)
        geoip._READERS.clear()
        self.env["ir.attachment"].sudo().search([
            ("res_model", "=", "cloud.settings"),
            ("name", "=like", "dbip-country-lite-%.mmdb"),
        ]).unlink()

    def _odoo_database(self, networks):
        """Give Odoo a country database with *networks*.

        :param dict networks: as :func:`_mmdb`.
        """
        path = Path(self.tmp, "GeoLite2-Country.mmdb")
        path.write_bytes(_mmdb(networks, "GeoLite2-Country"))
        config.options.maps[0]["geoip_country_db"] = str(path)

    def _stored(self):
        """Return the stored DB-IP attachment."""
        return geoip._stored(self.env)

    def _refresh(self, *answers, today=NOV):
        """Run :func:`geoip.refresh` with ``requests.get`` answering *answers*.

        :returns: ``(result, get mock)``.
        """
        with patch(_GET, side_effect=list(answers)) as get:
            result = geoip.refresh(self.env, today=today)
        return result, get

    def _store_october(self):
        """Store October's database, where 8.8.8.0/24 is in Spain."""
        result, _get = self._refresh(
            _response(body=gzip.compress(_mmdb({"8.8.8.0/24": "ES"}))),
            today=date(2026, 10, 5),
        )
        self.assertEqual(result, "stored")

    # -- Which database ---------------------------------------------------

    def test_odoo_database_first_and_no_download(self):
        """With Odoo's database, the lookup reads it and nothing is fetched."""
        self._odoo_database({"8.8.8.0/24": "US", "2a00:1450::/32": "DE"})
        with patch(_GET) as get:
            self.assertEqual(self.settings._cron_geoip_refresh(), "odoo")
        get.assert_not_called()
        self.assertFalse(self._stored())
        lookup = geoip.resolver(self.env)
        self.assertEqual(lookup.source, "odoo")
        self.assertIsNone(lookup.attribution)
        self.assertEqual(lookup("8.8.8.8"), "US")
        self.assertEqual(lookup("2a00:1450:4001::1"), "DE")
        self.assertIsNone(lookup("9.9.9.9"))

    def test_odoo_database_broken_falls_back_to_dbip(self):
        """An Odoo database that does not open counts as none."""
        path = Path(self.tmp, "broken.mmdb")
        path.write_bytes(b"not a database")
        config.options.maps[0]["geoip_country_db"] = str(path)
        self._store_october()
        self.assertEqual(geoip.country(self.env, "8.8.8.8"), "ES")

    def test_download_validates_and_stores(self):
        """Without Odoo's database, this month's file is fetched and used."""
        body = gzip.compress(_mmdb({"8.8.8.0/24": "ES"}))
        result, get = self._refresh(_response(
            body=body, headers={"Content-Length": str(len(body))},
        ))
        self.assertEqual(result, "stored")
        get.assert_called_once_with(
            URL_NOV, stream=True, allow_redirects=False,
            timeout=(geoip.CONNECT_TIMEOUT, geoip.READ_TIMEOUT),
        )
        stored = self._stored()
        self.assertEqual(stored.name, "dbip-country-lite-2026-11.mmdb")
        self.assertEqual(stored.res_id, self.settings.id)
        lookup = geoip.resolver(self.env)
        self.assertEqual(lookup.source, "dbip")
        self.assertEqual(lookup.attribution, geoip.ATTRIBUTION)
        self.assertIn("CC BY 4.0", lookup.attribution)
        self.assertEqual(lookup("8.8.8.8"), "ES")
        self.assertEqual(lookup("::ffff:8.8.8.8"), "ES")

    def test_month_stored_is_not_downloaded_again(self):
        """A month already stored makes the pass do nothing."""
        self._refresh(_response(body=gzip.compress(_mmdb({"8.8.8.0/24": "ES"}))))
        result, get = self._refresh()
        self.assertEqual(result, "current")
        get.assert_not_called()

    def test_attachment_in_the_database_opens_from_a_temp_file(self):
        """With attachments in the database, the lookup still answers."""
        self.env["ir.config_parameter"].sudo().set_param(
            "ir_attachment.location", "db",
        )
        self._store_october()
        stored = self._stored()
        self.assertFalse(stored.store_fname)
        self.assertEqual(geoip.country(self.env, "8.8.8.8"), "ES")
        temp = geoip._READERS[self.env.cr.dbname][3]
        self.assertTrue(temp and os.path.isfile(temp))
        self.addCleanup(geoip._remove, temp)

    def test_a_new_file_replaces_the_reader(self):
        """After a new month is stored, the lookup reads the new file."""
        self._store_october()
        self.assertEqual(geoip.country(self.env, "8.8.8.8"), "ES")
        self._refresh(_response(body=gzip.compress(_mmdb({"8.8.8.0/24": "PT"}))))
        self.assertEqual(geoip.country(self.env, "8.8.8.8"), "PT")

    # -- The previous month -------------------------------------------------

    def test_previous_month_while_this_one_is_missing(self):
        """A 404 for this month takes the previous one, once."""
        result, get = self._refresh(
            _response(status=404),
            _response(body=gzip.compress(_mmdb({"8.8.8.0/24": "ES"}))),
        )
        self.assertEqual(result, "stored")
        self.assertEqual([c.args[0] for c in get.call_args_list], [URL_NOV, URL_OCT])
        self.assertEqual(self._stored().name, "dbip-country-lite-2026-10.mmdb")
        # The next day November is still missing: October is not fetched again.
        result, get = self._refresh(_response(status=404))
        self.assertEqual(result, "current")
        get.assert_called_once()

    def test_january_falls_back_to_december(self):
        """The month before January is December of the year before."""
        self.assertEqual(
            geoip._months(date(2027, 1, 2)), ["2027-01", "2026-12"],
        )

    # -- Refused downloads keep the stored file ------------------------------

    def test_corrupt_file_does_not_replace_the_stored_one(self):
        """A file that does not open as a country database is dropped."""
        self._store_october()
        for body in (
            gzip.compress(b"garbage" * 100),
            gzip.compress(_mmdb({"8.8.8.0/24": "US"}, "GeoLite2-ASN")),
            b"not gzip at all",
            gzip.compress(_mmdb({"8.8.8.0/24": "US"}))[:-20],
        ):
            result, _get = self._refresh(_response(body=body))
            self.assertEqual(result, "failed")
            self.assertEqual(self._stored().name, "dbip-country-lite-2026-10.mmdb")
        self.assertEqual(geoip.country(self.env, "8.8.8.8"), "ES")

    def test_over_the_size_cap_is_refused(self):
        """A declared or a received size over the cap, or a gzip bomb."""
        self._store_october()
        too_big = str(geoip.MAX_DOWNLOAD_BYTES + 1)
        response = _response(headers={"Content-Length": too_big})
        result, _get = self._refresh(response)
        self.assertEqual(result, "failed")
        response.iter_content.assert_not_called()
        with patch.object(geoip, "MAX_DOWNLOAD_BYTES", 1000):
            result, _get = self._refresh(
                _response(chunks=[b"x" * 600, b"x" * 600, b"x" * 600]),
            )
        self.assertEqual(result, "failed")
        with patch.object(geoip, "MAX_DATABASE_BYTES", 1000):
            result, _get = self._refresh(
                _response(body=gzip.compress(bytes(100_000))),
            )
        self.assertEqual(result, "failed")
        self.assertEqual(self._stored().name, "dbip-country-lite-2026-10.mmdb")

    def test_over_the_time_cap_is_refused(self):
        """A download still running past its deadline is dropped."""
        self._store_october()
        with patch.object(geoip, "DOWNLOAD_SECONDS", -1):
            result, _get = self._refresh(_response(
                body=gzip.compress(_mmdb({"8.8.8.0/24": "US"})),
            ))
        self.assertEqual(result, "failed")
        self.assertEqual(self._stored().name, "dbip-country-lite-2026-10.mmdb")

    def test_redirect_to_another_host_is_refused(self):
        """A redirect off DB-IP's host is not followed; one on it is."""
        self._store_october()
        for location in (
            "https://evil.example/dbip.mmdb.gz",
            "http://download.db-ip.com/free/dbip-country-lite-2026-11.mmdb.gz",
            "https://download.db-ip.com.evil.example/x.mmdb.gz",
            "https://download.db-ip.com:8443/x.mmdb.gz",
        ):
            result, get = self._refresh(_response(
                status=302, headers={"Location": location},
            ))
            self.assertEqual(result, "failed", location)
            get.assert_called_once()
        body = gzip.compress(_mmdb({"8.8.8.0/24": "PT"}))
        result, get = self._refresh(
            _response(status=302, headers={"Location": "/free/mirror.mmdb.gz"}),
            _response(body=body),
        )
        self.assertEqual(result, "stored")
        self.assertEqual(
            get.call_args_list[1].args[0],
            "https://download.db-ip.com/free/mirror.mmdb.gz",
        )
        self.assertEqual(geoip.country(self.env, "8.8.8.8"), "PT")

    def test_endless_redirects_are_refused(self):
        """More redirects than the cap end the download."""
        hop = {"Location": "/free/again.mmdb.gz"}
        result, get = self._refresh(*[
            _response(status=301, headers=hop)
            for _ in range(geoip.MAX_REDIRECTS + 1)
        ])
        self.assertEqual(result, "failed")
        self.assertEqual(get.call_count, geoip.MAX_REDIRECTS + 1)

    def test_download_down_keeps_the_stored_one(self):
        """A transport failure or a 5xx leaves the stored database."""
        self._store_october()
        for answer in (
            requests.ConnectionError("down"),
            requests.Timeout("slow"),
            _response(status=503),
        ):
            result, _get = self._refresh(answer)
            self.assertEqual(result, "failed")
        self.assertEqual(self._stored().name, "dbip-country-lite-2026-10.mmdb")
        self.assertEqual(geoip.country(self.env, "8.8.8.8"), "ES")

    def test_no_database_at_all(self):
        """Without any database a public address is unknown."""
        self.assertIsNone(geoip.resolver(self.env).source)
        self.assertIsNone(geoip.country(self.env, "8.8.8.8"))

    # -- The setting and the addresses ---------------------------------------

    def test_setting_off_answers_none_and_downloads_nothing(self):
        """Off: no country for any address, and no download."""
        self._store_october()
        self.settings.security_geoip = False
        with patch(_GET) as get:
            self.assertEqual(geoip.refresh(self.env, today=NOV), "off")
        get.assert_not_called()
        lookup = geoip.resolver(self.env)
        self.assertIsNone(lookup.source)
        self.assertIsNone(lookup.attribution)
        for ip in ("8.8.8.8", "10.0.0.1", "::1"):
            self.assertIsNone(lookup(ip), ip)

    def test_setting_on_by_default(self):
        """The country is shown unless someone turns it off."""
        defaults = self.env["cloud.settings"].default_get(["security_geoip"])
        self.assertTrue(defaults["security_geoip"])

    def test_local_addresses(self):
        """Private, loopback and link-local addresses are local."""
        self._store_october()
        lookup = geoip.resolver(self.env)
        for ip in (
            "10.0.0.1", "127.0.0.1", "::1", "192.168.1.5", "172.16.0.1",
            "fe80::1", "fd00::1", "::ffff:10.0.0.1",
        ):
            self.assertEqual(lookup(ip), geoip.LOCAL, ip)

    def test_invalid_addresses(self):
        """Something that is not an address has no country."""
        self._store_october()
        lookup = geoip.resolver(self.env)
        for ip in ("", None, "not-an-ip", "8.8.8.8:22", "999.1.1.1"):
            self.assertIsNone(lookup(ip), ip)
