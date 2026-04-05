from .abstract_executor import AbstractSSHExecutor


class DockerPruneExecutor(AbstractSSHExecutor):
    """Runs ``docker system prune -af`` on the host to reclaim disk space.

    Removes all unused containers, images, networks and build cache.
    Stdout is forwarded to the job log so the reclaimed space summary is visible.
    """

    _job_type = "docker_prune"

    def get_commands(self):
        return [
            ("prune", "docker system prune -af 2>&1"),
        ]

    async def before_execute(self, transport):
        self._sys("Starting Docker cleanup...")

    def parse_results(self, results):
        prune = results.get("prune", {})
        if prune.get("exit_status", 0) != 0:
            return [f"docker system prune failed (exit {prune['exit_status']})"]
        return []

    async def on_success(self, results):
        stdout = results.get("prune", {}).get("stdout", "")
        # Extract "Total reclaimed space" line for the summary message
        for line in reversed(stdout.splitlines()):
            if "reclaimed" in line.lower():
                self._sys(f"✓ {line.strip()}")
                return
        self._sys("✓ Docker cleanup complete.")

    async def on_failure(self, results, errors):
        self._sys(f"✗ Docker cleanup failed: {'; '.join(errors)}")
