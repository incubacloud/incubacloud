import asyncio
import logging
import re
import shlex
from datetime import datetime, timedelta

import asyncssh

from odoo import fields
from odoo.exceptions import UserError

from . import sftp_dst
from ._backup_dst import dst_key
from .abstract_executor import AbstractSSHExecutor
from .cloud_backup_backend import _SFTP_OPERATION_TIMEOUT, SftpUnreachable

_logger = logging.getLogger(__name__)

#: Hours without a new backup set before ``backup_stale`` is raised. The
#: backup container takes one a day and the daily listing checks it some
#: hours later: 48 h is one missed night plus that gap, so a single late
#: run pages nobody, and a second one does (14-nov-2026).
BACKUP_STALE_HOURS = 48
BACKUP_STALE_ALERT_CODE = 'backup_stale'

# Cap the S3 listing per instance.  Backup chains for typical instances
# come in well below this; the cap is a defence against pointing the
# backend at an oversized unrelated bucket and accidentally enumerating
# millions of objects.
_S3_LISTING_CAP = 10000

# Match duplicity object names so we can attribute bytes to a specific
# backup set.  Both forms (data + signature files) carry the timestamp
# of the set they belong to as a UTC compact string ``YYYYMMDDThhmmssZ``.
# The backup image passes ``--file-prefix-archive/-manifest/-signature``
# (``archive-<host>-``, ``manifest-<host>-``, ``signature-<host>-``), so
# the real names read ``archive-<host>-duplicity-full.<ts>.vol1.difftar.gpg``;
# the prefix is optional so names written without it still match.
_FILE_PREFIX = r'(?:(?:archive|manifest|signature)-.+-)?'
_FULL_FILE_RE = re.compile(
    r'^' + _FILE_PREFIX
    + r'duplicity-full(?:-signatures)?\.(\d{8}T\d{6}Z)\..+$'
)
_INC_FILE_RE = re.compile(
    r'^' + _FILE_PREFIX
    + r'duplicity(?:-new-signatures|-inc)\.'
    r'\d{8}T\d{6}Z\.to\.(\d{8}T\d{6}Z)\..+$'
)


#: What duplicity's paramiko prints when the SFTP server's key is not the
#: trusted one (``does not match``, on the error output, as duplicity's
#: fatal errors are) or not one it knows (it then asks on its input, which
#: the command closes). The listing joins its error output to its output
#: so both reach ``on_failure``.
_SFTP_HOST_KEY_REFUSED = ("does not match", "authenticity of host")


def _duplicity_filename_to_iso(key):
    """Return the backup-set ISO timestamp for a duplicity object key,
    or ``None`` for unrelated keys."""
    m = _FULL_FILE_RE.match(key) or _INC_FILE_RE.match(key)
    return m.group(1) if m else None

# Regex patterns for parsing duplicity collection-status output
_CHAIN_START_RE = re.compile(
    r'Chain start time:\s+(.+)',
)
_CHAIN_END_RE = re.compile(
    r'Chain end time:\s+(.+)',
)
_BACKUP_SET_RE = re.compile(
    r'^\s+(Full|Incremental)\s+(.+?)\s+(\d+)\s*$',
)
_TOTAL_SETS_RE = re.compile(
    r'Number of contained backup sets:\s+(\d+)',
)
_TOTAL_VOLUMES_RE = re.compile(
    r'Total number of contained volumes:\s+(\d+)',
)
_NO_BACKUPS_RE = re.compile(
    r'No backup chains with active signatures found',
)


def _parse_duplicity_date(text):
    """Parse duplicity's date format into ISO-8601 string."""
    text = text.strip()
    for fmt in (
        '%a %b %d %H:%M:%S %Y',
        '%a %b  %d %H:%M:%S %Y',
    ):
        try:
            return datetime.strptime(text, fmt).isoformat()
        except ValueError:
            continue
    return text


