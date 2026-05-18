import re
from contextlib import suppress
from datetime import timezone as tz
from operator import itemgetter

from odoo import fields as odoo_fields

from .abstract_executor import AbstractSSHExecutor

# Alert thresholds
_MEM_WARN_PCT = 85.0      # memory % that triggers a warning once sustained
_CPU_WARN_PCT = 90.0      # CPU %    that triggers a warning once sustained
_ERROR_GROUPS_WARN = 1    # number of distinct ERROR fingerprints since last check
_ERROR_LINES_HEAD = 200   # cap raw ERROR lines fetched from the container log
_ERROR_GROUPS_MAX = 10    # cap distinct fingerprints shipped in the payload
_ERROR_SAMPLE_PER_GRP = 3 # raw lines kept per fingerprint for the payload

# Hysteresis: how many consecutive cycles must stay above the CPU/memory
# threshold before we surface the alert. With the cron firing every 5 min,
# a value of 2 means "the symptom was there twice in 10 min". Single-cycle
# spikes (deploys we missed gating on, GC pauses, transient bursts) are
# dropped on the floor.
_STREAK_REQUIRED = 2

# Job types that legitimately drive CPU / memory / log noise on the
# instance and would produce false-positive alerts if the health probe
# ran concurrently. When any of these has an *active* cloud.job pointing
# at the same instance, we skip the entire health pass — we still bump
# ``last_health_check`` so the schedule doesn't drift and the SPA shows
# a fresh timestamp.
_BLOCKING_JOB_TYPES = (
    'deploy_instance',
    'rebuild_instance',
    'full_setup',
    'restore_instance',
)

# Odoo log line prefix: "2026-05-17 18:30:21,415 1 ERROR devel module.path: "
# We strip the timestamp+pid+db so identical errors at different times
# collapse into one fingerprint.
_LOG_PREFIX_RE = re.compile(
    r'^\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2}[.,]\d+\s+\d+\s+'
)
# Generic digit runs (record ids, durations, addresses) get masked
# so "Record 42 not found" and "Record 9123 not found" share a fingerprint.
# Greedy on any 1+ digit run — for log dedupe purposes single-digit
# codes are just as much noise as longer ones.
_DIGIT_RUN_RE = re.compile(r'\b\d+\b')
# Hex / pointer-like runs (object ids, memory addrs) — same treatment.
_HEX_RUN_RE = re.compile(r'\b0x[0-9a-fA-F]+\b')
_FINGERPRINT_MAX_LEN = 160


