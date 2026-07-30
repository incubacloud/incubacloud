"""Base executor for host-state operations driven by Ansible.

Phase 3 draws a single line through remote execution:

    machine state = Ansible;  application operation = versioned script.

``AbstractExecutor.run_script()`` covers the second half. This module
covers the first: an executor that hands a playbook to ``ansible-runner``
instead of streaming shell commands over the job's SSH transport.

Ansible opens its own SSH connections, so ``AnsibleExecutor`` overrides
``_async_entry()`` and never calls ``cloud.host.get_transport()``. It
still reports through the same plumbing as every other executor — log
chunks, bus updates, ``parse_results`` / ``on_success`` / ``on_failure``
— so a playbook-backed job is indistinguishable in the panel.

The playbook run is blocking, so it goes to a worker thread. That thread
only appends to the log buffer; the ORM is touched exclusively from the
main thread, in the terminal hooks, exactly as in the base class.
"""
import asyncio
import logging
import os
import shutil
import tempfile
from pathlib import Path

import yaml

from .abstract_executor import AbstractExecutor, _looks_like_error

_logger = logging.getLogger(__name__)

try:
    import ansible_runner
except ImportError:
    # Kept importable without the dependency so the addon still loads on
    # an image that has not been rebuilt yet; jobs fail loudly instead.
    ansible_runner = None
    _logger.debug(
        "ansible-runner is not installed — Ansible-backed jobs are unavailable.",
    )