def _parse_collection_status(output):
    """Parse duplicity collection-status text into structured data.

    Returns: {
        'chains': [
            {
                'start': ISO date,
                'end': ISO date,
                'sets_count': int,
                'volumes_count': int,
                'is_primary': bool,
                'backups': [
                    {'type': 'Full'|'Incremental', 'time': ISO date, 'volumes': int},
                ]
            },
        ],
        'total_chains': int,
    }
    """
    chains = []
    current_chain = None
    is_primary = False

    for line in output.splitlines():
        if _NO_BACKUPS_RE.search(line):
            return {'chains': [], 'total_chains': 0}

        if 'primary backup chain' in line.lower():
            is_primary = True

        m = _CHAIN_START_RE.search(line)
        if m:
            current_chain = {
                'start': _parse_duplicity_date(m.group(1)),
                'end': None,
                'sets_count': 0,
                'volumes_count': 0,
                'is_primary': is_primary,
                'backups': [],
            }
            chains.append(current_chain)
            is_primary = False
            continue

        if current_chain is not None:
            m = _CHAIN_END_RE.search(line)
            if m:
                current_chain['end'] = _parse_duplicity_date(m.group(1))
                continue

            m = _TOTAL_SETS_RE.search(line)
            if m:
                current_chain['sets_count'] = int(m.group(1))
                continue

            m = _TOTAL_VOLUMES_RE.search(line)
            if m:
                current_chain['volumes_count'] = int(m.group(1))
                continue

            m = _BACKUP_SET_RE.match(line)
            if m:
                current_chain['backups'].append({
                    'type': m.group(1),
                    'time': _parse_duplicity_date(m.group(2)),
                    'volumes': int(m.group(3)),
                })

    return {
        'chains': chains,
        'total_chains': len(chains),
    }


def _to_datetime(iso_str):
    """Convert ISO string from parser to Odoo datetime."""
    if not iso_str:
        return None
    try:
        return datetime.fromisoformat(iso_str)
    except (ValueError, TypeError):
        return None


