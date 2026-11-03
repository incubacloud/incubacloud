# Third-party software

IncubaCloud is built on software made by others, and each piece keeps its own
licence. The full list, with each licence and how IncubaCloud uses it, is in
[THIRD_PARTY_NOTICES.md](https://github.com/incubacloud/incubacloud/blob/19.0/THIRD_PARTY_NOTICES.md)
in the core repository.

## In short

- **Odoo Community** (LGPL-3.0, Odoo S.A.) is the framework the panel runs on.
  The panel uses no Odoo Enterprise module.
- **Doodba** (Apache-2.0, Tecnativa) is the base image of the panel and of every
  instance it deploys, and **doodba-copier-template** (BSL-1.0, Tecnativa) is the
  template each instance's project is generated from.
- **queue_job** (LGPL-3.0, Odoo Community Association) runs the panel's
  background jobs.
- On your hosts, the panel installs **Traefik**, **PostgreSQL**, **WAL-G**,
  **rclone**, **Grafana**, **Loki**, **VictoriaMetrics** and other tools, as
  their authors publish them.

IncubaCloud's own code is licensed separately: see
[Licensing](https://github.com/incubacloud/incubacloud/blob/19.0/README.md#licensing).

## Odoo Enterprise

IncubaCloud deploys Odoo Community and Odoo Enterprise, but it does not provide
Odoo Enterprise or a licence for it. To deploy Enterprise, you bring its code
in your own repositories and you need your own **Odoo Enterprise subscription
("On-premise")** from Odoo S.A.

## Trademarks

Odoo is a trademark of Odoo S.A. IncubaCloud is not affiliated with Odoo S.A.
Doodba is a name of Tecnativa, used here only to describe what IncubaCloud is
based on.
