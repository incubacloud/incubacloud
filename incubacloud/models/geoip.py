"""Which country an address is in, looked up on the panel itself.

The security viewer shows the country of the addresses in the servers'
logs. No address leaves the panel for it: the lookup reads a local
MaxMind-format database, and resolves the country alone.

**Which database.** Odoo's own (``config['geoip_country_db']``) when the
server has it: Odoo.sh ships one. A Doodba image does not:
``/usr/share/GeoIP/`` is empty and there is no licence to fill it
(checked in devel, 13-nov-2026). Without it, DB-IP's IP to Country Lite
(CC BY 4.0), which :func:`refresh` downloads once a month and stores as
an ``ir.attachment``, so every worker and every server of the panel
shares one copy, on Odoo.sh too. When that attachment lives in the
database rather than in the filestore, it is opened from a temporary
file. A panel showing a country from DB-IP shows :data:`ATTRIBUTION`
beside it: the licence asks for it wherever the data is shown.

**The download is bounded.** HTTPS to :data:`DBIP_HOST` alone; a
redirect to any other host is refused, never followed. At most
:data:`MAX_DOWNLOAD_BYTES` received and :data:`MAX_DATABASE_BYTES` once
uncompressed, in :data:`DOWNLOAD_SECONDS`. A file that does not open as
a country database never replaces the one stored. DB-IP publishes each
month's file early in the month: while it is not there yet, the
previous month's is taken, and each daily pass tries the month again
until it is (one real download a month, plus that fallback).

**What a lookup answers.** A two-letter ISO code, or :data:`LOCAL` for
an address that is not routable on the internet (private, loopback,
link-local…), or None: unknown, no database, or the ``security_geoip``
setting off, which also stops the download.

Plan #84, cut C3 (13-nov-2026), with Fable's review: B4.
"""
import gzip
import io
import ipaddress
import logging
import os
import re
import tempfile
import threading
import time
import zlib
from datetime import date
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests

from odoo import api, fields, models
from odoo.tools import config

try:
    import maxminddb
except ImportError:  # Odoo lists it (with geoip2); a bare image may lack it
    maxminddb = None

_logger = logging.getLogger(__name__)

#: What a lookup answers for an address that is not on the internet.
LOCAL = "local"

#: The only host the database is downloaded from.
DBIP_HOST = "download.db-ip.com"

#: DB-IP's monthly file; ``{month}`` is ``YYYY-MM``.
DBIP_URL = "https://" + DBIP_HOST + "/free/dbip-country-lite-{month}.mmdb.gz"

#: The line the licence asks for wherever a country from DB-IP is shown.
ATTRIBUTION = "IP Geolocation by DB-IP (https://db-ip.com), CC BY 4.0"

#: Caps of one download. November 2026's file is 4.2 MB compressed and
#: 8.5 MB uncompressed (13-nov-2026): about seven times that leaves room
#: for it to grow and still refuses anything that is not that file.
MAX_DOWNLOAD_BYTES = 32 * 1024 * 1024
MAX_DATABASE_BYTES = 64 * 1024 * 1024

#: Seconds the whole download may take, checked between chunks, and the
#: per-call limits of the connection. Well under the two minutes a cron
#: worker runs for: 4.2 MB in 60 s needs 70 kB/s.
DOWNLOAD_SECONDS = 60
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 20

#: Redirects within :data:`DBIP_HOST` followed at most.
MAX_REDIRECTS = 3

#: Bytes read from the response at a time.
_CHUNK = 64 * 1024

#: The stored database: ``dbip-country-lite-YYYY-MM.mmdb``.
_ATTACHMENT_PREFIX = "dbip-country-lite-"
_ATTACHMENT_SUFFIX = ".mmdb"
_ATTACHMENT_NAME_RE = re.compile(r"^dbip-country-lite-(\d{4}-\d{2})\.mmdb$")
_ISO_RE = re.compile(r"^[A-Z]{2}$")

#: An address the opened database is checked with: any error means the
#: file's tree or data section is broken.
_PROBE_ADDRESS = "8.8.8.8"

#: The opened database of each Odoo database, per worker: ``dbname ->
#: (key, reader, source, temp_path)``. A reader is mapped in memory, so the
#: workers of one server share its pages.
_READERS = {}
_READERS_LOCK = threading.Lock()


class GeoipDownloadError(Exception):
    """A download that was refused or failed; the stored file stays."""


