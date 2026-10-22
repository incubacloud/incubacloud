# RB-15 — Bump the doodba template pin

**Severity:** planned maintenance · **When:** you decide to adopt a
newer `doodba-copier-template` release for tenant deploys.

Every instance deploy runs `copier copy` against the template pinned by
**Settings → Doodba template version** (`copier_template_ref`,
currently `v10.0.8`). Pinning means upstream improvements arrive when
*you* run this runbook — never as a surprise inside a tenant deploy.

> **Also re-check the odoo service's `command`.** The compose override
> replaces it to add `--logfile`, so it repeats whatever the template
> puts there: none in `prod.yaml` (the image's CMD) and
> `--workers=3 --max-cron-threads=1` in `test.yaml`. If the candidate
> tag changes either, update `ODOO_COMMAND_PROD` / `ODOO_COMMAND_TEST`
> in `deploy_instance_executor.py` — otherwise staging silently keeps
> the old worker settings.

## 1. Pick the candidate tag

```bash
git ls-remote --tags https://github.com/Tecnativa/doodba-copier-template \
    | tail -5
```

Read the release notes between the current pin and the candidate.
Anything touching `prod.yaml`, Traefik labels, the backup service or
the answers schema deserves extra attention in step 2.

## 2. Sandbox render with the real answers

Render the candidate locally with the exact answers shape the deploy
executor emits (`deploy_instance_executor._build_answers`). A minimal
data file needs: project fields, `odoo_version`, postgres block,
`domains_prod` (one plain entry and one redirect), the smtp block and
the backup block.

```bash
copier copy --defaults --overwrite --trust \
    --vcs-ref vX.Y.Z \
    --data-file answers.yml \
    gh:Tecnativa/doodba-copier-template /tmp/render-check
```

Then verify, at minimum:

- exit code 0 and `_commit: vX.Y.Z` in `/tmp/render-check/.copier-answers.yml`;
- every **non-secret** answers key is recorded in
  `.copier-answers.yml` (a key that disappears means the question was
  dropped upstream — the executor must be adapted first);
- domains and redirects appear in the Traefik labels of `prod.yaml`;
- the backup service carries the rendered `DST`.
- the Odoo routers still follow the scheme the login routers copy
  (`DeployInstanceExecutor._login_router_labels`): names
  `<project>-<version>-<prod|test>-<suffix>-<index>`, a `-secure` twin
  with the TLS labels, service `<key>-main`, the `/web` router at
  priority 10 with the `buffering`, `compress` and `forbid-crawlers`
  middlewares (plus `addSTS`/`forceSecure` when TLS is on, `whitelist`
  with a CIDR list). If the template changes any of it, adapt the login
  routers in the same release: a mismatch does not break the site —
  Traefik drops the login router and the login falls back to the
  template's own — but the login limit is lost until then.

## 3. Canary on staging

Set the new tag in **Settings → Doodba template version** on the panel,
then deploy (or rebuild) **one staging instance** and check it boots,
serves TLS and its backup service starts. The effective ref is echoed
in the job log (`running copier into … @ vX.Y.Z`).

## 4. Roll out / roll back

- **Keep it:** nothing else to do — every next deploy uses the new pin.
- **Problems:** set the previous tag back in Settings. Instances built
  with the bad tag can be rebuilt after the revert; `copier-deploy`
  always renders from scratch with `--overwrite`.

Rebuilding an existing healthy instance is **not** required after a
bump: the pin only affects the next `copier copy` run for each
instance.

## What past bumps taught

**v9.6.1 → v10.0.8 (core 1.0.195, 2026-10).** The template changed the
model of a staging's egress: one `gatekeeper` service that owns odoo's
network namespace, filters with nftables and carries the Traefik
labels, instead of `proxy_general` + `odoo_net_setup`. Three things
the sandbox render does not show and this bump had to add:

- the template gives the gatekeeper **no restart policy**; odoo cannot
  start inside the namespace of a container that is not running, so
  without one a host reboot left the staging down (the override adds
  it, `_gatekeeper_override`);
- the image has **no versions**, only `latest` and pull-request tags:
  pinned by digest (`GATEKEEPER_IMAGE`); moving it is a decision like
  this runbook's;
- the shared whitelist network is gone, so **an empty allowlist means
  no egress** rather than the host's proxies; the panel renders the
  host's list in that case.

A stack keeps the model it was rendered with until its next rebuild
(`stack_template_ref`), so the probe and the deploy understand both;
the host's `globalwhitelist` proxies stay until no old-model staging
is left. Production's `prod.yaml` was identical in both versions.

The canary in production (2026-10-22) found what QA had not tried:
the **first rebuild of a staging rendered with the old template**. The
safe boot test runs before the stack is restarted, so it met the new
files with the old containers and no gatekeeper; it now starts the
gatekeeper alone for the test (and removes it if the test fails). Its
boot container also runs with `--no-deps`: otherwise Compose recreates
any dependency whose definition the new template changed — the live
`db`, in the middle of the test. While that gatekeeper runs beside the
old `odoo` until the restart, Traefik sees both containers' routers, so
the staging may answer 502 for those few minutes. In general, a bump
must be canaried on a stack **rendered with the previous pin**, not
only on a fresh deploy.
