# Third-party notices

IncubaCloud Core is built on, and works with, software made by others. This
file lists that software, its licence and how IncubaCloud uses it. Each
component keeps its own licence; the [Elastic License 2.0](LICENSE) covers
only IncubaCloud's own code (see [Licensing](README.md#licensing)).

"Unmodified" means IncubaCloud runs the component exactly as its authors
publish it: the image, package or file is fetched from the project's own
registry or repository, and none of its code is changed.

## Platform and Odoo modules

| Component | Author | Licence | How IncubaCloud uses it | Project |
|---|---|---|---|---|
| Odoo Community 18.0 | Odoo S.A. | [LGPL-3.0](https://www.gnu.org/licenses/lgpl-3.0.html) | The framework the panel runs on. IncubaCloud's modules are Odoo addons that depend on and extend it; Odoo is not included in this repository. | [odoo/odoo](https://github.com/odoo/odoo) |
| queue_job | Odoo Community Association (OCA) | [LGPL-3.0](https://www.gnu.org/licenses/lgpl-3.0.html) | Required addon for background jobs, installed unmodified from OCA's repository. | [OCA/queue](https://github.com/OCA/queue) |
| Doodba | Tecnativa | [Apache-2.0](https://www.apache.org/licenses/LICENSE-2.0) | Base image of the panel's Odoo image and of every instance it deploys, pulled unmodified from `ghcr.io/tecnativa/doodba`. | [Tecnativa/doodba](https://github.com/Tecnativa/doodba) |
| doodba-copier-template | Tecnativa | [BSL-1.0](https://www.boost.org/LICENSE_1_0.txt) | Template each instance's project is generated from: Copier fetches it from GitHub on the host at deploy time, at the version set in the panel's settings. | [Tecnativa/doodba-copier-template](https://github.com/Tecnativa/doodba-copier-template) |

## Files included in this repository

| Component | Author | Licence | How IncubaCloud uses it | Project |
|---|---|---|---|---|
| xterm.js 5.3.0 and xterm-addon-fit 0.8.0 | The xterm.js authors | [MIT](https://opensource.org/license/mit) | Browser terminal. Unmodified copy in `incubacloud/static/lib/xterm/`, with its `LICENSE`. | [xtermjs/xterm.js](https://github.com/xtermjs/xterm.js) |
| Space Grotesk | The Space Grotesk Project Authors | [OFL-1.1](https://openfontlicense.org/open-font-license-official-text/) | Display typeface. Font files in `incubacloud/static/fonts/` and `docs/user/docs/assets/fonts/`, with `OFL-space-grotesk.txt`. | [floriankarsten/space-grotesk](https://github.com/floriankarsten/space-grotesk) |
| Hanken Grotesk | The Hanken Grotesk Project Authors | [OFL-1.1](https://openfontlicense.org/open-font-license-official-text/) | Text typeface. Font files in `incubacloud/static/fonts/`, with `OFL-hanken-grotesk.txt`. | [marcologous/hanken-grotesk](https://github.com/marcologous/hanken-grotesk) |
| JetBrains Mono | The JetBrains Mono Project Authors | [OFL-1.1](https://openfontlicense.org/open-font-license-official-text/) | Monospaced typeface. Font files in `incubacloud/static/fonts/`, with `OFL-jetbrains-mono.txt`. | [JetBrains/JetBrainsMono](https://github.com/JetBrains/JetBrainsMono) |

## Python packages and Ansible collections

Installed by pip or `ansible-galaxy` from their public indexes, at the versions
pinned in `requirements.txt` and `incubacloud/ansible/requirements.yml`. None
is included in this repository.

| Component | Author | Licence | How IncubaCloud uses it | Project |
|---|---|---|---|---|
| ansible-core | Red Hat, Inc. and the Ansible contributors | [GPL-3.0-or-later](https://www.gnu.org/licenses/gpl-3.0.html) | Runs the playbooks in `incubacloud/ansible/` that set up and maintain hosts, as a separate program. | [ansible/ansible](https://github.com/ansible/ansible) |
| ansible-runner | Red Hat, Inc. | [Apache-2.0](https://www.apache.org/licenses/LICENSE-2.0) | Starts those playbooks and streams their output into the job log. | [ansible/ansible-runner](https://github.com/ansible/ansible-runner) |
| ansible.posix, community.general | The Ansible community | [GPL-3.0-or-later](https://www.gnu.org/licenses/gpl-3.0.html) | Ansible modules the playbooks call. | [ansible.posix](https://github.com/ansible-collections/ansible.posix), [community.general](https://github.com/ansible-collections/community.general) |
| AsyncSSH | Ron Frederick | [EPL-2.0](https://www.eclipse.org/legal/epl-2.0/) | SSH connections to the hosts. | [ronf/asyncssh](https://github.com/ronf/asyncssh) |
| boto3 | Amazon Web Services | [Apache-2.0](https://www.apache.org/licenses/LICENSE-2.0) | S3-compatible storage for backups. | [boto/boto3](https://github.com/boto/boto3) |
| cryptography | The Python Cryptographic Authority | [Apache-2.0 OR BSD-3-Clause](https://github.com/pyca/cryptography/blob/main/LICENSE) | Encryption of stored secrets, and keys and signatures (GitHub App, SSH). | [pyca/cryptography](https://github.com/pyca/cryptography) |
| pyOpenSSL | The Python Cryptographic Authority | [Apache-2.0](https://www.apache.org/licenses/LICENSE-2.0) | Kept at a release that works with cryptography. | [pyca/pyopenssl](https://github.com/pyca/pyopenssl) |
| bcrypt | The Python Cryptographic Authority | [Apache-2.0](https://www.apache.org/licenses/LICENSE-2.0) | Password hash of the Traefik dashboard on each host. | [pyca/bcrypt](https://github.com/pyca/bcrypt) |
| PyYAML | Ingy döt Net, Kirill Simonov | [MIT](https://opensource.org/license/mit) | Reading and writing YAML configuration. | [yaml/pyyaml](https://github.com/yaml/pyyaml) |
| joserfc | Hsiaoming Yang | [BSD-3-Clause](https://opensource.org/license/bsd-3-clause) | JWS and JWT validation of agents' OAuth tokens. | [authlib/joserfc](https://github.com/authlib/joserfc) |

## Software the panel installs on hosts

When the panel sets up a host, it pulls these images unmodified from their
publishers' registries, or downloads the published release, on that host.
IncubaCloud does not redistribute them.

| Component | Author | Licence | How IncubaCloud uses it | Project |
|---|---|---|---|---|
| Traefik v2.11 | Traefik Labs | [MIT](https://opensource.org/license/mit) | Reverse proxy and HTTPS certificates for each host. | [traefik/traefik](https://github.com/traefik/traefik) |
| docker-socket-proxy | Tecnativa | [Apache-2.0](https://www.apache.org/licenses/LICENSE-2.0) | Read-only access to the Docker API for Traefik. | [Tecnativa/docker-socket-proxy](https://github.com/Tecnativa/docker-socket-proxy) |
| docker-whitelist | Tecnativa | [Apache-2.0](https://www.apache.org/licenses/LICENSE-2.0) | Outbound proxies that allow only listed destinations. | [Tecnativa/docker-whitelist](https://github.com/Tecnativa/docker-whitelist) |
| egress-gatekeeper | Tecnativa | [Apache-2.0](https://www.apache.org/licenses/LICENSE-2.0) | Closes the outbound traffic of staging instances. | [Tecnativa/egress-gatekeeper](https://github.com/Tecnativa/egress-gatekeeper) |
| postgres-autoconf | Tecnativa | [Apache-2.0](https://www.apache.org/licenses/LICENSE-2.0) | PostgreSQL image of each instance. | [Tecnativa/docker-postgres-autoconf](https://github.com/Tecnativa/docker-postgres-autoconf) |
| PostgreSQL | The PostgreSQL Global Development Group | [PostgreSQL](https://www.postgresql.org/about/licence/) | Database of each instance, inside the image above. | [postgresql.org](https://www.postgresql.org/) |
| docker-duplicity | Tecnativa | [Apache-2.0](https://www.apache.org/licenses/LICENSE-2.0) | Scheduled backups of instances to S3-compatible storage. | [Tecnativa/docker-duplicity](https://github.com/Tecnativa/docker-duplicity) |
| WAL-G v3.0.9 | The WAL-G authors | [Apache-2.0](https://www.apache.org/licenses/LICENSE-2.0) | Continuous archiving for point-in-time restore. The published binary is downloaded on the host and checked against its SHA-256. | [wal-g/wal-g](https://github.com/wal-g/wal-g) |
| rclone 1.71.1 | Nick Craig-Wood and the rclone contributors | [MIT](https://opensource.org/license/mit) | Copies point-in-time archives to storage. | [rclone/rclone](https://github.com/rclone/rclone) |
| Alpine Linux image | Alpine Linux | [MIT](https://github.com/alpinelinux/docker-alpine/blob/master/LICENSE) | Short-lived helper containers for file operations on the host. | [alpinelinux/docker-alpine](https://github.com/alpinelinux/docker-alpine) |
| Grafana 11.2.0 | Grafana Labs | [AGPL-3.0-only](https://www.gnu.org/licenses/agpl-3.0.html) | Monitoring dashboards, run as its own container. | [grafana/grafana](https://github.com/grafana/grafana) |
| Loki 2.9.8 | Grafana Labs | [AGPL-3.0-only](https://www.gnu.org/licenses/agpl-3.0.html) | Log storage, run as its own container. | [grafana/loki](https://github.com/grafana/loki) |
| Promtail 2.9.8 | Grafana Labs | [Apache-2.0](https://github.com/grafana/loki/blob/main/LICENSING.md) | Ships host and container logs to Loki. | [grafana/loki](https://github.com/grafana/loki) |
| VictoriaMetrics, vmagent and vmauth v1.102.0 | VictoriaMetrics | [Apache-2.0](https://www.apache.org/licenses/LICENSE-2.0) | Metrics storage, collection and access control. | [VictoriaMetrics/VictoriaMetrics](https://github.com/VictoriaMetrics/VictoriaMetrics) |
| node_exporter v1.8.2 | The Prometheus authors | [Apache-2.0](https://www.apache.org/licenses/LICENSE-2.0) | Host metrics. | [prometheus/node_exporter](https://github.com/prometheus/node_exporter) |
| cAdvisor v0.55.1 | Google | [Apache-2.0](https://www.apache.org/licenses/LICENSE-2.0) | Container metrics. | [google/cadvisor](https://github.com/google/cadvisor) |

## Documentation site

| Component | Author | Licence | How IncubaCloud uses it | Project |
|---|---|---|---|---|
| Material for MkDocs | Martin Donath | [MIT](https://opensource.org/license/mit) | Builds the user documentation in `docs/user/`. | [squidfunk/mkdocs-material](https://github.com/squidfunk/mkdocs-material) |

## Trademarks

Odoo is a trademark of Odoo S.A. IncubaCloud is not affiliated with Odoo S.A.

Doodba is a name of Tecnativa. IncubaCloud uses it only to describe what it is
based on: the instances it deploys are Doodba projects.

Other names are the trademarks of their respective owners and are used here
only to identify their software.