class _NotPublished(GeoipDownloadError):
    """DB-IP has no file for that month (yet)."""


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


class CountryLookup:
    """Answers the country of addresses from one opened database.

    Built by :func:`resolver` once per request or pass, and called for
    each address.

    :ivar source: ``"odoo"`` (Odoo's database), ``"dbip"`` (DB-IP's) or
        None (none, or the setting off).
    """

    def __init__(self, reader=None, source=None, enabled=True):
        """Keep the reader to look addresses up in.

        :param reader: an opened ``maxminddb`` reader, or None.
        :param source: ``"odoo"``, ``"dbip"`` or None.
        :param bool enabled: False when the setting is off: every address
            answers None.
        """
        self._reader = reader
        self._enabled = enabled
        self.source = source if enabled and reader is not None else None

    @property
    def attribution(self):
        """The line to show beside the countries, or None.

        :returns: :data:`ATTRIBUTION` when they come from DB-IP.
        :rtype: str | None
        """
        return ATTRIBUTION if self.source == "dbip" else None

    def __call__(self, ip):
        """Return the country of *ip*.

        :param str ip: an IPv4 or IPv6 address.
        :returns: its ISO 3166 code, :data:`LOCAL`, or None when unknown,
            invalid, without a database or with the setting off.
        :rtype: str | None
        """
        if not self._enabled:
            return None
        try:
            address = ipaddress.ip_address(str(ip or "").strip())
        except ValueError:
            return None
        if address.version == 6 and address.ipv4_mapped:
            address = address.ipv4_mapped
        if not address.is_global:
            return LOCAL
        if self._reader is None:
            return None
        try:
            record = self._reader.get(str(address))
        except (ValueError, OSError, maxminddb.InvalidDatabaseError):
            return None
        if not isinstance(record, dict):
            return None
        place = record.get("country") or record.get("registered_country")
        code = place.get("iso_code") if isinstance(place, dict) else None
        return code if isinstance(code, str) and _ISO_RE.match(code) else None


def resolver(env):
    """Return the lookup of this panel's database, opened once per worker.

    :param env: an Odoo environment.
    :rtype: CountryLookup
    """
    if not env["cloud.settings"]._get_system().security_geoip:
        return CountryLookup(enabled=False)
    if maxminddb is None:
        return CountryLookup()
    reader, source = _opened(env)
    return CountryLookup(reader, source)


def country(env, ip):
    """Return the country of *ip*: :class:`CountryLookup` for one address.

    :param env: an Odoo environment.
    :param str ip: an IPv4 or IPv6 address.
    :rtype: str | None
    """
    return resolver(env)(ip)


def _odoo_database_path():
    """Return the path of Odoo's country database when it is a file.

    :rtype: str | None
    """
    path = config.get("geoip_country_db")
    return path if path and os.path.isfile(path) else None


def _odoo_reader(env):
    """Return Odoo's country database opened, or None when it does not open.

    :param env: an Odoo environment.
    """
    path = _odoo_database_path()
    if not path:
        return None
    try:
        stat = os.stat(path)
    except OSError:
        return None
    key = ("odoo", path, stat.st_mtime_ns, stat.st_size)
    return _cached(env, key, "odoo", lambda: (path, None))


def _stored(env):
    """Return the stored DB-IP attachment, or an empty recordset.

    :param env: an Odoo environment.
    """
    return env["ir.attachment"].sudo().search([
        ("res_model", "=", "cloud.settings"),
        ("name", "=like", _ATTACHMENT_PREFIX + "%" + _ATTACHMENT_SUFFIX),
    ], order="id desc", limit=1)


def _stored_month(attachment):
    """Return the ``YYYY-MM`` of a stored attachment, or None.

    :param attachment: an ``ir.attachment`` record or an empty recordset.
    :rtype: str | None
    """
    if not attachment:
        return None
    match = _ATTACHMENT_NAME_RE.match(attachment.name or "")
    return match.group(1) if match else None


def _dbip_reader(env):
    """Return the stored DB-IP database opened, or None.

    From the filestore file itself when there is one; when attachments
    live in the database, from a temporary copy kept while it is in use.

    :param env: an Odoo environment.
    """
    attachment = _stored(env)
    if not attachment:
        return None
    key = ("dbip", attachment.id, attachment.checksum)

    def locate():
        """Return ``(path, temp_path)`` of the attachment's bytes."""
        if attachment.store_fname:
            full = attachment._full_path(attachment.store_fname)
            if os.path.isfile(full):
                return full, None
        temp = _write_temp(attachment.raw or b"")
        return temp, temp

    return _cached(env, key, "dbip", locate)


