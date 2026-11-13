# RB-42 — A rebuild left its work unapplied on the server

**Severity:** warning · **When:** a Start or Restart ends with «started the
instance's previous build», a Start fails with «did not start anything», a
restore is refused with «Refused: rebuild #N left rewritten files and an
image…», or a move is postponed with `unapplied`.

## What the mark is

A rebuild rewrites the instance's files on its server (`copier update`:
the compose files, the Odoo conf, `repos.yaml`, the override…) and
rebuilds its image before it tries that image on a copy of the database,
updates the modules and restarts the instance. A rebuild that ends in
between (stopped, failed in its build or its boot test, its worker gone)
leaves the instance running its previous build, over files and an image
nothing has tried.

So `rebuild.sh copier-update` writes `.incubacloud-unapplied` in the
instance's directory right before copier, and the rebuild's restart
removes it once it worked. The file holds three lines:

```text
job=<the rebuild's job id>
kind=build
at=<UTC>
```

`kind=files` comes from a job that rewrites files and builds no image (a
layer's plan change): it holds nothing back. A `build` mark:

| What | With the mark |
|---|---|
| Start, Restart | Start the containers the instance has, the previous build, without recreating them, and end with a warning. When those containers are not the services the rewritten compose declares (a service added or dropped), nothing is started and the job fails, leaving the instance's status as it was. |
| Restore (backup, upload, link, another instance, point in time) | Refused before anything runs. |
| Move | Postponed before the source stops (`unapplied`), without an alert. |
| An `odoo shell` a layer runs in the image | Refused (exit 26). |
| Stop, delete | As always. |

## The way out: rebuild

Rebuild the instance. A rebuild that passes applies what the previous one
left and removes the mark. When the rebuild fails for a reason of the
code (a branch that does not build, a module that does not update), fix
the code and rebuild again: until then the instance runs its previous
build, and restoring or moving it waits.

## By hand, when a rebuild cannot pass

Removing the mark gives back the behaviour from before it existed: the
next `docker compose up` (a Start, a restore, a move) recreates the
containers from the rewritten files and the image the server tags now,
untried. Do it only when that is what you want, for instance to restore a
backup over an instance whose code you are going to rebuild anyway.

1. Find the instance's directory on its server: the project's folder and
   the instance's name, under the SSH user's home (`~/<project>/<instance>`).
2. Open a shell on the server as that user, read the mark, and remove it:

   ```bash
   cat ~/<project>/<instance>/.incubacloud-unapplied
   bash <core checkout>/incubacloud/scripts/rebuild.sh clear-unapplied ~/<project>/<instance>
   ```

   Without a copy of the scripts on the server, removing the file is the
   same:

   ```bash
   rm ~/<project>/<instance>/.incubacloud-unapplied
   ```

3. Start, restore or move the instance again.

A layer that holds a move postponed for `unapplied` until a rebuild passes
is not told that the mark was removed by hand: its move keeps waiting for
a rebuild, which it queues again each time the move's window passes, and
the operator is alerted then. Rebuild the instance once it can pass.

## Rollback

Nothing to roll back: the mark is a marker file, not part of the stack. The
next rebuild writes it again before its copier, and removes it after its
restart.
