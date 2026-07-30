import logging
import re
from datetime import datetime

from .abstract_executor import AbstractSSHExecutor

_logger = logging.getLogger(__name__)

# Cap the S3 listing per instance.  Backup chains for typical tenants
# come in well below this; the cap is a defence against pointing the
# backend at an oversized unrelated bucket and accidentally enumerating
# millions of objects.
_S3_LISTING_CAP = 10000

# Match duplicity object names so we can attribute bytes to a specific
# backup set.  Both forms (data + signature files) carry the timestamp
# of the set they belong to as a UTC compact string ``YYYYMMDDThhmmssZ``.
_FULL_FILE_RE = re.compile(
    r'^duplicity-full(?:-signatures)?\.(\d{8}T\d{6}Z)\..+$'
)
_INC_FILE_RE = re.compile(
    r'^duplicity(?:-new-signatures|-inc)\.'
    r'\d{8}T\d{6}Z\.to\.(\d{8}T\d{6}Z)\..+$'
)


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
                f" dup collection-status \"{dst}\"",
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
        # Best-effort S3 size pass — never fails the job.
        self._populate_sizes_from_s3()

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

    def _populate_sizes_from_s3(self):
        """List S3 objects under the backend and write per-set bytes
        onto each ``cloud.instance.backup`` row.

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

        project_folder = (
            (inst.project_id.remote_folder if inst.project_id else '')
            or 'default'
        )
        prefix = (
            f"{(backend.s3_path or '').strip('/')}/"
            f"{project_folder}/{inst.name}/"
        ).lstrip('/')

        client_kwargs = {
            'aws_access_key_id': backend.s3_access_key_id,
            'aws_secret_access_key': backend.s3_secret_access_key,
        }
        if backend.s3_endpoint_url:
            client_kwargs['endpoint_url'] = backend.s3_endpoint_url

        sizes_by_iso = {}
        try:
            s3 = boto3.client('s3', **client_kwargs)
            paginator = s3.get_paginator('list_objects_v2')
            total = 0
            capped = False
            for page in paginator.paginate(
                Bucket=backend.s3_bucket, Prefix=prefix,
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

        if not sizes_by_iso:
            return

        rows = self.env['cloud.instance.backup'].sudo().search(
            [('instance_id', '=', inst.id)],
        )
        for row in rows:
            if not row.backup_time:
                continue
            iso = row.backup_time.strftime('%Y%m%dT%H%M%SZ')
            if iso in sizes_by_iso:
                row.size = sizes_by_iso[iso]

    async def on_failure(self, results, errors):
        for err in errors:
            self._sys(f"✗ {err}")
