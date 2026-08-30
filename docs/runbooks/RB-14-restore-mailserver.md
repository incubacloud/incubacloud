# RB-14 — Restore the mail server

**Severity:** critical · **When:** the host running
`mail.incubacloud.io` is lost or its mail data is corrupted.

The mail stack (docker-mailserver) lives outside the panel compose, in
`~/Mailserver` on its host, but its backup is a service of the
production compose: `backup_mailserver` ships `~/Mailserver` whole to
`…/backups/incubacloud/mailserver` in the backup bucket via duplicity
(daily incremental, weekly full, 3-month retention), encrypted with the
same custody-verified passphrase as the panel backup.

What that archive contains (per the docker-mailserver FAQ scope):

- `docker-data/dms/config/` — **accounts, aliases and DKIM private
  keys**. The DKIM keys are the reason this runbook exists: losing
  them breaks signing until keys are regenerated and DNS TXT records
  updated, which costs hours of deliverability at the worst moment.
- `docker-data/dms/mail-*` — the mailboxes and mail state.
- `compose.yaml` + `mailserver.env` — the stack definition.
- `docker-data/certbot/` — the dedicated TLS cert and the Cloudflare
  token used by its renewal cron (regenerable, but restoring is
  faster).

## 1. Get the keys

From custody (Enpass): the duplicity `PASSPHRASE` and the backup
bucket credentials — the same ones as `.docker/backup.env` of the
panel deployment.

## 2. Restore the tree

On the target host (new or repaired), with duplicity available (any
`tecnativa/docker-duplicity` container works):

```bash
docker run --rm -it \
  -e AWS_ACCESS_KEY_ID=… -e AWS_SECRET_ACCESS_KEY=… \
  -e AWS_ENDPOINT_URL=…  -e PASSPHRASE=… \
  -v /root/Mailserver:/mnt/backup/src \
  ghcr.io/tecnativa/docker-duplicity:latest \
  dup restore boto3+s3://incubacloud-platform-backups/backups/incubacloud/mailserver /mnt/backup/src
```

For a partial restore (e.g. only DKIM keys), add
`--file-to-restore docker-data/dms/config`.

## 3. Bring the stack up

```bash
cd /root/Mailserver && docker compose up -d
```

Re-create the weekly certbot renewal cron if the crontab was lost
(see the mailserver architecture notes; it is a one-line `docker run
certbot/dns-cloudflare renew` + `docker compose restart mailserver`).

## 4. DNS

If the host IP changed: update the `mail` A record in Cloudflare, and
verify MX, SPF, DKIM (`dig TXT mail._domainkey.incubacloud.io`) and
DMARC still match the restored keys.

## 5. Verify

- `docker logs mailserver` clean start, ports 25/465/587/993 listening.
- Send a test mail to an external mailbox; check it arrives
  DKIM-signed (`dkim=pass` in the received headers).
- Send an inbound test mail and read it over IMAP.

## Caveats

- The backup snapshots a **running** mail store; a message in flight
  during the daily run may need a second incremental to appear. For a
  planned migration, stop the mailserver before a manual
  `/etc/periodic/daily/jobrunner` run on `backup_mailserver` to get a
  perfectly consistent final increment.
- The first deploy of `backup_mailserver` on a host without
  `/root/Mailserver` will fail its bind mount — the service belongs
  only on the host that actually runs the mail stack.
