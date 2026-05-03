# IncubaCloud user documentation

Source for [docs.incubacloud.io](https://docs.incubacloud.io).
Built with [mkdocs-material](https://squidfunk.github.io/mkdocs-material/).

## Local preview

```bash
cd docs/user
pip install -r requirements.txt
mkdocs serve   # http://127.0.0.1:8000
```

To validate without serving:

```bash
mkdocs build --strict
```

`--strict` turns warnings into errors. CI uses the same flag — keep local builds clean
to avoid CI failures.

## Structure

```
docs/user/
├── mkdocs.yml          # Site config + nav
├── requirements.txt    # mkdocs-material pin (CI uses this exact version)
├── overrides/          # Theme overrides (templates, hooks)
└── docs/
    ├── CNAME           # Custom domain (docs.incubacloud.io) — copied to gh-pages
    ├── index.md        # Landing page
    ├── stylesheets/    # CSS overrides for IncubaCloud palette
    ├── getting-started/
    ├── projects/
    ├── instances/
    ├── hosts/
    ├── backups/
    ├── billing/
    ├── migrations/
    └── reference/      # Per-screen reference (work in progress)
```

## Deployment

CI: `.github/workflows/docs.yml`. Pushes to branch `19.0` that touch `docs/user/**`
build and deploy to the `gh-pages` branch. GitHub Pages serves it at
`https://docs.incubacloud.io`.

Pull requests build the site (with `--strict`) and upload it as an artifact for review,
but do not publish.

## Writing docs

- **Tone**: outcome-first. Tell the reader what they'll get done, not which library
  we use under the hood. Keep it second-person and direct.
- **Length**: most pages should fit in 500–800 words. Use admonitions
  (`!!! info`, `!!! warning`, `!!! note`, `??? note` for collapsible) liberally.
- **Code blocks**: tag the language so syntax highlighting kicks in.
- **Cross-links**: prefer relative paths (`../backups/restore.md`) over absolute URLs
  for maintainability.
- **Screenshots**: place in `docs/<section>/img/` (create the folder when needed).

## Sister docs

This site is for **users**. Other docs in the same repo target other audiences:

- `docs/architecture.md` — internal architecture (developers).
- `docs/runbooks/` — operations playbooks (on-call).
- `docs/api-endpoints.md` — internal API surface (developers).
- `incubacloud/README.md` — module installation (developers).
