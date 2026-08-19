# RB-15 — Bump the doodba template pin

**Severity:** planned maintenance · **When:** you decide to adopt a
newer `doodba-copier-template` release for tenant deploys.

Every instance deploy runs `copier copy` against the template pinned by
**Settings → Doodba template version** (`copier_template_ref`,
currently `v9.6.1`). Pinning means upstream improvements arrive when
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
