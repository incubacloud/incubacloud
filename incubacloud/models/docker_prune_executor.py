import re
import shlex

from .abstract_executor import HANDOFF_SWEEP, JOB_DIR_PREFIX, handoff_sweep
from .ansible_executor import AnsibleExecutor
from .cloud_restore_upload_grant import UPLOAD_ROOT, USED_GRANT_DAYS

# Docker label that opts a resource out of the prune. Stamped by
# ``DeployInstanceExecutor._resource_override_content`` on every
# panel-deployed service, because any of them may legitimately sit
# stopped (a warm pool spare, a Sablier-slept free instance, a
# manually stopped one) and an unlabelled stopped container *matches*
# the ``label!=`` filter below. Defined here because the playbook this
# executor drives is what acts on it.
#
# The project's network is NOT labelled and NOT pruned — see the play's
# own comment, and ``_resource_override_content`` for why labelling it
# is worse than the disease.
PROTECT_LABEL = "incubacloud.protect=1"

# Alert raised when the post-prune inventory shows that a container the
# panel manages is gone. With every managed resource labelled this
# should never fire; it exists precisely because it once did (2026-08:
# rebuilds rewrote override.yml without the label and the nightly prune
# swept the warm pool and failed the free-host backups for two days).
SWEPT_ALERT_CODE = "prune_swept_managed"

# Services whose container must exist (any state) for every deployed
# instance. Deliberately the unconditional pair from
# ``cloud.instance.expected_services`` — checking by project prefix
# would miss a Sablier-slept free instance whose ``odoo`` was swept
# while its still-running ``db``/``smtp`` kept the prefix alive.
_REQUIRED_SERVICES = ("odoo", "db")

# Alert raised when a project network the panel manages is gone after
# the prune. Distinct from the container alert because the remedy is
# different: the containers are still there, they simply cannot attach
# to anything, so the stack has to be recreated rather than restored.
NETWORK_ALERT_CODE = "prune_swept_network"

#: What releases before 1.0.200 named, at /tmp, the files a job staged or
#: built and the directories it worked in: archives of customers'
#: databases, a deploy's copier answers, configuration and secrets, a
#: purge's bucket keys, the proxy's private keys and certificate store,
#: the scripts a job ran, and the SaaS layer's tenant dumps and files.
#: Created 0644 and left there by a job that ended early.
_LEGACY_TMP_NAMES = (
    ".incubacloud-handoff-*.zip",
    ".incubacloud-backup-*.zip",
    ".incubacloud-bkdl-*",
    ".incubacloud-bkneu-*",
    ".incubacloud-export-*",
    ".incubacloud-archived-purge-*",
    ".incubacloud-*-answers.yml",
    ".incubacloud-*-addons.yaml",
    ".incubacloud-*-repos.yaml",
    ".incubacloud-*-odoo.conf",
    ".incubacloud-*-pip.txt",
    ".incubacloud-*-apt.txt",
    ".incubacloud-*-override.yml",
    ".incubacloud-*-extra-*",
    ".incubacloud-*-backup.env",
    ".incubacloud-*-incubacloud.env",
    ".incubacloud-acme.json",
    ".incubacloud-traefik-*",
    ".incubacloud-whitelist.yaml",
    ".incubacloud-github-webhook.yml",
    ".incubacloud-scripts-*",
    ".icfree-*",
    ".ic-modules-*",
    ".ic-sablier*",
)

#: How old a job's directory must be before the day's sweep takes it:
#: a job removes its own, and one whose worker died does not. No job
#: runs this long — a step's command stops after an hour, a download
#: from a link after a day.
_JOB_DIR_AGE_MINUTES = 3 * 24 * 60


