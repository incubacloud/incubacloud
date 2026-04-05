import logging
import re
from datetime import datetime

from .abstract_executor import AbstractSSHExecutor

_logger = logging.getLogger(__name__)

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

    def _sync_backup_records(self, parsed):
        """Sync parsed duplicity data to cloud.instance.backup records."""
        inst = self._inst()
        Backup = self.env['cloud.instance.backup'].sudo()
        existing = Backup.search([('instance_id', '=', inst.id)])

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
                        'backup_time': dt,
                    })

        # Remove backups purged by retention
        stale = existing.filtered(
            lambda r: r.backup_time not in seen_times
        )
        if stale:
            stale.unlink()

    async def on_failure(self, results, errors):
        for err in errors:
            self._sys(f"✗ {err}")