def _cached(env, key, source, locate):
    """Return the reader for *key*, opening it when the cache holds another.

    :param env: an Odoo environment.
    :param tuple key: what identifies the file's content.
    :param str source: ``"odoo"`` or ``"dbip"``.
    :param locate: callable returning ``(path, temp_path)``; *temp_path*
        is removed when the reader is replaced, or at once if it does not
        open.
    :returns: the reader, or None when the file does not open.
    """
    dbname = env.cr.dbname
    with _READERS_LOCK:
        held = _READERS.get(dbname)
        if held and held[0] == key:
            return held[1]
        path, temp = locate()
        try:
            reader = maxminddb.open_database(path)
        except (OSError, ValueError, maxminddb.InvalidDatabaseError) as exc:
            # Odoo's, tried on every lookup, says so as quietly as Odoo does.
            _logger.log(
                logging.DEBUG if source == "odoo" else logging.WARNING,
                "GeoIP: the %s database does not open: %s", source, exc,
            )
            _remove(temp)
            return None
        # A reader another thread still holds keeps working: it is not
        # closed here, only forgotten, and its mapping outlives the file.
        _READERS[dbname] = (key, reader, source, temp)
        if held:
            _remove(held[3])
        return reader


def _opened(env):
    """Return ``(reader, source)``: Odoo's database, else DB-IP's, else none.

    :param env: an Odoo environment.
    :rtype: tuple
    """
    reader = _odoo_reader(env)
    if reader is not None:
        return reader, "odoo"
    reader = _dbip_reader(env)
    if reader is not None:
        return reader, "dbip"
    return None, None


def _write_temp(data):
    """Write *data* to a private temporary file and return its path.

    :param bytes data: the database.
    :rtype: str
    """
    fd, path = tempfile.mkstemp(prefix="incubacloud-geoip-", suffix=".mmdb")
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    return path