def legacy_tmp_sweep(root="/tmp"):
    """Return the command that removes older releases' leftovers in *root*.

    Files and directories under those names right in *root*, a day old,
    first this account's (``-user``): ``find`` follows no link, a link
    is neither a file nor a directory to it, and ``rm -rf`` does not
    follow one inside a directory either. Nothing this release creates
    is named like them.

    Then the privileged pass, for what the account cannot remove: root's
    (before a hardening moves the panel to an operator account it
    connected as root) and uid 1000's, the odoo container's, which wrote
    the live dumps and exports at those names through its ``/tmp``
    mount (``instance_logs.sh``) and is not the operator on a platform
    host (root) or on a VPS whose operator has another uid. Without
    root the pass asks ``sudo -n``, and only after a search the account
    can do itself found something: a host without sudoers would
    otherwise report an incident, and mail root, every day. Where the
    account is root it runs directly, with no search. It never fails
    the play. After its first run on a host it finds nothing.

    :param str root: the directory to sweep, ``/tmp`` but in tests
    :rtype: str
    """
    names = " -o ".join(
        f"-name {shlex.quote(name)}" for name in _LEGACY_TMP_NAMES
    )
    match = (
        "-mmin +1440 \\( -type f -o -type d \\)"
        f" \\( {names} \\)"
    )
    where = f"find {shlex.quote(root)} -mindepth 1 -maxdepth 1"
    others = "\\( -user 0 -o -user 1000 \\)"
    remove = f"{where} {others} {match} -exec rm -rf {{}} +"
    return (
        f'{where} -user "$(id -u)" {match} -exec rm -rf {{}} +;'
        f' if [ "$(id -u)" = 0 ]; then {remove};'
        f" elif {where} {others} {match} -print -quit 2>/dev/null"
        f" | grep -q .; then sudo -n {remove};"
        " fi 2>/dev/null || true"
    )


def job_dir_sweep(root="/tmp"):
    """Return the command that removes the job directories a dead worker
    left in *root*: this account's, three days old (``JOB_DIR_PREFIX``).

    :param str root: the directory to sweep, ``/tmp`` but in tests
    :rtype: str
    """
    return (
        f"find {shlex.quote(root)} -mindepth 1 -maxdepth 1 -type d"
        f" -name '{JOB_DIR_PREFIX[5:]}*' -user \"$(id -u)\""
        f" -mmin +{_JOB_DIR_AGE_MINUTES} -exec rm -rf {{}} +"
    )


def upload_dir_sweep(root=UPLOAD_ROOT):
    """Return the command that removes old upload keys' directories.

    The panel revokes a consumed grant ``USED_GRANT_DAYS`` after its
    restore, directory and all (``cloud.restore.upload.grant``); this is
    the safety net for a host where that job never ran. Only directories
    named as a grant's token, this account's and that old, right under
    a root that is this account's own directory and not a link — in a
    root another account owns, a name could be swapped for a link
    between the search and the removal. ``find`` follows no link, and
    ``rm -rf`` none inside a directory either.

    :param str root: the grants' root, ``UPLOAD_ROOT`` but in tests
    :rtype: str
    """
    quoted = shlex.quote(root)
    token = "[0-9a-f]" * 16
    return (
        f"{{ [ -d {quoted} ] && [ ! -h {quoted} ] && [ -O {quoted} ]"
        f" && find {quoted} -mindepth 1 -maxdepth 1 -type d"
        f" -name '{token}' -user \"$(id -u)\""
        f" -mmin +{USED_GRANT_DAYS * 24 * 60} -exec rm -rf {{}} +; }}"
        " || true"
    )


def tmp_sweep(env, host):
    """Return the day's sweep of /tmp for *host*.

    Its handoffs a day old but those a restore still waits for
    (``handoff_sweep``), older releases' leftovers, the directories of
    jobs whose worker died, and what upload keys left three days ago
    (``upload_dir_sweep``).

    :param env: an environment to read the jobs with
    :param host: the ``cloud.host`` it runs on
    :rtype: str
    """
    return (
        f"{handoff_sweep(env, host)}; {legacy_tmp_sweep()}; {job_dir_sweep()};"
        f" {upload_dir_sweep()}"
    )


#: The day's sweep of /tmp as a playbook run alone has it: with no panel
#: to ask, it spares no handoff (``tmp_sweep``).
TMP_SWEEP = (
    f"{HANDOFF_SWEEP}; {legacy_tmp_sweep()}; {job_dir_sweep()};"
    f" {upload_dir_sweep()}"
)

# Docker prints sizes with SI units (``12.86GB``, ``938.4kB``, ``0B``).
_SIZE_UNITS = {"b": 1, "kb": 10 ** 3, "mb": 10 ** 6, "gb": 10 ** 9,
               "tb": 10 ** 12}
