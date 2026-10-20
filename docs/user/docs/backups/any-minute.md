# Restore to any minute

A nightly backup takes you back to last night. **Continuous recovery**
takes a production instance back to **any minute** of its backup window:
the database's changes are archived as they happen and the files copied as
they change, so a mistake at 11:42 can be undone by going back to 11:41.

It lives in the instance's **Backups** tab, in the *Continuous recovery*
card above the list of backups. It needs a backup backend, like the
nightly backups, and uses the same bucket.

!!! info "IncubaCloud SaaS"
    On the hosted service it comes with the plan: switched on by the
    platform with the plan's objective. The card shows the state and lets
    you restore; the objective is the plan's.

## What the card shows

| Field | Meaning |
|---|---|
| **State** | `OK · Archiving`: everything is being archived. `Behind`: the archive is more than three objectives late; we are told, and it usually catches up on its own. `Failing`: nothing is being archived; we are told at once. `Switching on…` / `Off`. |
| **Restorable to** | The earliest minute you can go back to, and how many days are kept (the backup retention). The window grows from the moment it was switched on until it covers the retention. |
| **Last archived** | When the last piece of the database's log reached the bucket. |
| **Checked** | When the platform last asked the server. *Last archived* is as fresh as this: the check runs every ten minutes, so a reading a few minutes old is normal. |
| **Loses at most** | The recovery objective: how far behind the archive may be at worst, from 1 minute to 1 hour. Shorter means more frequent uploads. Changing it rebuilds the instance's stack, with a short pause. |

## Restore to a moment

1. Open the instance, go to **Backups** and click **Restore to a moment…**.
2. Pick the **moment**, in your local time. It must be inside the window
   the card shows; the dialog opens on the present.
3. Type the instance's name to confirm, then **Restore**.

What happens next, in order:

1. A backup of the present is taken first, so the state you are leaving
   can be restored too.
2. Odoo is stopped. Nothing of the instance runs meanwhile.
3. The database is brought back to the moment chosen and the files with
   it. Whatever happened after that moment is undone.
4. The instance is started again, and a fresh base copy is taken: the
   window now continues from the restored moment.

How long it takes depends on the size of the database, usually a few
minutes. The job's log shows every step; if the restored database does
not come up, the previous data is put back and the instance started
again, and the job says so.

!!! warning "A restore undoes everything after the moment"
    Orders, invoices, messages, anything written after the moment chosen
    is gone from the instance, though it is still in the backup taken
    first. If in doubt, go back less far: you can restore again, to a
    later moment, from the window the card shows.

## Switching it on or off

**Switch on** rebuilds the instance's stack and restarts its database
once; from then on it archives. The first base copy takes a few minutes,
and the card says *Switching on…* until it is done.

**Switch off** stops archiving and lets the archive expire with the backup
retention. The nightly backups go on as before.

## See also

- [Backups](index.md)
- [Restore a backup](restore.md)