def _remove(path):
    """Remove a temporary file, if any; a missing one is not an error.

    :param path: the file, or None.
    """
    if path:
        Path(path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


def _allowed(url):
    """Return True when *url* is HTTPS on :data:`DBIP_HOST`.

    :param str url: a URL to download from.
    :rtype: bool
    """
    parts = urlsplit(url)
    return (
        parts.scheme == "https"
        and parts.hostname == DBIP_HOST
        and parts.port in (None, 443)
    )


def _fetch(url):
    """Download *url* within the caps and return its bytes.

    :param str url: the file on :data:`DBIP_HOST`.
    :rtype: bytes
    :raises _NotPublished: on a 404.
    :raises GeoipDownloadError: on a refused redirect, a cap exceeded, an
        HTTP error or a transport failure.
    """
    deadline = time.monotonic() + DOWNLOAD_SECONDS
    for _hop in range(MAX_REDIRECTS + 1):
        if not _allowed(url):
            raise GeoipDownloadError(f"refused to download from {url}")
        try:
            response = requests.get(
                url, stream=True, allow_redirects=False,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            )
        except requests.RequestException as exc:
            raise GeoipDownloadError(f"download failed: {exc}") from exc
        try:
            if response.status_code in (301, 302, 303, 307, 308):
                url = urljoin(url, response.headers.get("Location") or "")
                continue
            if response.status_code == 404:
                raise _NotPublished(url)
            if response.status_code != 200:
                raise GeoipDownloadError(
                    f"HTTP {response.status_code} from {url}",
                )
            return _read_body(response, deadline)
        finally:
            response.close()
    raise GeoipDownloadError(f"more than {MAX_REDIRECTS} redirects")


def _read_body(response, deadline):
    """Return the body of *response*, refusing it past the caps.

    :param response: a streamed ``requests.Response``.
    :param float deadline: ``time.monotonic()`` it must end by.
    :rtype: bytes
    :raises GeoipDownloadError: when it is too large or too slow.
    """
    declared = response.headers.get("Content-Length") or ""
    if declared.isdigit() and int(declared) > MAX_DOWNLOAD_BYTES:
        raise GeoipDownloadError(f"{declared} bytes is over the cap")
    body = bytearray()
    try:
        for chunk in response.iter_content(_CHUNK):
            body += chunk
            if len(body) > MAX_DOWNLOAD_BYTES:
                raise GeoipDownloadError("the download is over the size cap")
            if time.monotonic() > deadline:
                raise GeoipDownloadError("the download is over the time cap")
    except requests.RequestException as exc:
        raise GeoipDownloadError(f"download failed: {exc}") from exc
    return bytes(body)


def _gunzip(data):
    """Uncompress *data* within :data:`MAX_DATABASE_BYTES`.

    :param bytes data: the ``.gz`` file.
    :rtype: bytes
    :raises GeoipDownloadError: when it is not gzip or too large.
    """
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as handle:
            out = handle.read(MAX_DATABASE_BYTES + 1)
    except (OSError, EOFError, zlib.error) as exc:
        raise GeoipDownloadError(f"not a gzip file: {exc}") from exc
    if len(out) > MAX_DATABASE_BYTES:
        raise GeoipDownloadError("the database is over the size cap")
    return out


def _validate(data):
    """Check that *data* opens, as the lookup opens it, as a country database.

    :param bytes data: the uncompressed ``.mmdb``.
    :raises GeoipDownloadError: when it does not.
    """
    path = _write_temp(data)
    try:
        reader = maxminddb.open_database(path)
        try:
            kind = reader.metadata().database_type or ""
            if "Country" not in kind:
                raise GeoipDownloadError(f"{kind!r} is not a country database")
            reader.get(_PROBE_ADDRESS)
        finally:
            reader.close()
    except (OSError, ValueError, maxminddb.InvalidDatabaseError) as exc:
        raise GeoipDownloadError(f"not a valid database: {exc}") from exc
    finally:
        _remove(path)


def _months(today):
    """Return this month and the one before, as ``YYYY-MM``.

    :param date today: the day of the pass.
    :rtype: list[str]
    """
    if today.month == 1:
        previous = date(today.year - 1, 12, 1)
    else:
        previous = date(today.year, today.month - 1, 1)
    return [today.strftime("%Y-%m"), previous.strftime("%Y-%m")]


def refresh(env, today=None):
    """Download this month's DB-IP database when the panel needs it.

    Nothing when the setting is off or Odoo's own database is there.
    Otherwise this month's file, or, while it is not published, the
    previous month's; a month already stored is not downloaded again. A
    failed download, a file over a cap or one that does not open leaves
    the stored database as it was.

    :param env: an Odoo environment.
    :param date today: the day of the pass (today when None).
    :returns: ``"off"``, ``"odoo"``, ``"unavailable"`` (no maxminddb),
        ``"current"`` (the month is stored), ``"stored"`` or ``"failed"``.
    :rtype: str
    """
    settings = env["cloud.settings"]._get_system()
    if not settings.security_geoip:
        return "off"
    if maxminddb is None:
        return "unavailable"
    if _odoo_reader(env) is not None:
        return "odoo"
    attachment = _stored(env)
    held = _stored_month(attachment)
    for month in _months(today or fields.Date.context_today(settings)):
        if month == held:
            return "current"
        url = DBIP_URL.format(month=month)
        try:
            data = _gunzip(_fetch(url))
            _validate(data)
        except _NotPublished:
            _logger.info("GeoIP: %s is not published yet", url)
            continue
        except GeoipDownloadError as exc:
            _logger.warning("GeoIP: keeping the stored database: %s", exc)
            return "failed"
        values = {
            "name": f"{_ATTACHMENT_PREFIX}{month}{_ATTACHMENT_SUFFIX}",
            "raw": data,
            "mimetype": "application/octet-stream",
        }
        if attachment:
            attachment.write(values)
        else:
            env["ir.attachment"].sudo().create(
                values | {"res_model": "cloud.settings", "res_id": settings.id},
            )
        _logger.info("GeoIP: stored DB-IP's database of %s", month)
        return "stored"
    _logger.warning(
        "GeoIP: neither month is published; keeping the stored database",
    )
    return "failed"


class CloudSettings(models.Model):
    """The monthly download of the GeoIP database (:func:`refresh`)."""

    _inherit = "cloud.settings"

    @api.model
    def _cron_geoip_refresh(self):
        """Daily pass: download DB-IP's database when its month is missing.

        :returns: what :func:`refresh` did.
        :rtype: str
        """
        return refresh(self.env)
