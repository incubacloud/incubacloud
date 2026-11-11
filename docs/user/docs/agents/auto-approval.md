# Auto-approval

Auto-approval lets **one credential of one person** act without asking, until a
date or with no end date: an API key, an OAuth client (an MCP client the person signed in with), or
the panel's [assistant](../assistant/index.md). While the rule is active, every
action that would wait for an [approval](confirmations.md) runs at once. Every
other agent keeps asking.

It is for an agent you leave working on its own — a dedicated key for an
automation, say — while the agents you use day to day keep asking you first. It
is off until an Administrator grants a rule.

!!! danger "What you give up"
    An approval is the moment a person reads what an agent is about to do. With
    a rule there is none, for **every** action the person's role allows,
    production included. Read the risks below before you grant one.

## What runs without asking

Everything the [Approvals](confirmations.md) page lists, and nothing is held
back: deleting projects, instances, servers and backup destinations; restoring
over production; commands on production instances and on servers; security
settings and purging the audit log; sign-in links and backup downloads; writing
to any branch and to public repositories. For the assistant, also what it would
show a card for, including changes after it read logs, files or mail.

What stays as it is: the person's role and projects, every check the panel makes
before it would ask (an action it refuses is still refused), secrets (they never
reach an agent), and the limits on calls.

!!! info "IncubaCloud SaaS"
    Auto-approval is not available yet on the panels of IncubaCloud SaaS:
    **Settings**, **Agents** says so, and a grant is refused. Once it is,
    charges are the exception you choose: the actions that charge money —
    orders, purchases, plan changes billed to your account — stay out of a
    rule unless you tick **Include charges** when you grant it. Without it
    they keep asking, as before; with it they run without asking too. The
    notice, the list of rules and the agent's list of tools say whether a
    rule includes them.

## The risks

The panel stops protecting you from three things:

1. **The model's mistakes.** A wrong call — another instance, another backup —
   runs at once. Agents treat "the panel asks first" as a safety net and call a
   tool to *propose* an action; with a rule there is no net. Deleting, restoring
   over production, continuous recovery and removing a frozen copy cannot be
   undone.
2. **Instructions hidden in what the agent reads**: a log, a captured mail, a
   file of a repository, a command's output, the end of a job, an alert, and
   anything it reads outside the panel, such as an issue or a web page. If the
   agent follows them, they run, on production too, and nobody sees them
   before. What such an instruction aims for is deleting or restoring, a
   command on a server, or taking data out: a sign-in link, a backup download,
   a public repository, or the notifications sent to another chat.
3. **A leaked credential.** Whoever holds the key or the client's token acts as
   that person, without asking, until the rule ends. Revoke the rule *and* the
   credential.

Also keep in mind:

- **A rule for an Administrator** reaches everything an Administrator does:
  security settings, trusting a server's SSH key without anyone comparing it,
  commands on every server — the panel's own included, from which their agent
  can write to the panel's database, rules too — and purging the audit log.
- **An agent that drives an Administrator's browser**, with the panel open,
  can grant a rule, as it can approve an action: the panel refuses a
  grant that comes through an agent's connection, but cannot tell a browser
  an agent drives from the person.
- **Whoever has a shell on the panel's server, or is Odoo's system
  administrator**, has the panel's database, and with it the rules.
- **With an OAuth client**, the rule covers every session of that person with
  that application, on any device. Disconnecting the agent does not end the
  rule: if the person connects the same application again, it applies again.
  Revoke the rule too.
- **Who answers for it.** The audit names the person whose credential acted, not
  the Administrator who granted the rule.
- **Revoking does not undo.** What already ran, or is queued, stays.
- **The audit records actions, not reads.** It says what the agent did, not what
  it had read before doing it.
- **The audit is not kept for ever.** Its retention (**Settings**, **Audit Log
  Retention**), **Purge old logs now** and an agent's `purge_audit_logs` delete
  old rows, those of what a rule approved included; the rule, its count of
  calls and its last use stay. A rule's **Activity** may then list fewer
  actions than it counts.

## Grant a rule

Only an Administrator grants a rule, from the panel; no agent's connection can
grant, extend or revoke one.

1. Connect the agent first: a rule is granted to a credential that has already
   connected to the panel (the assistant is offered for the people who use it).
2. Open **Settings**, the **Agents** tab, and choose **Grant auto-approval…**.
3. Choose the **Person**, their **Credential** (each one shows its id and when
   it last connected, so two keys with the same name are told apart), and how
   long: 7, 30 or 90 days, **No end date**, or until a date. Nothing is chosen
   for you; the panel recommends 90 days, the usual rotation period of a
   credential. A rule with no end date lasts until it is revoked, the API key
   is deleted or the person is archived: how long is your decision, and a
   date is safer.
4. Read the notice. It names the person, the credential and the date (or says
   the rule has no end date) and lists the risks above. Tick **I understand,
   and I take responsibility for what this agent does without asking** and
   choose **Grant auto-approval**.

The panel keeps who granted it, when, from which address and which version of
the notice. A rule cannot be extended: when it ends, grant a new one, with the
notice again. Granting a rule to a credential that has one replaces it.

## When a rule ends

A rule stops approving at once, at the next call, when:

- its date passes, if it has one;
- it is revoked: an Administrator revokes any rule, and a person their own,
  from **Settings**, **Agents**, or from **Connect an agent**;
- the person is archived: their rules end, and stay ended if the person is
  reactivated;
- the API key is deleted or expires.

When the person's role goes **up**, the rule is **paused**: it was granted for
what the person could do then. An Administrator grants a new one if it should
cover the new role.

## Where you see it

- **Settings**, **Agents**: every rule, with its person and role, credential,
  who granted it and when, its date (or *No end date*), when it was last used,
  how many calls it approved, and its state (active, paused, expired or revoked). **Activity**
  lists what the rule approved, with its grant and its end.
- **Connect an agent**: the person a rule covers sees *… acts without asking
  until …* (or *… with no end date*), with **Revoke**.
- **The audit log** of instances and servers: *auto-approved* after the agent's
  name (the rule and who granted it, on hover), and *approved* when the person
  approved the action.
- **The jobs**: the history and an instance's activity show *via agent … ·
  auto-approved* under the user.
- **The assistant's chat**: each action shows *ran without asking*, and says so
  when the assistant had read content other people can write.
- **The agent itself**: its list of tools says which run without asking, and
  until when (or that the rule has no end date).