class InstanceHealthExecutor(AbstractSSHExecutor):
    """Checks the health of a deployed doodba instance.

    Checks performed:
    1. Container state — is the ``odoo`` container running?
    2. HTTP health — does Odoo respond on port 8069? (exec inside container)
    3. CPU / memory snapshot — one ``docker stats --no-stream`` reading.
       Combined with a per-instance streak counter, an alert is only
       raised after ``_STREAK_REQUIRED`` consecutive cycles above the
       threshold — single-cycle spikes are intentionally ignored.
    4. Error logs — fetch up to ``_ERROR_LINES_HEAD`` ERROR lines since
       the last check and dedupe them into fingerprint groups. The
       alert payload carries the groups so the operator does not need
       to SSH into the host to see *which* errors fired.

    Skipped entirely when an intrusive job (deploy / rebuild / restore /
    full setup) is in flight on the same instance: those jobs are the
    only ones that legitimately drive resource usage and log noise
    above the thresholds, so probing during them is pure noise.

    State writes on ``cloud.instance``:
    - Container down  → ``running=False``, ``status='error'``
    - Up + issues     → ``running=True``,  ``status='warning'`` or ``'error'``
    - All clear       → ``running=True``,  ``status='ok'``

    Instance-scoped alert codes: ``instance_down``, ``instance_unresponsive``,
    ``instance_high_cpu``, ``instance_high_memory``, ``instance_error_logs``.
    """

    _job_type = "instance_health"

    # ── Helpers ────────────────────────────────────────────────────────────

    def _inst(self):
        return self.job.instance_id

    def _is_blocked_by_active_job(self):
        """True iff another active job on the same instance would make
        the resource snapshot meaningless (deploy/rebuild/etc.).
        """
        inst = self._inst()
        if not inst:
            return False
        Job = self.env['cloud.job']
        return bool(Job.search_count([
            ('id', '!=', self.job.id),
            ('instance_id', '=', inst.id),
            ('state', 'in', Job._active_states),
            ('job_type_id.code', 'in', list(_BLOCKING_JOB_TYPES)),
        ]))

    # ── AbstractSSHExecutor interface ─────────────────────────────────────

    async def before_execute(self, transport):
        inst = self._inst()
        self._sys(f"Checking health of '{inst.name}'…")
        self._skipped = self._is_blocked_by_active_job()
        if self._skipped:
            self._sys(
                "Skipped — an intrusive job (deploy / rebuild / restore) "
                "is currently running on this instance. "
                "Resource and log readings would be meaningless."
            )

    def get_commands(self):
        # If we are skipping this cycle, run a single cheap no-op so the
        # executor still produces results to traverse. Keeping the shape
        # of ``get_commands`` non-empty avoids special-casing the
        # AbstractSSHExecutor loop.
        if getattr(self, '_skipped', False):
            return [("noop", "true")]

        inst = self._inst()
        d = self._inst_dir(inst)

        lhc = inst.last_health_check
        if lhc:
            since = lhc.replace(tzinfo=tz.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        else:
            since = "10m"

        return [
            # 1. Container state
            (
                "container_state",
                f"cd {d} && "
                f"docker compose ps --format '{{{{.Service}}}}\t{{{{.State}}}}' 2>&1",
            ),
            # 2. CPU / memory — single non-streaming snapshot (~1.5 s).
            # `docker stats --no-stream` already computes CPU% from two
            # internal samples taken ~1 s apart, so we get an instantaneous
            # but real CPU reading without the cost (and false positives)
            # of holding the SSH channel open for 20 s.
            (
                "cpu_mem_snapshot",
                f"CTNR=$(cd {d} && docker compose ps -q odoo 2>/dev/null | head -1) && "
                f"[ -n \"$CTNR\" ] && "
                f"docker stats --no-stream "
                f"--format '{{{{.CPUPerc}}}}\t{{{{.MemPerc}}}}' \"$CTNR\" 2>/dev/null | "
                f"sed 's/%//g' | "
                f"awk -F'\\t' "
                f"'{{gsub(/[^0-9.]/,\"\",$1);gsub(/[^0-9.]/,\"\",$2);"
                f"printf \"%.1f\\t%.1f\\n\",$1+0,$2+0}}' "
                f"|| echo '0.0\t0.0'",
            ),
            # 3. HTTP health — curl inside the odoo container
            (
                "http_health",
                f"cd {d} && "
                f"docker compose exec -T odoo "
                f"curl -sf --max-time 15 http://localhost:8069/web/health "
                f"> /dev/null 2>&1; echo \"exit:$?\"",
            ),
            # 4. ERROR lines in odoo logs since last check — raw sample,
            # capped to _ERROR_LINES_HEAD so a runaway log loop can't
            # blow up parsing memory. Dedupe happens in parse_results.
            # The grep is anchored to the Odoo log-level field (after the
            # "<date> <time> <pid>" prefix) so substrings of the word ERROR
            # inside INFO/DEBUG lines — notably asyncssh logging the very
            # command we run here — do not generate self-referential
            # false positives. The trailing sed strips ANSI color codes
            # that Odoo injects around the level keyword so samples stored
            # in the alert payload render as plain text.
            (
                "error_lines",
                f"cd {d} && "
                f"docker compose logs --no-color --since '{since}' odoo 2>&1 "
                f"| grep -aP "
                f"'\\b\\d{{4}}-\\d{{2}}-\\d{{2}}\\s+"
                f"\\d{{2}}:\\d{{2}}:\\d{{2}}[.,]\\d+\\s+\\d+\\s+"
                f"(\\x1b\\[[0-9;]+m)*(ERROR|CRITICAL)"
                f"(\\x1b\\[[0-9;]+m)*\\s' "
                f"| sed -E 's/\\x1b\\[[0-9;]*m//g' "
                f"| head -{_ERROR_LINES_HEAD} || true",
            ),
        ]

    def parse_results(self, results):
        # Skipped pass: nothing to parse, on_success just stamps the timestamp.
        if getattr(self, '_skipped', False):
            return []

        # 1. Container state
        state_out = results.get("container_state", {}).get("stdout", "")
        self._container_running = False
        for line in state_out.splitlines():
            parts = line.strip().split('\t')
            if len(parts) >= 2 and parts[0].strip() == 'odoo':
                self._container_running = parts[1].strip().lower() == 'running'
                break

        # 2. CPU / memory snapshot
        cm_out = results.get("cpu_mem_snapshot", {}).get("stdout", "0.0\t0.0").strip()
        self._cpu_current = 0.0
        self._mem_current = 0.0
        parts = cm_out.split('\t')
        if len(parts) >= 2:
            with suppress(ValueError):
                self._cpu_current = float(parts[0].strip())
                self._mem_current = float(parts[1].strip())

        # 3. HTTP health
        http_out = results.get("http_health", {}).get("stdout", "").strip()
        self._http_ok = "exit:0" in http_out

        # 4. ERROR lines → fingerprint groups
        err_out = results.get("error_lines", {}).get("stdout", "")
        self._error_groups = self._dedupe_error_lines(err_out)
        self._error_total = sum(g['count'] for g in self._error_groups)

        return []  # never hard-fail the job — report via status/alerts

    # ── Error log dedupe ──────────────────────────────────────────────────

    def _fingerprint(self, line):
        """Collapse an Odoo log line into a fingerprint that ignores
        timestamps, PIDs, numeric ids and pointer-like runs so that
        identical errors with different metadata group together.
        """
        stripped = _LOG_PREFIX_RE.sub('', line.strip())
        stripped = _HEX_RUN_RE.sub('<HEX>', stripped)
        stripped = _DIGIT_RUN_RE.sub('<N>', stripped)
        return stripped[:_FINGERPRINT_MAX_LEN]

    def _dedupe_error_lines(self, raw):
        """Turn a blob of raw ERROR lines into an ordered list of groups:
        ``[{'fingerprint', 'count', 'samples'}, ...]``, sorted by count
        descending, capped to ``_ERROR_GROUPS_MAX``.
        """
        groups = {}
        order = []
        for line in raw.splitlines():
            line = line.rstrip()
            if not line:
                continue
            fp = self._fingerprint(line)
            if not fp:
                continue
            if fp not in groups:
                groups[fp] = {'fingerprint': fp, 'count': 0, 'samples': []}
                order.append(fp)
            grp = groups[fp]
            grp['count'] += 1
            if len(grp['samples']) < _ERROR_SAMPLE_PER_GRP:
                grp['samples'].append(line[:512])
        ordered = sorted(
            (groups[fp] for fp in order),
            key=itemgetter('count'),
            reverse=True,
        )
        return ordered[:_ERROR_GROUPS_MAX]

    # ── Outcome ───────────────────────────────────────────────────────────

    async def on_success(self, results):
        inst = self._inst()

        # Always stamp the timestamp so the schedule visibly advances
        # even when the cycle is a no-op (skipped).
        inst.write({'last_health_check': odoo_fields.Datetime.now()})

        if getattr(self, '_skipped', False):
            # Reset streaks too: a fresh deploy/rebuild typically wipes
            # the previous baseline, so resuming from streak=1 next
            # cycle would be misleading.
            inst.write({
                'cpu_over_threshold_streak': 0,
                'mem_over_threshold_streak': 0,
            })
            return

        if not self._container_running:
            inst.write({
                'running': False,
                'status': 'error',
                'cpu_over_threshold_streak': 0,
                'mem_over_threshold_streak': 0,
            })
            self._inst_alert(
                'instance_down',
                f"Container 'odoo' is not running on '{inst.name}'.",
                level='critical',
            )
            self._sys(f"✗ Container down — '{inst.name}' is not running.")
            return

        # Container is up — resolve down alert if it existed
        self._resolve_inst_alert('instance_down')
        inst.write({'running': True})

        issues = []

        # HTTP health
        if not self._http_ok:
            issues.append('unresponsive')
            self._inst_alert(
                'instance_unresponsive',
                f"Odoo HTTP health check failed on '{inst.name}' (port 8069 not responding).",
                level='critical',
            )
            self._sys("✗ HTTP health check failed — Odoo not responding.")
        else:
            self._resolve_inst_alert('instance_unresponsive')

        # Memory — with hysteresis
        mem_over = self._mem_current >= _MEM_WARN_PCT
        new_mem_streak = (inst.mem_over_threshold_streak + 1) if mem_over else 0
        inst.write({'mem_over_threshold_streak': new_mem_streak})
        if mem_over and new_mem_streak >= _STREAK_REQUIRED:
            issues.append(f'mem:{self._mem_current:.0f}%')
            self._inst_alert(
                'instance_high_memory',
                f"Memory at {self._mem_current:.0f}% on '{inst.name}' "
                f"for {new_mem_streak} consecutive checks "
                f"(threshold {_MEM_WARN_PCT:.0f}%).",
                level='warning',
            )
            self._sys(
                f"⚠ Memory: {self._mem_current:.0f}% "
                f"(streak {new_mem_streak}/{_STREAK_REQUIRED})"
            )
        elif not mem_over:
            self._resolve_inst_alert('instance_high_memory')

        # CPU — with hysteresis
        cpu_over = self._cpu_current >= _CPU_WARN_PCT
        new_cpu_streak = (inst.cpu_over_threshold_streak + 1) if cpu_over else 0
        inst.write({'cpu_over_threshold_streak': new_cpu_streak})
        if cpu_over and new_cpu_streak >= _STREAK_REQUIRED:
            issues.append(f'cpu:{self._cpu_current:.0f}%')
            self._inst_alert(
                'instance_high_cpu',
                f"CPU at {self._cpu_current:.0f}% on '{inst.name}' "
                f"for {new_cpu_streak} consecutive checks "
                f"(threshold {_CPU_WARN_PCT:.0f}%).",
                level='warning',
            )
            self._sys(
                f"⚠ CPU: {self._cpu_current:.0f}% "
                f"(streak {new_cpu_streak}/{_STREAK_REQUIRED})"
            )
        elif not cpu_over:
            self._resolve_inst_alert('instance_high_cpu')

        # Error logs — dedupe groups + payload
        if len(self._error_groups) >= _ERROR_GROUPS_WARN:
            issues.append(f'errors:{self._error_total}')
            top = self._error_groups[0]
            top_sample = (top['samples'][0] if top['samples'] else top['fingerprint'])
            self._inst_alert(
                'instance_error_logs',
                (
                    f"{self._error_total} ERROR line(s) in "
                    f"{len(self._error_groups)} group(s) on '{inst.name}'. "
                    f"Top: {top_sample[:200]}"
                ),
                level='warning',
                payload=self._error_groups,
            )
            self._sys(
                f"⚠ {self._error_total} ERROR lines in logs "
                f"({len(self._error_groups)} unique group(s))."
            )
        else:
            self._resolve_inst_alert('instance_error_logs')

        # Derive overall status
        if 'unresponsive' in issues:
            inst.write({'status': 'error'})
        elif issues:
            inst.write({'status': 'warning'})
        else:
            inst.write({'status': 'ok'})
            self._sys(
                f"✓ '{inst.name}' healthy — "
                f"CPU {self._cpu_current:.0f}% · Mem {self._mem_current:.0f}%"
            )

    # ── Instance-scoped alert helpers ─────────────────────────────────────

    def _inst_alert(self, code, message, level='warning', payload=None):
        inst = self._inst()
        with self.job.env.registry.cursor() as cr:
            env = self.job.env(cr=cr)
            Alert = env['cloud.alert']
            existing = Alert.search([
                ('instance_id', '=', inst.id),
                ('code', '=', code),
                ('state', '=', 'active'),
            ], limit=1)
            vals = {
                'message': message,
                'level': level,
                'job_id': self.job.id,
                'payload': payload,
            }
            if existing:
                existing.write(vals)
            else:
                Alert.create({
                    'instance_id': inst.id,
                    'code': code,
                    **vals,
                })

    def _resolve_inst_alert(self, code):
        inst = self._inst()
        with self.job.env.registry.cursor() as cr:
            env = self.job.env(cr=cr)
            env['cloud.alert'].search([
                ('instance_id', '=', inst.id),
                ('code', '=', code),
                ('state', '=', 'active'),
            ]).write({'state': 'dismissed'})
