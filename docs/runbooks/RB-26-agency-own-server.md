# RB-26: Agency: its own server

**Severity:** planned
**Typical trigger:** a tenant signs up for the Agency plan or changes to it; an
Agency tenant goes back to another plan or is deleted.
**Who runs it:** platform manager, on the master panel.

The Agency plan includes its own server for the tenant's panel: a server that
runs that panel and nothing else. It is promised on the website and in the
terms as ready within 1 business day of the subscription or of the change to
Agency. Nothing automates it yet: the operator orders the server, sets it up and
moves the panel onto it by hand. Business has no server of its own: it is Pro
with more quota and lives on the paid pool like the other paid plans.

The server is a Hetzner `cx43`, which is shared CPU. Do not describe it as
dedicated CPU anywhere.

> **Moves are only safe once the rollback fix of 28-oct-2026 is deployed.** A
> move that fails halfway is undone with *Roll back move*, and until that fix is
> running on the master the rollback cannot be relied on. Check that the master
> runs it before moving a customer's panel.

The labels below are as the master panel shows them in English, with the Spanish
of `es.po` after the slash.

## Give the Agency tenant its server

1. **Order the server.** `Hosts` → **Order VPS** (Pedir VPS). In the modal
   (**Order new VPS** / Pedir nuevo VPS) fill:

   | Field | Value |
   |---|---|
   | **Host name** / Nombre del host | one that says whose it is, e.g. `agency-<subdomain>` |
   | **Provider** / Proveedor | the active provider (Hetzner) |
   | **Location** / Ubicación | the Free pool's location policy, EU only: the one set under `Host Pools` → Free → Location, or another EU location if it is not available |
   | **VPS size** / Tamaño VPS | `cx43` |

   Click **Order VPS**. The modal only contracts the server; hardening and setup
   are not chained.
2. **Keep it away from other tenants.** Open the new host, `General` tab:
   - **Host Pool** / Pool de hosts: *Paid*.
   - **Exclude from auto-assignment** / Excluir de auto-asignación: checked
     (under **Auto-assignment** / Auto-asignación). While checked, the placement
     of new tenants, the warm pool and the pool's capacity count skip this
     host, so nothing else lands on it and the auto-provisioner does not read
     it as free room.
   - **Warm Pool** → **Target** / Objetivo (the *Warm Pool Target* field,
     Objetivo del pool warm): `0`.
   Save the host.
3. **Set it up.** Click **Apply Hardening** / Aplicar hardening and, in the
   modal, **Hardening + Full Setup** / Hardening + configuración completa (the
   recommended order). If the modal offers only **Apply Hardening**, apply it
   and then click **Full Setup** / Configuración completa in the action bar.
   Wait until the hardening job and the Full Setup job are both `done` in the
   host's job history, and the host shows **Compatible** with no *Not hardened*
   or *Changes not applied* pill.
4. **Move the panel onto it.** Open the tenant's panel instance (the instance of
   the tenant's panel on the master), click **Move to host** / Mover a host,
   pick the new host under **Move to:** / Mover a: (only compatible hosts with
   Traefik deployed are listed) and click **Move** / Mover; confirm the dialog
   (*Move instance* / Mover instancia). The panel is unavailable for a few
   minutes while its data is copied, and comes back by itself.
5. **Check.** The move job ends `done`, the instance shows the new host, and the
   tenant's panel answers at `https://<subdomain>.<base domain>` (sign in, or at
   least a `200` on the login page). The whole sequence has to fit in the
   promised 1 business day.

If the move fails or leaves the instance marked as moving, use **Roll back move**
/ Revertir traslado on the same instance; it brings the panel back up on its
previous host. The new host stays and can be tried again.

## Release it

When the tenant goes back to another plan, or is deleted:

1. **Downgrade.** On the tenant's panel instance click **Move to host**, pick a
   host of the paid pool, click **Move**, and check the panel answers, as above.
   **Deletion.** Let the deletion run: it removes the instance from the host.
2. Wait until the host has no instances left (`Hosts` → the host shows `0
   instances`).
3. Open the host and click the trash button (**Delete** / Eliminar, *Delete
   host* / Eliminar host), then confirm **Delete** in the dialog. A host with
   instances refuses it (*Host still has instances. Delete all instances
   first.*). A host that was set up is torn down by a `delete_host` job and
   then archived; archiving destroys the VM at the provider and releases its
   firewall, so the hourly cost stops.
4. Check on the provider's console that the server is gone.

## Rollback

- Setup fails: fix the cause from the host's job history and run **Full
  Setup** again. Nothing has moved yet.
- A move fails: **Roll back move**, as above.
- Ordered the wrong size or place: delete the host (step 3 of *Release it*; it
  has no instances) and order it again.
