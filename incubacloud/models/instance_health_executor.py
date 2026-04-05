from contextlib import suppress
from datetime import timezone as tz

from odoo import fields as odoo_fields

from .abstract_executor import AbstractSSHExecutor

# Alert thresholds
_MEM_WARN_PCT   = 85.0   # memory peak % that triggers a warning
_CPU_WARN_PCT   = 90.0   # CPU peak % that triggers a warning
_ERROR_LOG_WARN = 5      # ERROR log lines since last check that trigger a warning

# Seconds of docker-stats streaming used to detect CPU/memory peaks.
# docker stats refreshes ~every second, so this gives N readings.
# NOTE: this covers only a window at check time, not the full 5-min interval.
# Docker does not expose historical per-container stats natively.
_STATS_SAMPLE_SECS = 20


class InstanceHealthExecutor(AbstractSSHExecutor):
    """Checks the health of a deployed doodba instance.

    Checks performed:
    1. Container state — is the ``odoo`` container running?
    2. HTTP health — does Odoo respond on port 8069? (exec inside container)
    3. CPU/memory peak — stream docker stats for ~20 s and take the max values.
    4. Error logs — count ERROR lines since the last health check.

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

    # ── AbstractSSHExecutor interface ─────────────────────────────────────

    def get_commands(self):
        inst = self._inst()
        d = self._inst_dir(inst)

        # Format last_health_check as RFC3339 UTC for "docker logs --since"
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
            # 2. CPU/Memory peak — stream stats for N seconds, report max
            (
                "cpu_mem_peak",
                f"CTNR=$(cd {d} && docker compose ps -q odoo 2>/dev/null | head -1) && "
                f"[ -n \"$CTNR\" ] && "
                f"timeout {_STATS_SAMPLE_SECS} docker stats "
                f"--format '{{{{.CPUPerc}}}}\t{{{{.MemPerc}}}}' \"$CTNR\" 2>/dev/null | "
                f"sed 's/%//g' | "
                f"awk -F'\\t' '"
                f"BEGIN{{mc=0;mm=0}}"
                f"{{gsub(/[^0-9.]/,\"\",$1);gsub(/[^0-9.]/,\"\",$2);"
                f"c=$1+0;m=$2+0;if(c>mc)mc=c;if(m>mm)mm=m}}"
                f"END{{printf \"%.1f\\t%.1f\\n\",mc,mm}}' "
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
            # 4. ERROR lines in odoo logs since last check
            (
                "error_count",
                f"cd {d} && "
                f"docker compose logs --since '{since}' odoo 2>&1 "
                f"| grep -c 'ERROR' || echo 0",
            ),
        ]

    async def before_execute(self, transport):
        inst = self._inst()
        self._sys(f"Checking health of '{inst.name}'…")

    def parse_results(self, results):
        # 1. Container state
        state_out = results.get("container_state", {}).get("stdout", "")
        self._container_running = False
        for line in state_out.splitlines():
            parts = line.strip().split('\t')
            if len(parts) >= 2 and parts[0].strip() == 'odoo':
                self._container_running = parts[1].strip().lower() == 'running'
                break

        # 2. CPU / Memory peak
        cm_out = results.get("cpu_mem_peak", {}).get("stdout", "0.0\t0.0").strip()
        self._cpu_peak = 0.0
        self._mem_peak = 0.0
        parts = cm_out.split('\t')
        if len(parts) >= 2:
            with suppress(ValueError):
                self._cpu_peak = float(parts[0].strip())
                self._mem_peak = float(parts[1].strip())

        # 3. HTTP health
        http_out = results.get("http_health", {}).get("stdout", "").strip()
        self._http_ok = "exit:0" in http_out

        # 4. Error count
        err_out = results.get("error_count", {}).get("stdout", "0").strip()
        try:
            self._error_count = int(err_out.splitlines()[-1])
        except (ValueError, IndexError):
            self._error_count = 0

        return []  # never hard-fail the job — report via status/alerts

    async def on_success(self, results):
        inst = self._inst()

        # Always update last_health_check
        inst.write({'last_health_check': odoo_fields.Datetime.now()})

        if not self._container_running:
            inst.write({'running': False, 'status': 'error'})
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

        # Memory
        if self._mem_peak >= _MEM_WARN_PCT:
            issues.append(f'mem:{self._mem_peak:.0f}%')
            self._inst_alert(
                'instance_high_memory',
                f"Peak memory at {self._mem_peak:.0f}% on '{inst.name}' "
                f"(threshold {_MEM_WARN_PCT:.0f}%).",
                level='warning',
            )
            self._sys(f"⚠ Memory peak: {self._mem_peak:.0f}%")
        else:
            self._resolve_inst_alert('instance_high_memory')

        # CPU
        if self._cpu_peak >= _CPU_WARN_PCT:
            issues.append(f'cpu:{self._cpu_peak:.0f}%')
            self._inst_alert(
                'instance_high_cpu',
                f"Peak CPU at {self._cpu_peak:.0f}% on '{inst.name}' "
                f"(threshold {_CPU_WARN_PCT:.0f}%).",
                level='warning',
            )
            self._sys(f"⚠ CPU peak: {self._cpu_peak:.0f}%")
        else:
            self._resolve_inst_alert('instance_high_cpu')

        # Error logs
        if self._error_count >= _ERROR_LOG_WARN:
            issues.append(f'errors:{self._error_count}')
            self._inst_alert(
                'instance_error_logs',
                f"{self._error_count} ERROR lines in Odoo logs since last check "
                f"on '{inst.name}'.",
                level='warning',
            )
            self._sys(f"⚠ {self._error_count} ERROR lines in logs since last check.")
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
                f"CPU {self._cpu_peak:.0f}% · Mem {self._mem_peak:.0f}%"
            )

    # ── Instance-scoped alert helpers ─────────────────────────────────────

    def _inst_alert(self, code, message, level='warning'):
        inst = self._inst()
        with self.job.env.registry.cursor() as cr:
            env = self.job.env(cr=cr)
            Alert = env['cloud.alert']
            existing = Alert.search([
                ('instance_id', '=', inst.id),
                ('code', '=', code),
                ('state', '=', 'active'),
            ], limit=1)
            if existing:
                existing.write({
                    'message': message,
                    'level': level,
                    'job_id': self.job.id,
                })
            else:
                Alert.create({
                    'instance_id': inst.id,
                    'code': code,
                    'message': message,
                    'level': level,
                    'job_id': self.job.id,
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
