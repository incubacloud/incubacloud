from .abstract_executor import AbstractSSHExecutor


class DeleteProjectExecutor(AbstractSSHExecutor):
    """Remove the project directory from a remote host.

    Host-level job triggered when a cloud.project is unlinked. Works with
    the remote_folder passed in the job payload so it does not depend on
    the (already removed) project record.

    Double-safety: before rm -rf, it checks that no docker-compose project
    under ~/{remote_folder}/ has running containers. If it does, the job
    fails — the caller should have stopped/deleted instances first.
    """

    _job_type = "delete_project"

    def _remote_folder(self):
        payload = self.job.payload or {}
        folder = (payload.get('remote_folder') or '').strip()
        if not folder or folder in ('/', '~', '.', '..'):
            raise ValueError(
                f"delete_project: invalid remote_folder: {folder!r}"
            )
        if (
            '/' in folder
            or folder.startswith('.')
            or folder.startswith('-')
        ):
            # Leading '-' would be interpreted as a flag by any tool
            # that consumed the value alone (rm, find, etc.). Reject
            # at the executor boundary even though cloud.project's
            # regex already blocks it on write — defense in depth for
            # any future caller that builds the payload differently.
            raise ValueError(
                f"delete_project: unsafe remote_folder: {folder!r}"
            )
        return folder

    def get_commands(self):
        folder = self._remote_folder()
        d = f"~/{folder}"
        return [
            (
                "Check no running containers in project dir",
                self.run_script("project_containers_check.sh", [d]),
                {"stop_on_failure": True},
            ),
            # Left inline: a lone ``rm -rf`` is not an operation worth a
            # versioned script, and unquoted the remote shell expands
            # the ``~``.
            (
                "Remove project directory",
                f"rm -rf {d}",
            ),
        ]

    async def on_success(self, results):
        folder = (self.job.payload or {}).get('remote_folder', '?')
        self._sys(f"✓ Project directory '~/{folder}' removed.")

    async def on_failure(self, results, errors):
        for err in errors:
            self._sys(f"✗ {err}")