class AnsibleExecutor(AbstractExecutor):
    """Run an Ansible playbook against this job's host.

    Subclasses set ``_playbook`` (path relative to the addon's
    ``ansible/`` directory) and may override ``get_extra_vars()``. Set
    ``_check_mode`` to run with ``--check``, which is how a playbook
    doubles as a drift audit: same code, no changes applied.
    """

    # Path of the playbook inside ``<addon>/ansible/``, e.g.
    # "playbooks/host_probe.yml". Required in subclasses.
    _playbook = None

    # Run with --check (report drift, change nothing).
    _check_mode = False

    def __init__(self, job_record, host_record, sleep_interval=0.5):
        super().__init__(job_record, host_record, sleep_interval)
        # Flipped by _check_cancel so the worker thread's
        # cancel_callback can stop the playbook mid-run.
        self._cancel_requested = False
        # Structured data a playbook hands back through
        # ``ansible.builtin.set_stats`` (see ``playbook_facts``).
        self._facts = {}

    def get_commands(self):
        """Unused: this executor drives ansible-runner, not the command loop.

        Declared because ``AbstractExecutor`` requires it; returning an
        empty list keeps the contract honest for anything that
        introspects an executor without running it.
        """
        return []

    # ===============================
    # INVENTORY / VARS (OVERRIDABLE)
    # ===============================

    def inventory_hostname(self):
        """Return the stable inventory name for this job's host."""
        return f"cloud-host-{self._host_record.id}"

    def get_extra_vars(self):
        """Return the ``--extra-vars`` mapping for the playbook.

        Override to feed a playbook from the job's records. Keep it to
        plain data: everything here is serialised to YAML on disk.
        """
        return {}

    def build_inventory(self, key_path, known_hosts_path):
        """Return the inventory structure for this job's host.

        Host-key checking stays on and is pinned to the key captured by
        the panel's TOFU flow (``cloud.host.known_hosts_key``), so an
        Ansible run trusts exactly what the SSH transport trusts.

        :param key_path: private key file, or None for password auth
        :param known_hosts_path: known_hosts file written for this run
        """
        host = self._host_record
        hostvars = {
            "ansible_host": host.ip_address,
            "ansible_port": host.port,
            "ansible_user": host.user,
            "ansible_python_interpreter": "auto_silent",
            "ansible_ssh_common_args": (
                f"-o UserKnownHostsFile={known_hosts_path} "
                f"-o StrictHostKeyChecking=yes"
            ),
        }
        if key_path:
            hostvars["ansible_ssh_private_key_file"] = key_path
        else:
            # Requires sshpass on the panel image (see dependencies/apt.txt).
            hostvars["ansible_password"] = host.password
        return {"all": {"hosts": {self.inventory_hostname(): hostvars}}}

    # ===============================
    # RUNNER PLUMBING
    # ===============================

    def _build_private_data_dir(self, root):
        """Materialise the ansible-runner private data dir under *root*.

        Lays out ``project/`` (the merged ``ansible/`` overlay of the
        addon chain), ``inventory/hosts.yml`` and the credential files.
        Everything lives inside a 0700 temp dir that the caller removes.
        """
        host = self._host_record
        if not host.known_hosts_key:
            raise RuntimeError(
                f"Host '{host.name}' has no trusted SSH key. "
                f"Open the host page and click 'Trust SSH Key' first."
            )

        project = os.path.join(root, "project")
        for rel, local in self._addon_overlay("ansible").items():
            target = os.path.join(project, rel)
            Path(target).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(local, target)
        if not os.path.isfile(os.path.join(project, self._playbook)):
            raise RuntimeError(
                f"Playbook {self._playbook!r} not found under ansible/ of: "
                f"{', '.join(self._addon_chain())}."
            )

        known_hosts_path = os.path.join(root, "known_hosts")
        self._write_private(known_hosts_path, host.known_hosts_key + "\n")

        key_path = None
        if host.login_type == "ssh_key" and host.key_file:
            key_path = os.path.join(root, "ssh_key")
            self._write_private(
                key_path,
                host._get_ssh_private_key_bytes().decode() + "\n",
            )

        inventory_dir = os.path.join(root, "inventory")
        Path(inventory_dir).mkdir(parents=True, exist_ok=True)
        self._write_private(
            os.path.join(inventory_dir, "hosts.yml"),
            yaml.safe_dump(
                self.build_inventory(key_path, known_hosts_path),
                default_flow_style=False,
            ),
        )

    @staticmethod
    def _write_private(path, content):
        """Write *content* to *path* with 0600 permissions."""
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(content)

    def _emit(self, text):
        """Buffer playbook output as log chunks (called from the worker thread).

        Mirrors ``on_stdout``'s classification. Only touches the log
        buffer — ``list.append`` is all the cross-thread contact there
        is, and the main loop drains it on its usual tick.
        """
        for line in (text or "").splitlines():
            line = line.rstrip()
            if not line:
                continue
            truncated = self._truncate_line(line)
            source = "stderr" if _looks_like_error(truncated) else "stdout"
            self._log_buffer.append((truncated, source))

    def _run_playbook(self, root):
        """Run the playbook synchronously. Returns ``(rc, status)``."""
        runner = ansible_runner.run(
            private_data_dir=root,
            project_dir=os.path.join(root, "project"),
            playbook=self._playbook,
            inventory=os.path.join(root, "inventory", "hosts.yml"),
            extravars=self.get_extra_vars(),
            cmdline="--check" if self._check_mode else None,
            envvars={
                "ANSIBLE_HOST_KEY_CHECKING": "True",
                "ANSIBLE_RETRY_FILES_ENABLED": "False",
                # Ansible's own colour codes would reach the log viewer
                # as escape noise; the panel does its own styling.
                "ANSIBLE_NOCOLOR": "True",
            },
            event_handler=self._on_runner_event,
            cancel_callback=lambda: self._cancel_requested,
            quiet=True,
        )
        return runner.rc, runner.status

    def playbook_facts(self):
        """Return the structured data the playbook exported, if any.

        A playbook hands data back to Python with::

            - ansible.builtin.set_stats:
                data: {ic_...: ...}
                aggregate: false

        which lands in the ``playbook_on_stats`` event's ``artifact_data``.
        ``parse_results`` / ``on_success`` read it to make the same
        decisions the old bash-parsing executors made from stdout —
        keeping the *gathering* on the host and the *judgement* in Python.
        """
        return self._facts

    def _on_runner_event(self, event):
        """ansible-runner event hook: stream output, capture exported stats."""
        self._emit(event.get("stdout"))
        if event.get("event") == "playbook_on_stats":
            artifact = (event.get("event_data") or {}).get("artifact_data")
            if isinstance(artifact, dict):
                self._facts = artifact
        return True

    def _check_cancel(self):
        """Also tell the running playbook to stop before raising."""
        try:
            super()._check_cancel()
        except Exception:
            self._cancel_requested = True
            raise

    # ===============================
    # ASYNC ENTRYPOINT
    # ===============================

    async def _async_entry(self):
        """Run the playbook, then dispatch the usual terminal hooks."""
        if ansible_runner is None:
            raise RuntimeError(
                "ansible-runner is not installed in this image — rebuild it "
                "before running Ansible-backed jobs.",
            )
        if not self._playbook:
            raise RuntimeError(
                f"{type(self).__name__} does not define a playbook.",
            )

        mode = " (check mode)" if self._check_mode else ""
        self._sys(f"Running playbook {self._playbook}{mode} on {self.host}...")
        root = tempfile.mkdtemp(prefix="incubacloud-ansible-")
        os.chmod(root, 0o700)
        try:
            self._build_private_data_dir(root)
            rc, status = await asyncio.get_running_loop().run_in_executor(
                None, self._run_playbook, root,
            )
        except Exception as e:
            _logger.exception("[ansible] playbook failed: %s", e)
            self._sys(f"✗ {type(e).__name__}: {e}")
            raise
        finally:
            # The private data dir holds the host's private key and the
            # runner artifacts; it never outlives the job.
            shutil.rmtree(root, ignore_errors=True)

        self._sys(f"Playbook finished: {status} (rc={rc}).")
        results = {
            self._playbook: {"stdout": "", "exit_status": rc},
        }
        await self._dispatch_outcome(results)