_RECLAIMED_RE = re.compile(
    r"total reclaimed space:\s*([\d.]+)\s*([a-z]*b)", re.IGNORECASE,
)


def _reclaimed_bytes(stdout):
    """Sum every ``Total reclaimed space`` line in *stdout*.

    The play runs three prunes, so its output carries three totals.
    Reporting the last one — which is what reading the output backwards
    used to do — would under-report by whatever the other two freed.

    :param stdout: combined stdout of the prune commands.
    :return: total reclaimed space in bytes.
    """
    return int(sum(
        float(value) * _SIZE_UNITS.get(unit.lower(), 1)
        for value, unit in _RECLAIMED_RE.findall(stdout)
    ))


def _format_size(size):
    """Render a byte count the way Docker does, for a familiar log line.

    :param size: size in bytes.
    :return: a string such as ``12.86GB`` or ``0B``.
    """
    for unit, factor in (
        ("TB", 10 ** 12), ("GB", 10 ** 9), ("MB", 10 ** 6), ("kB", 10 ** 3),
    ):
        if size >= factor:
            # ``.4g`` is what Docker's own humaniser uses, so 1.5GB
            # stays "1.5GB" instead of gaining a padded decimal.
            return f"{size / factor:.4g}{unit}"
    return f"{size}B"


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
        """Hand the playbook the label to spare and the sweep of /tmp.

        Passing them (rather than letting the playbook's own defaults
        stand) keeps this module the single source of truth for the
        label, shared with whatever stamps it onto its containers, and
        for the sweep, whose handoff part the jobs run too and which
        spares the handoffs a restore still waits for on this host.
        """
        return {
            "ic_protect_label": PROTECT_LABEL,
            "ic_tmp_sweep": tmp_sweep(self.env, self.job.host_id),
        }

    async def before_execute(self, transport):
        self._sys("Starting Docker cleanup...")

    def _summarize_prune_output(self, stdout):
        """Log what the prune deleted, by section.

        ``docker system prune`` prints ``Deleted <kind>:`` sections.
        Containers and images are listed by ID/digest — useless for
        post-mortems, so they are logged as counts; networks are listed
        by *name*, which is exactly what an operator needs to see, so
        those are spelled out. The presence check below is what turns
        "a network named like a tenant got deleted" into an alert.
        """
        section = None
        counts = {}
        networks = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("Deleted ") and line.endswith(":"):
                section = line[len("Deleted "):-1].lower()
                counts.setdefault(section, 0)
                continue
            if line.lower().startswith("total reclaimed"):
                section = None
                continue
            if section:
                counts[section] += 1
                if section == "networks":
                    networks.append(line)
        if counts:
            parts = ", ".join(
                f"{n} {kind}" for kind, n in counts.items() if n
            )
            self._sys(f"Deleted: {parts or 'nothing'}.")
        if networks:
            shown = ", ".join(networks[:10])
            more = f" (+{len(networks) - 10} more)" if len(networks) > 10 else ""
            self._sys(f"Deleted networks: {shown}{more}")

    def _instances_to_check(self):
        """Yield the deployed instances whose resources must have survived.

        Instances with an active non-background job are skipped: a
        rebuild's ``compose down`` phase legitimately leaves the stack
        without containers — and without its network — for a while, and
        alerting on that would cry wolf on every rebuild.

        :return: iterator over ``cloud.instance`` records.
        """
        Job = self.job.env["cloud.job"]
        for inst in self.job.host_id.instance_ids.filtered("deployed"):
            busy = Job.search_count([
                ("instance_id", "=", inst.id),
                ("state", "in", Job._active_states),
                ("job_type_id.code", "not in", Job._hidden_job_types),
            ])
            if not busy:
                yield inst

    def _check_managed_containers(self, survivors_raw):
        """Alert if a panel-managed container did not survive the prune.

        Compares the post-prune ``docker ps -a`` inventory against the
        ``odoo``/``db`` containers every deployed instance on this host
        must have (named ``<project>-<service>-1`` by compose, the same
        convention the rest of the panel relies on). Instances with an
        active non-background job are skipped: a rebuild's ``compose
        down`` phase legitimately leaves the stack containerless for a
        while. Raises/refreshes a critical host alert when something is
        missing; resolves it when everything is present again.
        """
        survivors = {
            line.strip() for line in survivors_raw.splitlines() if line.strip()
        }
        missing = []
        checked = 0
        for inst in self._instances_to_check():
            checked += 1
            project = inst.doodba_project_name
            missing.extend(
                name
                for svc in _REQUIRED_SERVICES
                if (name := f"{project}-{svc}-1") not in survivors
            )
        with self._durable_env() as alert_env:
            host = alert_env["cloud.host"].browse(self.job.host_id.id)
            if missing:
                shown = ", ".join(sorted(missing))
                alert_env["cloud.alert"].raise_alert(
                    SWEPT_ALERT_CODE,
                    f"Docker prune on '{host.name}' left managed "
                    f"container(s) missing: {shown}. The protect label "
                    f"should have spared them — a rebuild may be "
                    f"rewriting overrides without it.",
                    level="critical",
                    host=host,
                    job=alert_env["cloud.job"].browse(self.job.id),
                    # The protect label is the platform's to get
                    # right; the operator only sees the damage.
                    audience="provider",
                )
                self._sys(f"✗ Managed container(s) missing after prune: {shown}")
            else:
                alert_env["cloud.alert"].resolve_alert(SWEPT_ALERT_CODE, host=host)
                self._sys(
                    f"✓ All managed stacks intact "
                    f"({checked} instance(s) checked)."
                )

    def _check_managed_networks(self, networks_raw):
        """Alert if a panel-managed project network did not survive.

        The container check cannot see this failure: the containers are
        label-protected and survive, the network is not and does not,
        and the stack is left unable to start at all. A container that
        compose merely *starts* keeps the network id it was created
        with, so a missing network answers ``network <id> not found``
        and the whole ``up`` fails — which is how it stayed invisible
        until a backup tried to wake a stopped stack days later.

        Since the play no longer prunes networks this should never
        fire. It exists because the equivalent container check was
        added for the same reason and still caught a real regression.

        :param networks_raw: ``docker network ls`` output, one name per
            line, gathered after the prune.
        """
        survivors = {
            line.strip() for line in networks_raw.splitlines() if line.strip()
        }
        missing = [
            name
            for inst in self._instances_to_check()
            if (name := f"{inst.doodba_project_name}_default") not in survivors
        ]
        with self._durable_env() as alert_env:
            host = alert_env["cloud.host"].browse(self.job.host_id.id)
            if missing:
                shown = ", ".join(sorted(missing))
                alert_env["cloud.alert"].raise_alert(
                    NETWORK_ALERT_CODE,
                    f"Docker prune on '{host.name}' left managed "
                    f"network(s) missing: {shown}. Their containers "
                    f"cannot attach to anything — the stacks must be "
                    f"recreated, not merely started.",
                    level="critical",
                    host=host,
                    job=alert_env["cloud.job"].browse(self.job.id),
                    # The protect label is the platform's to get
                    # right; the operator only sees the damage.
                    audience="provider",
                )
                self._sys(f"✗ Managed network(s) missing after prune: {shown}")
            else:
                alert_env["cloud.alert"].resolve_alert(
                    NETWORK_ALERT_CODE, host=host,
                )

    async def on_success(self, results):
        facts = self.playbook_facts()
        stdout = facts.get("ic_prune_stdout", "")
        self._summarize_prune_output(stdout)
        # Every prune in the play prints its own total; report the sum,
        # not whichever one happened to run last.
        if "reclaimed" in stdout.lower():
            self._sys(
                f"✓ Total reclaimed space: "
                f"{_format_size(_reclaimed_bytes(stdout))}"
            )
        else:
            self._sys("✓ Docker cleanup complete.")
        survivors_raw = facts.get("ic_containers_after")
        if survivors_raw is None:
            self._sys("Container inventory unavailable — presence check skipped.")
        else:
            self._check_managed_containers(survivors_raw)
        networks_raw = facts.get("ic_networks_after")
        if networks_raw is None:
            self._sys("Network inventory unavailable — presence check skipped.")
        else:
            self._check_managed_networks(networks_raw)

    async def on_failure(self, results, errors):
        self._sys(f"✗ Docker cleanup failed: {'; '.join(errors)}")
