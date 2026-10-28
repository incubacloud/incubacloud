"""The panel keeps each instance's encryption key; hosts only receive it.

An instance that runs this module encrypts its own secrets with the
``INCUBACLOUD_SECRET_KEY`` chain in ``.docker/incubacloud.env``. That file
used to exist on its host only: ``deploy.sh ensure-secret-key`` generated
it on the first deploy, and nothing ever read it back. An instance moved
to another host, or rebuilt on a new one, got a new key, and its restored
database could no longer read what it had encrypted with the old one
(two panels, 18-oct-2026).

``cloud.instance.secret_key`` is now the panel's copy. Every deploy and
rebuild reads what the host holds first and merges it in
(``merge_secret_key_lines``: no key is ever dropped), saves the result,
and writes it to the host. A restore that brings another instance's data
without neutralizing it gives the receiving one the sender's keys as well.
"""
import logging

from .password_utils import generate_secret_key, merge_secret_key_lines

_logger = logging.getLogger(__name__)

#: Where the instance's Odoo reads ``INCUBACLOUD_SECRET_KEY`` from,
#: relative to its directory (compose ``env_file``).
SECRET_KEY_ENV = ".docker/incubacloud.env"
#: Label of the step that puts the panel's copy of the key in place.
LABEL_WRITE_SECRET_KEY = "Write incubacloud.env"


class SecretKeyEscrowMixin:
    """Read, keep and ship an instance's key chain, for instance executors.

    Expects the executor API: ``_inst()``, ``_inst_dir()``, ``job``,
    ``_persist()``, ``_job_path()``, ``_upload_private_files()`` and
    ``_sys()``.
    """

    def _secret_key_tmp(self):
        """Return where the key file is staged on the job's host.

        :return: a path in the job's own directory
        :rtype: str
        """
        return self._job_path("incubacloud.env")

    async def _read_host_secret_key(self, transport, inst=None):
        """Return what an instance's ``incubacloud.env`` holds on a host.

        Read with ``transport.run``, which logs nothing, never as a step:
        a step streams its output into the job log.

        :param transport: an open transport to the host to read from
        :param inst: the instance whose file is read (default: the job's)
        :return: the file's content, or ``""`` when there is none
        :rtype: str
        :raise RuntimeError: when the host could not be read, or its file
            holds something but no key the panel can read; the caller must
            not write over a key it could not compare
        """
        path = f"{self._inst_dir(inst or self._inst())}/{SECRET_KEY_ENV}"
        result = await transport.run(
            f'f={path}; if [ -f "$f" ]; then cat "$f"; fi',
        )
        if result.exit_status != 0:
            raise RuntimeError(
                f"Could not read {SECRET_KEY_ENV} on the host "
                f"(exit {result.exit_status})."
            )
        held = result.stdout or ""
        if held.strip() and not merge_secret_key_lines(held):
            raise RuntimeError(
                f"{SECRET_KEY_ENV} on the host holds no key the panel can "
                f"read; it is left as it is. Check its format by hand."
            )
        return held

    async def _read_secret_key_where_it_lives(self, transport):
        """Return what the instance's file holds on the host it lives on.

        That is the job's host, except during a move: the job then runs
        on the target while the instance — and the only copy of its key —
        is still on the origin. An instance not deployed has nothing on
        any host.

        :param transport: the job's open transport
        :return: the file's content, or ``""``
        :rtype: str
        """
        inst = self._inst()
        lives_on = inst.host_id
        if not (inst.deployed and lives_on):
            return ""
        if lives_on == self.job.host_id:
            return await self._read_host_secret_key(transport)
        self._sys(
            f"Reading the instance's encryption key on {lives_on.name}…"
        )
        async with lives_on.get_transport() as origin:
            return await self._read_host_secret_key(origin)

    def _keep_secret_key(self, *chains):
        """Save the merge of *chains* as the instance's key chain.

        The instance's own copy is merged in last, so nothing it held is
        lost either. A new key is generated when none of them holds one.
        Written durably (``_persist``) before anything reaches the host,
        so the panel always holds what a host holds. The value is never
        logged; who changed it is.

        :param chains: key chains or env file contents, primary first
        :return: the chain the instance now keeps
        :rtype: str
        """
        inst = self._inst()
        kept = inst.sudo().secret_key or ""
        chain = merge_secret_key_lines(*chains, kept) or generate_secret_key()
        if chain != kept:
            self._persist(inst.sudo(), {"secret_key": chain})
            _logger.info(
                "Job %s: the panel now keeps the encryption key of "
                "instance %s (%d key(s)).",
                self.job.id, inst.id, chain.count(",") + 1,
            )
        return chain

    async def _stage_secret_key(self, transport, chain):
        """Upload *chain* as the env file the write step moves into place.

        :param transport: the job's open transport
        :param str chain: the key chain to write
        """
        await self._upload_private_files(
            transport,
            {self._secret_key_tmp(): f"INCUBACLOUD_SECRET_KEY={chain}\n"},
        )

    async def _ship_secret_key(self, transport):
        """Keep the instance's key chain and stage it for its host.

        What the host holds comes first, so the key the running Odoo
        encrypts with stays the primary one; the panel's copy is merged
        in after it.

        :param transport: the job's open transport
        """
        held = await self._read_secret_key_where_it_lives(transport)
        await self._stage_secret_key(transport, self._keep_secret_key(held))

    def _write_secret_key_step(self, d):
        """Return the step that puts the staged key file in place.

        Deploy and rebuild run it after copier (which creates
        ``.docker/``) and before ``ensure-secret-key``, which then finds
        the file and keeps it. No staged file: nothing to do.

        :param str d: the instance directory
        :return: ``(label, command)``
        :rtype: tuple
        """
        tmp = self._secret_key_tmp()
        dst = f"{d}/{SECRET_KEY_ENV}"
        return (
            LABEL_WRITE_SECRET_KEY,
            f'f={tmp}; if [ -f "$f" ]; then mkdir -p {d}/.docker'
            f' && mv "$f" {dst} && chmod 600 {dst}; fi',
        )
