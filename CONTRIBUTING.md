# Contributing to IncubaCloud Core

Thanks for your interest in contributing. Please read this document before opening a pull request.

---

## Before you start

- For **bug reports**, open a GitHub issue with a clear reproduction case.
- For **new features or significant changes**, open an issue first to discuss the approach. This saves both sides from wasted work.
- For **security vulnerabilities**, do **not** open a public issue. See [SECURITY.md](SECURITY.md).

---

## Development setup

### Requirements

- Docker + Docker Compose
- Python 3.12
- [invoke](https://www.pyinvoke.org/)

### Clone and run

```bash
git clone https://github.com/incubacloud/core /path/to/addons/core
```

Place the cloned folder inside an Odoo `addons_path`. For a Doodba-based setup:

```bash
# From the doodba project root:
invoke install -m incubacloud
invoke restart
```

For a standard Odoo setup, add the path to `odoo.conf` and install via CLI or UI.

### Python dependencies

```bash
pip install asyncssh cryptography boto3 refurb
```

---

## Coding standards

### Python

- Target **Python 3.12** and **Odoo 19.0** APIs.
- Follow [refurb](https://github.com/dosisod/refurb) idioms:
  - `str.removeprefix()` / `str.removesuffix()` instead of conditional slicing
  - `with suppress(ExcType):` instead of `try/except: pass`
  - `Path(x)` instead of `os.path.*` / `open(x)`
  - `a | b` instead of `{**a, **b}` for dict merges
  - Tuples for membership tests: `x in (a, b)` not `x in [a, b]`
- Run `refurb incubacloud/ incubacloud_connect/` before opening a PR. The CI will fail if there are warnings.

### JavaScript / OWL

- Use the existing **OWL patterns** in the codebase (hooks, `useState`, `onWillStart`).
- For confirmation dialogs, use the **project's own `_confirm()` pattern** — never Odoo's `ConfirmationDialog`. See the [architecture docs](docs/architecture.md#frontend-spa) for the pattern.
- Do not add new external JS dependencies.

### Odoo-specific

- New SSH job types require:
  1. A new `AbstractSSHExecutor` subclass with `_job_type` set.
  2. A matching `cloud.job.type` record in `data/job_type.xml`.
  3. Tests covering `get_commands()`, `parse_results()`, and the `on_success()` / `on_failure()` paths.
- All DB writes inside an executor must use **fresh cursors** (see [architecture docs](docs/architecture.md#transaction-model)).
- Bus notifications must be sent from the **main cursor** only, never from postcommit hooks.

---

## Running tests

```bash
# All tests
odoo --test-enable --stop-after-init --workers=0 \
     -u incubacloud \
     --test-tags /incubacloud

# Single test file
odoo --test-enable --stop-after-init --workers=0 \
     -u incubacloud \
     --test-tags /incubacloud:TestCloudJob
```

All new code must be covered by tests. We use three tiers:

| Tier | Base class | When to use |
|---|---|---|
| 1 | `unittest.TestCase` | Pure Python logic (no DB) |
| 2 | `TransactionCase` | ORM behaviour, model methods |
| 3 | `HttpCase` | Controllers, HTTP routes |

---

## Pull request process

1. **Fork** the repository and create a branch from `19.0`:
   ```bash
   git checkout -b feature/my-feature 19.0
   ```

2. **Write tests** for your changes. PRs without tests for new behaviour will not be merged.

3. **Run the linter** and fix any warnings:
   ```bash
   refurb incubacloud/ incubacloud_connect/
   ```

4. **Open the PR** against the `19.0` branch. Fill in the PR template.

5. A maintainer will review within a reasonable time. Please be patient.

---

## What we will not merge

- Code that breaks existing tests.
- Code with `refurb` warnings.
- New features without tests.
- Changes that tie the platform to a specific deployment tool (e.g. hardcoded Doodba assumptions in core).
- Changes to the license or copyright headers.

---

## License

By contributing, you agree that your contributions will be licensed under the [Elastic License 2.0](LICENSE).
