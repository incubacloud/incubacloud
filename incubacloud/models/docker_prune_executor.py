from .ansible_executor import AnsibleExecutor


class DockerPruneExecutor(AnsibleExecutor):
    """Reclaim disk on a host by pruning unused Docker resources.

    Runs ``ansible/playbooks/host_maintenance.yml`` (``docker system
    prune -af``). The playbook exports the reclaimed-space line so the
    job log keeps showing how much was freed; a failure of the prune
    command fails the play, which the default ``parse_results`` turns
    into a job failure.
    """

    _job_type = "docker_prune"
    _playbook = "playbooks/host_maintenance.yml"

    async def before_execute(self, transport):
        self._sys("Starting Docker cleanup...")

    async def on_success(self, results):
        stdout = self.playbook_facts().get("ic_prune_stdout", "")
        # Surface the "Total reclaimed space" line if the prune printed one.
        for line in reversed(stdout.splitlines()):
            if "reclaimed" in line.lower():
                self._sys(f"✓ {line.strip()}")
                return
        self._sys("✓ Docker cleanup complete.")

    async def on_failure(self, results, errors):
        self._sys(f"✗ Docker cleanup failed: {'; '.join(errors)}")
