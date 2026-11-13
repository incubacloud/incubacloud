# RB-35: Move the central to another server by hand, with its history

**Severity:** planned.
**Typical trigger:** the central's server is to be freed, or the central needs a
bigger one, and the history must come along.
**Who runs it:** platform manager, in one session, at a quiet hour.

*Start an empty central* moves the panel to a new central and leaves the history
behind. This procedure copies the history first. It needs the single path (**One
path, without a CDN** on, the central under its public name): the name moves with the
DNS record, and the destination has a valid certificate before the record changes.

Throughout: `ORIGIN` is the central's server today, `DEST` the new one, `NAME` the
central's public name and `PORT` its platform port.

## Before

1. `DEST` is a host of the panel, prepared with Full Setup, with **the same direct
   port** as `ORIGIN` (the URLs carry it), and its list applied (*Sync Direct Path*).
   From `ORIGIN`: `curl -sk https://DEST_ADDRESS:PORT/` answers 403 or 421 (the port is
   open, nothing routed yet).
2. If the panel itself runs on `ORIGIN`, add `ORIGIN`'s address to **Who may delete
   series and read the fleet on the central** (`/32`).

## Steps

1. **Start an empty central on `DEST`** (Settings › Monitoring › *Where the central
   runs*). It deploys the stack there with the same credentials and a certificate for
   `NAME`, and records `DEST` as the central. `NAME` still points at `ORIGIN`, which
   goes on receiving and serving: the panel says the DNS is pending and keeps its URLs.
   Until the record changes, an account the panel adds is written on `DEST` only, so do
   the rest of this procedure in one go.
2. Stop the stores on both, `DEST` first; vmauth stays and answers 502, the agents
   queue:
   ```bash
   cd ~/observability-central && docker compose -p incubacloud-central stop victoriametrics loki
   ```
3. Copy the two volumes, `ORIGIN` to `DEST`, from the operator's machine, with a
   manifest and its SHA-256 on both sides:
   ```bash
   for v in incubacloud-central_vmdata incubacloud-central_lokidata; do
     ssh ORIGIN "docker run --rm -v $v:/v:ro alpine tar -C /v -cf - ." \
       | tee >(sha256sum > $v.origin.sha256) \
       | ssh DEST "docker run --rm -i -v $v:/v alpine sh -c 'rm -rf /v/* && tar -C /v -xf -'"
     ssh DEST "docker run --rm -v $v:/v:ro alpine tar -C /v -cf - ." | sha256sum > $v.dest.sha256
   done
   diff incubacloud-central_vmdata.origin.sha256 incubacloud-central_vmdata.dest.sha256
   ```
   If the archives differ by order, compare a file list with sizes instead
   (`find /v -type f -printf '%P %s\n' | sort`).
4. On `DEST`: `docker compose -p incubacloud-central up -d`, then read through the name
   at `DEST`'s address; the answer is 200 with series older than the cut:
   ```bash
   curl -sS --cacert ~/observability/central-ca.pem --resolve NAME:PORT:DEST_ADDRESS \
     -u operator:… "https://NAME:PORT/admin-r/api/v1/query?query=count(up)"
   ```
5. **Point `NAME`'s A record at `DEST`.** The panel's next check (or **Check now**)
   closes the pending alert, and the queues empty into `DEST`.
6. **72 hours later**, on `ORIGIN` and by hand: check that the panel's central is
   `DEST` and that `DEST` answers, then
   `docker compose -p incubacloud-central down -v` and `rm -rf ~/observability-central`.
   Nothing guards this step but the check: do it once.

## Rollback

- **Before step 5:** start the stores on `ORIGIN` again (`docker compose start`) and
  *Start an empty central* on `ORIGIN`: the stack comes up on the volumes it has, with
  the history.
- **After step 5, within the 72 hours:** point the record back at `ORIGIN` and do the
  same. What was written on `DEST` since the change is lost.
