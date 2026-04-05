from odoo import fields
from .abstract_executor import AbstractSSHExecutor

DISK_CRITICAL_THRESHOLD = 95  # % — mark host degraded and raise alert above this


class HostMetricsExecutor(AbstractSSHExecutor):
    """Collects static server specs (CPU cores, total RAM) and disk health.

    Runs silently in the background — stdout is suppressed, only a single
    system summary line is written to the job log.

    Can be triggered from the cron (every 5 min) or after any other job
    that may have altered disk state (e.g. a deployment).
    """

    _job_type = "host_metrics"

    def get_commands(self):
        return [
            ("cpu_cores",    "nproc"),
            ("ram_total_gb", "free -b | awk '/^Mem:/ {printf \"%.1f\", $2/1073741824}'"),
            ("disk_usage",   "df / | awk 'NR==2 {gsub(/%/,\"\",$5); print $5}'"),
            ("disk_free_gb", "df -B1 / | awk 'NR==2 {printf \"%.1f\", $4/1073741824}'"),
        ]

    async def before_execute(self, transport):
        self._sys("Collecting host specs...")

    # Suppress raw command output — these are just numbers, not for display
    async def on_stdout(self, line):
        pass

    async def on_stderr(self, line):
        pass

    def parse_results(self, results):
        self._cpu_cores    = int(self._safe_float(results.get("cpu_cores",    {}).get("stdout", "")))
        self._ram_total_gb =     self._safe_float(results.get("ram_total_gb", {}).get("stdout", ""))
        self._disk_usage   =     self._safe_float(results.get("disk_usage",   {}).get("stdout", ""))
        self._disk_free_gb =     self._safe_float(results.get("disk_free_gb", {}).get("stdout", ""))
        return []  # informational — never fails

    async def on_success(self, results):
        host = self.job.host_id
        host.write({
            'cpu_cores':    self._cpu_cores,
            'ram_total_gb': self._ram_total_gb,
            'disk_usage':   self._disk_usage,
            'disk_free_gb': self._disk_free_gb,
            'last_probed':  fields.Datetime.now(),
        })

        if self._disk_usage >= DISK_CRITICAL_THRESHOLD:
            # Escalate to degraded only if currently compatible
            if host.status == 'compatible':
                host.write({'status': 'degraded'})
            self._alert(
                'disk_critical',
                f"Disk at {int(self._disk_usage)}% — "
                f"only {self._disk_free_gb:.1f} GB free",
                level='critical',
            )
            self._sys(
                f"⚠ Disk critical: {int(self._disk_usage)}% used, "
                f"{self._disk_free_gb:.1f} GB free"
            )
        else:
            self._resolve_alert('disk_critical')
            # Restore to compatible only if we were the ones who degraded it
            if host.status == 'degraded':
                host.write({'status': 'compatible'})
            self._sys(
                f"✓ {self._cpu_cores} cores · "
                f"{self._ram_total_gb:.1f} GB RAM · "
                f"{int(self._disk_usage)}% disk "
                f"({self._disk_free_gb:.1f} GB free)"
            )



    def _safe_float(self, raw, default=0.0):
        try:
            return float((raw or '').strip())
        except (ValueError, TypeError):
            return default
