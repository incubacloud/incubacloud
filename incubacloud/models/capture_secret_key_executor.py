"""Read the encryption key an instance holds on its host into the panel.

Instances deployed before the panel kept their keys (core 1.0.189) have
one only on their host; so does an imported instance. Until a deploy or
rebuild reads it, losing that host loses the key. The daily
``cron_capture_secret_keys`` queues this job for every such instance.

Nothing on the host changes: the file is only read (see
``SecretKeyEscrowMixin._read_host_secret_key``, which keeps it out of the
job log).
"""
from .abstract_executor import AbstractSSHExecutor
from .password_utils import merge_secret_key_lines
from .secret_key_escrow import SECRET_KEY_ENV, SecretKeyEscrowMixin

#: Raised when an instance's key still exists on its host only.
ALERT_CODE_NOT_ESCROWED = "secret_key_not_escrowed"


class CaptureSecretKeyExecutor(SecretKeyEscrowMixin, AbstractSSHExecutor):
    """Save the key chain an instance's host holds as the panel's copy."""

    _job_type = "capture_secret_key"

    def _inst(self):
        """Return the instance whose key is read."""
        return self.job.instance_id

    async def before_execute(self, transport):
        """Read the host's file and keep it, merged with the panel's copy.

        :param transport: an open transport to the instance's host
        """
        inst = self._inst()
        if not inst:
            raise ValueError("capture_secret_key job has no instance_id")
        held = await self._read_host_secret_key(transport)
        if not merge_secret_key_lines(held):
            self._sys(
                f"No {SECRET_KEY_ENV} on the host: the panel's key is "
                f"written there by the next deploy or rebuild."
            )
        self._keep_secret_key(held)
        self._sys("✓ The panel keeps this instance's encryption key.")

    def get_commands(self):
        """Nothing to run: the reading happens in ``before_execute``."""
        return []

    async def on_success(self, results):
        """Close the alert a previous failed reading may have left.

        Outcome hooks run on a durable cursor already (``_dispatch_outcome``).
        """
        self.env["cloud.alert"].resolve_alert(
            ALERT_CODE_NOT_ESCROWED, instance=self._inst(),
        )

    async def on_failure(self, results, errors):
        """Warn that the key still lives on the host only.

        Confirmed over two daily readings (``cloud.alert.policy``), so a
        host rebooting at the wrong minute is not news.
        """
        for err in errors:
            self._sys(f"✗ {err}")
        inst = self._inst()
        self.env["cloud.alert"].raise_alert(
            ALERT_CODE_NOT_ESCROWED,
            (
                f"The encryption key of '{inst.name}' exists on its host "
                f"only: the panel could not read it. If that host is "
                f"lost, the instance's encrypted settings are lost with it."
            ),
            level="warning",
            instance=inst,
        )

    async def on_abort(self, exc):
        """A reading that never ran (host unreachable) failed like one that did."""
        await self.on_failure({}, [f"{type(exc).__name__}: {exc}"])
