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
                # If the directory does not exist, nothing to do.
                f"if [ ! -d {d} ]; then"
                f"  echo 'Project dir {d} not found — nothing to check.';"
                f"  exit 0;"
                f"fi;"
                # Look for any docker compose project under {d} that still
                # has containers (running OR stopped). We use `docker ps -a`
                # filtered by the compose working-dir label to catch stray
                # containers even if compose.yaml was removed manually.
                f" running=$(docker ps -a --filter"
                f' "label=com.docker.compose.project.working_dir"'
                f" --format '{{{{.Label \"com.docker.compose.project"
                f".working_dir\"}}}}'"
                f" | awk -v d=\"$(readlink -f {d})\""
                f" 'index($0, d)==1' | head -n1);"
                f" if [ -n \"$running\" ]; then"
                f"   echo \"Refusing to delete: containers still exist under"
                f" $running\";"
                f"   exit 1;"
                f" fi;"
                f" echo 'No containers found under {d}.'",
                {"stop_on_failure": True},
            ),
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