class BackupListExecutor(AbstractSSHExecutor):
    """List backups via duplicity collection-status (production)
    or directly from cloud.instance.backup records (non-production)."""

    _job_type = "backup_list"

    def _inst(self):
        return self.job.instance_id

    def _is_production(self):
        return self._inst().environment == 'production'

    def get_commands(self):
        if not self._is_production():
            # Non-prod: records already exist, no SSH needed
            return []
        inst = self._inst()
        dst = inst.instance_backup_dst
        if not dst:
            raise ValueError(
                "No backup backend configured for this instance."
            )
        d = self._inst_dir(inst)
        return [
            (
                "List backups",
                f"cd {d} && docker compose exec -T backup"
                f" dup collection-status {shlex.quote(dst)} </dev/null 2>&1",
            ),
        ]

    def parse_results(self, results):
        if not self._is_production():
            return []
        errors = []
        for label, data in results.items():
            if data.get('exit_status', 1) != 0:
                stdout = data.get('stdout', '')
                if ('No backup chains' in stdout
                        or 'Collection Status' in stdout):
                    continue
                errors.append(
                    f"'{label}' exited with status {data['exit_status']}"
                )
        return errors

    async def on_success(self, results):
        inst = self._inst()
        count = self.env['cloud.instance.backup'].sudo().search_count(
            [('instance_id', '=', inst.id)]
        )

        if not self._is_production():
            self._sys(f"✓ Found {count} backup(s).")
            return

        stdout = results.get('List backups', {}).get('stdout', '')
        parsed = _parse_collection_status(stdout)
        total_sets = sum(len(c['backups']) for c in parsed['chains'])
        self._sys(
            f"✓ Found {parsed['total_chains']} backup chain(s)"
            f" with {total_sets} backup set(s)."
        )
        self._sync_backup_records(parsed)
        self._check_backup_freshness()
        # Best-effort size pass — never fails the job.
        await self._populate_sizes()

    async def _populate_sizes(self):
        """Write each set's bytes, read from the destination by its type."""
        backend = self._inst().effective_backup_backend.sudo()
        if backend.backend_type == 'sftp':
            await self._populate_sizes_from_sftp(backend)
        else:
            self._populate_sizes_from_s3()

    async def _populate_sizes_from_sftp(self, backend):
        """List the instance's SFTP folder and write per-set bytes.

        Awaited in the job's own loop. Best-effort, like the S3 pass: a
        server that cannot be reached leaves the sizes as they are.

        :param backend: the instance's ``cloud.backup.backend``, as the
            platform
        """
        inst = self._inst()
        try:
            path = sftp_dst.check_dst(
                backend.sftp_user, backend.sftp_host, backend.sftp_port,
                inst.instance_backup_dst,
            )
            kwargs = backend._sftp_connect_kwargs()

            async def _walk():
                async with backend._sftp_client(kwargs) as sftp:
                    return await backend._sftp_walk(sftp, path)

            files = await asyncio.wait_for(_walk(), _SFTP_OPERATION_TIMEOUT)
        except (
            SftpUnreachable, sftp_dst.DstError, UserError, OSError,
            asyncssh.Error, TimeoutError, asyncio.TimeoutError,
        ) as exc:
            _logger.warning(
                "SFTP listing failed for %s: %s", inst.name, type(exc).__name__,
            )
            return
        sizes_by_iso = {}
        for name, size in files:
            iso = _duplicity_filename_to_iso(name.rsplit('/', 1)[-1])
            if iso:
                sizes_by_iso[iso] = sizes_by_iso.get(iso, 0) + size
        self._write_sizes(sizes_by_iso)

    def _write_sizes(self, sizes_by_iso):
        """Write *sizes_by_iso* (set timestamp → bytes) onto the rows."""
        if not sizes_by_iso:
            return
        rows = self.env['cloud.instance.backup'].sudo().search(
            [('instance_id', '=', self._inst().id)],
        )
        for row in rows:
            if not row.backup_time:
                continue
            iso = row.backup_time.strftime('%Y%m%dT%H%M%SZ')
            if iso in sizes_by_iso:
                row.size = sizes_by_iso[iso]

    def _sync_backup_records(self, parsed):
        """Sync parsed duplicity data to cloud.instance.backup records."""
        inst = self._inst()
        Backup = self.env['cloud.instance.backup'].sudo()
        # Chain rows only: this sync mirrors the duplicity listing, and
        # its stale-pruning below must never touch archive rows (one-shot
        # ZIP dumps), which by definition are absent from that listing.
        existing = Backup.search([
            ('instance_id', '=', inst.id),
            ('kind', '=', 'chain'),
        ])

        seen_times = set()
        for chain in parsed.get('chains', []):
            chain_start = _to_datetime(chain.get('start'))
            is_primary = chain.get('is_primary', False)
            for bk in chain.get('backups', []):
                dt = _to_datetime(bk['time'])
                if not dt:
                    continue
                seen_times.add(dt)
                rec = existing.filtered(
                    lambda r, d=dt: r.backup_time == d
                )
                vals = {
                    'backup_type': bk['type'],
                    'volumes': bk.get('volumes', 1),
                    'chain_start': chain_start,
                    'is_primary': is_primary,
                }
                if rec:
                    rec.write(vals)
                else:
                    Backup.create({
                        **vals,
                        'instance_id': inst.id,
                        'kind': 'chain',
                        'backup_time': dt,
                    })

        # Remove backups purged by retention
        stale = existing.filtered(
            lambda r: r.backup_time not in seen_times
        )
        if stale:
            stale.unlink()

    def _check_backup_freshness(self):
        """Raise or resolve ``backup_stale`` from the sets just synced.

        Only the scheduled duplicity sets count (``kind == 'chain'``): a ZIP
        made by hand is not the copy the container takes. An instance with
        no set yet is measured from its creation, so a new one is not
        flagged before its first nights. The alert closes on the first
        listing that sees a set within :data:`BACKUP_STALE_HOURS`.

        It is raised on the second listing in a row that finds none
        (``alert_policy_backup_stale``): measured from the creation, an
        instance whose backups started later reads as stale on its
        first listing.
        """
        inst = self._inst()
        Alert = self.env['cloud.alert'].sudo()
        latest = self.env['cloud.instance.backup'].sudo().search(
            [('instance_id', '=', inst.id), ('kind', '=', 'chain')],
            order='backup_time desc', limit=1,
        ).backup_time
        since = latest or inst.create_date
        limit = timedelta(hours=BACKUP_STALE_HOURS)
        if not since or fields.Datetime.now() - since <= limit:
            # The host as raise_alert gets it: the suspicion of a first
            # stale listing is kept per (code, host, instance), and a
            # resolve without the host would leave it to count.
            Alert.resolve_alert(
                BACKUP_STALE_ALERT_CODE, host=inst.host_id or None, instance=inst,
            )
            return
        if latest:
            last = f"the last one is from {latest:%Y-%m-%d %H:%M}"
        else:
            last = "there is none yet"
        Alert.raise_alert(
            BACKUP_STALE_ALERT_CODE,
            f"Production instance '{inst.name}' has had no new backup for"
            f" more than {BACKUP_STALE_HOURS} hours: {last}.",
            host=inst.host_id or None,
            instance=inst,
        )

    def _populate_sizes_from_s3(self):
        """List S3 objects under the instance's backup path and write
        per-set bytes onto each ``cloud.instance.backup`` row.

        Best-effort: a misconfigured backend, network hiccup or missing
        boto3 must NEVER fail the ``backup_list`` job. The duplicity
        chain summary is the source of truth for which sets exist; the
        size is decorative metadata that defaults to 0 if unavailable.
        """
        inst = self._inst()
        # ``s3_secret_access_key``/``passphrase`` are EncryptedChar fields
        # restricted to ``group_cloud_manager``. The executor runs as
        # the queue worker (already privileged for SSH), so ``sudo()``
        # is needed to read them. The decrypted secret is passed into
        # boto3.client() and never logged.
        backend = inst.effective_backup_backend.sudo()
        if not backend or backend.backend_type != 's3':
            return
        if not (backend.s3_bucket and backend.s3_access_key_id
                and backend.s3_secret_access_key):
            return

        try:
            import boto3
            from botocore.exceptions import (
                BotoCoreError, ClientError, NoCredentialsError,
            )
        except ImportError:
            _logger.warning(
                "boto3 not available; skipping S3 size pass for %s",
                inst.name,
            )
            return

        # The path duplicity writes to, as the purges read it: a suffix
        # (``backup_path_suffix``) or a path of its own included. Rebuilt
        # from the folder and the name, it listed the path of another
        # instance of the same name. The final slash keeps ``foo`` from
        # counting ``foo-42``'s objects.
        key = dst_key(inst.instance_backup_dst)
        if not (key and key[1]):
            return
        bucket, prefix = key[0], '/'.join(key[1]) + '/'

        client_kwargs = {
            'aws_access_key_id': backend.s3_access_key_id,
            'aws_secret_access_key': backend.s3_secret_access_key,
        }
        if backend.s3_endpoint_url:
            client_kwargs['endpoint_url'] = backend.s3_endpoint_url
        if backend.s3_region:
            client_kwargs['region_name'] = backend.s3_region

        sizes_by_iso = {}
        try:
            s3 = boto3.client('s3', **client_kwargs)
            paginator = s3.get_paginator('list_objects_v2')
            total = 0
            capped = False
            for page in paginator.paginate(
                Bucket=bucket, Prefix=prefix,
            ):
                for obj in page.get('Contents', []):
                    total += 1
                    if total > _S3_LISTING_CAP:
                        capped = True
                        break
                    key = obj['Key'].rsplit('/', 1)[-1]
                    iso = _duplicity_filename_to_iso(key)
                    if iso:
                        sizes_by_iso[iso] = (
                            sizes_by_iso.get(iso, 0) + obj['Size']
                        )
                if capped:
                    _logger.warning(
                        "S3 listing for %s exceeded %d objects; "
                        "size pass capped",
                        inst.name, _S3_LISTING_CAP,
                    )
                    break
        except (BotoCoreError, ClientError, NoCredentialsError) as exc:
            _logger.warning(
                "S3 listing failed for %s: %s", inst.name, type(exc).__name__,
            )
            return

        self._write_sizes(sizes_by_iso)

    async def on_failure(self, results, errors):
        for err in errors:
            self._sys(f"✗ {err}")
        stdout = (results.get('List backups') or {}).get('stdout', '')
        if any(text in stdout for text in _SFTP_HOST_KEY_REFUSED):
            await self._sftp_host_key_refused()

    async def _sftp_host_key_refused(self):
        """Raise the changed-key alert, or say the instance needs a build.

        Only when the instance runs with the server key trusted now: one
        built before a new key was trusted still holds the old one, and
        what it needs is its changes applied, not an alert that would be
        raised again the day after it was resolved. And even then only
        when the server presents another key than the trusted one, read
        without credentials (``_sftp_check_presented_key_async``: this runs
        in the job's own loop). A trusted RSA key the server still presents
        beside the other type it answers duplicity's paramiko with is told
        as such, not as a changed key.
        """
        inst = self._inst()
        backend = inst.effective_backup_backend.sudo()
        if backend.backend_type != 'sftp':
            return
        applied = (
            (inst.applied_config_snapshot or {}).get('backup_sftp') or {}
        ).get('host_key', '')
        if applied != (backend.sftp_host_key_fingerprint or ''):
            self._sys(
                "The SFTP server's key trusted on the destination is not the "
                "one this instance runs with: apply the instance's changes "
                "(rebuild) so it gets the trusted key."
            )
            return
        outcome, presented = await backend._sftp_check_presented_key_async()
        if outcome == 'changed':
            self._sys(
                f"The SFTP server presents the key {presented}, not the "
                f"trusted {backend.sftp_host_key_fingerprint}: check it and "
                "trust it again on the backup destination."
            )
        elif outcome == 'key_type':
            self._sys(
                f"The backups get the SFTP server's key {presented}, of "
                "another type than the trusted RSA key, which the server "
                "still presents unchanged: read the server's key on the "
                "backup destination, trust it and rebuild the instance."
            )
