# RB-27 — A notice of illegal content

**Severity:** high · **When:** someone writes to `support@incubacloud.io`
that content hosted on the platform is illegal, or an authority writes
to it as our point of contact.

Section 9A of the Terms of Service promises this procedure under the
Digital Services Act (Regulation (EU) 2022/2065, articles 11, 12, 14, 16,
17 and 18). `support@` is both the notice channel and the single point
of contact; there is no separate `abuse@`.

## 1. Acknowledge

Reply to the sender the same working day: the notice is received, with
its date, and it will be reviewed. Keep the thread; it is the record.

A notice is complete when it says why the content is illegal, where it
is (an instance's address, a page), and carries the sender's name and
email (not needed for child sexual abuse material) and a good-faith
statement. An incomplete one is still reviewed; ask for what is missing.

## 2. Find the tenant

From the address in the notice, find the instance and its tenant in
the master (the instances list, searched by domain). Note the tenant, its
owner and the exact content. Never open or copy data beyond what the
review needs.

## 3. Decide

A person decides, never a tool. Look at the content against the law
invoked and against section 9 (Acceptable use) of the Terms.

- **Not illegal, or not ours to judge:** tell the sender, with the
  reasons, and that they can go to the courts or to the authorities.
- **Illegal or against the Terms:** act in proportion: ask the customer
  to remove it with a deadline, or, when it is serious or urgent,
  suspend the tenant at once (step 4).
- **A threat to the life or safety of persons:** inform the competent
  authorities straight away (in Spain, the Policía Nacional or the
  Guardia Civil), whatever else is decided.

## 4. Suspend, when needed

There is no button: run it from a shell on the master.

```python
env['cloud.tenant'].browse(TENANT_ID).action_suspend()
env.cr.commit()
```

It stops the tenant's instances. A manual suspension does not start the
inactivity deletion countdown, which only applies to tenants suspended
for inactivity; after 14 days with a VPS still reserved, a
`lingering_suspension` alert asks for a decision. To undo it:
`action_reactivate()`.

## 5. Tell the customer

Unless the law forbids it (an authority's order may), write to the
tenant's owner with what was decided: the content concerned, the facts,
the provision relied on (the law or section 9), what was done and for
how long, and how to contest it: by replying, before an out-of-court
dispute settlement body certified under the Digital Services Act, or
before the courts. Tell the sender the outcome too.

## 6. Repeat offenders

Note every notice and decision against the tenant. A customer who
repeatedly hosts manifestly illegal content is warned, and then has
the Service suspended (section 9 of the Terms) and terminated
(section 9A).

## Rollback

A decision contested with reason is reviewed again by a person; if it
was wrong, reactivate the tenant and tell both parties.
