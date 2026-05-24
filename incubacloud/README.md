IncubaCloud
-----------

Cloud infrastructure management from Odoo. Deploy, monitor and manage
[doodba](https://github.com/Tecnativa/doodba)-based Odoo instances on your
own servers via SSH.

Features
--------

- **Host management** — register VPS servers, run health checks, auto-install
  Docker/Traefik/dependencies via a single "Full Setup" command.
  CPU/RAM/disk metrics collected every 5 minutes.
- **Project & instance lifecycle** — create projects, deploy production/staging
  instances with [copier](https://copier.readthedocs.io/), rebuild, restart,
  stop and delete — all from a single-page OWL application.
- **Safe rebuild** — new Docker images are verified with a boot test against a
  cloned database before being applied. If the test fails, the running
  instance keeps the previous image (inspired by odoo.sh).
- **Auto-update modules** — on rebuild, `click-odoo-update` detects which
  modules changed via checksums and updates only those, instead of a
  full `-u all`. Checksums baseline is established on first deploy.
- **GitHub integration** — connect a GitHub App for automatic webhook-triggered
  rebuilds on push, or use a Personal Access Token for private repository
  access during deploys.
- **Backup management** — configure S3-compatible backup backends, list/create/
  download/restore backups powered by [duplicity](https://duplicity.gitlab.io/)
  inside each instance's backup container. Scheduled backup list refresh and
  attachment cleanup via cron.
- **Clone to staging** — one-click clone of a production instance into a staging
  environment, including database restore from the latest backup.
- **Multi-domain support** — assign multiple domains per instance with optional
  redirects, automatically configured in Traefik.
- **SMTP relay** — optional per-instance SMTP configuration. When not
  configured, the smtp service is stripped from `prod.yaml` to avoid
  Docker Compose errors.
- **Async job engine** — all SSH operations run asynchronously via `queue_job`
  with real-time log streaming to the browser. Data race prevention blocks
  concurrent deploy/rebuild on the same instance.
- **Export for development** — download a sanitised copy of any instance
  (tokens, passwords and SSH keys stripped) ready for local development.
- **Role-based access** — five security groups (Stakeholder, Consultant,
  Project Manager, Developer, Administrator) with project-scoped record
  rules.

Frontend (OWL SPA)
------------------

The management UI lives at `/cloud/ui` and is built entirely with OWL
components — no traditional Odoo views.

- **Toast notifications** — global feedback service for all actions
  (success/error/warning/info).
- **Unsaved changes warning** — visual indicator + browser `beforeunload`
  prompt on instance and project forms.
- **Empty states** — all lists show contextual icons and hints when empty.
- **Debounced search** — project switcher and member search inputs.
- **Accessibility** — `aria-label` on icon-only buttons, search inputs and
  slide-over panels.

Architecture
------------

| Model | Description |
|---|---|
| `cloud.host` | Server with IP, port, SSH credentials (key or password) |
| `cloud.project` | Project grouping instances, repos and members |
| `cloud.instance` | Odoo instance (production/staging/development) |
| `cloud.backup.backend` | S3 backup backend (passphrase and secret are EncryptedChar) |
| `cloud.job` | Async SSH job (uses `queue_job`) |
| `cloud.job.type` | Job type registry (deploy, rebuild, probe, backup, etc.) |
| `cloud.job.log.chunk` | SSH output line (stdout/stderr/system) |
| `cloud.alert` | Actionable alerts (addon conflicts, pip conflicts, health) |

Executor pattern:

- `AbstractSSHExecutor` — async SSH via asyncssh, log flushing, bus
  notifications (debug level), `stop_on_failure` support.
- Commands are `(label, shell_cmd)` or `(label, shell_cmd, {"stop_on_failure": True})`.
- `DeployInstanceExecutor` — copier copy + init + click-odoo-update checksums.
- `RebuildInstanceExecutor` — copier update + safe boot test + click-odoo-update.

Requirements
------------

- Odoo 18.0
- `queue_job` (OCA)
- Python: `asyncssh`, `cryptography`, `PyJWT`, `PyYAML`, `boto3`
- Docker image: `ghcr.io/tecnativa/doodba` (includes `click-odoo-contrib`)

Extending with Custom Actions
-----------------------------

You can add custom instance actions to the UI from any Odoo module that
depends on `incubacloud`, without modifying this module's frontend.

**Step 1 — Create the executor** (Python):

.. code-block:: python

    # my_module/models/my_executor.py
    from odoo.addons.incubacloud.models.abstract_executor import AbstractSSHExecutor

    class MyCustomExecutor(AbstractSSHExecutor):
        _job_type = "my_custom_action"

        def get_commands(self):
            inst = self.job.instance_id
            d = self._inst_dir(inst)
            return [
                ("Run my script", f"cd {d} && ./my_script.sh"),
            ]

**Step 2 — Declare the job type** (XML):

.. code-block:: xml

    <!-- my_module/data/job_type.xml -->
    <record id="my_custom_action" model="cloud.job.type">
        <field name="name">My Custom Action</field>
        <field name="code">my_custom_action</field>
        <field name="apply_to">instance</field>
        <field name="show_as_action" eval="True"/>
        <field name="action_icon">fa-magic</field>
        <field name="action_order">50</field>
        <field name="description">Does something cool on the instance.</field>
    </record>

That's it. The button appears automatically in the instance detail actions bar
for every deployed instance. No frontend changes required.

**Available fields on** ``cloud.job.type``:

- ``show_as_action`` (Boolean, default ``False``) — set to ``True`` to render
  a button in the instance actions bar.
- ``action_icon`` (Char, default ``fa-cog``) — FontAwesome class for the
  button icon (e.g. ``fa-magic``, ``fa-database``, ``fa-refresh``).
- ``action_order`` (Integer, default ``100``) — lower values appear first
  in the custom actions group.
- ``priority_tier`` (Selection: ``high``/``normal``/``low``, default
  ``normal``) — controls the queue_job channel and priority for jobs of
  this type. See **Queue Job Configuration** below.

**Permissions:** custom action buttons are only shown to users with the
``can_deploy`` permission (Developer role or higher). Server-side permission
checks still apply when the job is enqueued.

Queue Job Configuration
-----------------------

IncubaCloud routes jobs through three priority tiers so that a flood of
background jobs (health checks, metrics, docker prune) never blocks a
user-initiated deploy or rebuild on a production instance.

**Tiers**:

+---------+-----------------+--------------+------------------------------------+
| Tier    | Channel         | Priority     | Used for                           |
+=========+=================+==============+====================================+
| HIGH    | ``root.user``   | 5            | User jobs on production instance   |
+---------+-----------------+--------------+------------------------------------+
| NORMAL  | ``root.user``   | 10           | User jobs on staging / host jobs   |
+---------+-----------------+--------------+------------------------------------+
| LOW     | ``root.bg``     | 10           | Automated: health, metrics, prune  |
+---------+-----------------+--------------+------------------------------------+

Jobs with ``priority_tier = 'normal'`` are **auto-promoted to HIGH** at
enqueue time when their target ``cloud.instance`` has
``environment = 'production'``. Host-only jobs stay at their declared tier.

**Required Odoo configuration** (the Odoo instance where ``incubacloud`` is
installed — add to your ``odoo.conf`` under ``/etc/odoo/`` or via
``conf.d/``):

.. code-block:: ini

    [options]
    workers = 3
    server_wide_modules = web,queue_job

    [queue_job]
    channels = root:3,root.user:2,root.bg:1

This gives you up to 3 concurrent SSH jobs, with **hard isolation** between
user-initiated work (capacity 2) and background jobs (capacity 1). Within
``root.user``, production jobs jump ahead of staging by priority.

**Tuning the tier of a specific job type:** go to Settings → Technical →
Cloud Job Types (admin only) and change the ``Priority Tier`` field.
Background/automated jobs shipped out-of-the-box (``host_metrics``,
``instance_health``, ``docker_prune``) are already set to ``low``.

**Instances deployed by IncubaCloud** keep a minimal ``odoo.conf``
default that does **not** assume queue_job is in use — an instance you
deploy just to run a plain Odoo app has no need for cloud.job's channels.
If your deployed instance **does** run queue_job, add the ``[queue_job]``
block shown above to its ``Odoo Conf`` field from the instance form and
redeploy/rebuild.

License
-------

Elastic License 2.0
