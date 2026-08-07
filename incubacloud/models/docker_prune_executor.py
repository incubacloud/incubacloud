from .ansible_executor import AnsibleExecutor

# Docker label that opts a resource out of the prune. Generic on
# purpose: anything that must survive while stopped can carry it (only
# consumer today is the SaaS warm pool, whose instances are stopped
# containers by design). Defined here because the playbook this
# executor drives is what acts on it.
PROTECT_LABEL = "incubacloud.protect=1"


class DockerPruneExecutor(AnsibleExecutor):
    """Reclaim disk on a host by pruning unused Docker resources.

    Runs ``ansible/playbooks/host_maintenance.yml`` (``docker system
    prune -af``, excluding resources labelled ``PROTECT_LABEL``). The
    playbook exports the reclaimed-space line so the job log keeps
    showing how much was freed; a failure of the prune command fails
    the play, which the default ``parse_results`` turns into a job
    failure.
    """

    _job_type = "docker_prune"
    _playbook = "playbooks/host_maintenance.yml"

    def get_extra_vars(self):
        """Hand the playbook the label whose resources must be spared.

        Passing it (rather than letting the playbook's own default
        stand) keeps this module the single source of truth for the
        label, shared with whatever stamps it onto its containers.
        """
        return {"ic_protect_label": PROTECT_LABEL}

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
