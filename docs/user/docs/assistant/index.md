# Assistant

The assistant is a chat inside your panel. Ask it how the panel works, what an
error in your logs means, or to do something for you — deploy, back up, clone a
staging, or write a small Odoo module and try it on a staging.

It works **as you**: with your role and your projects' access, never more. What
your role cannot do in the panel, the assistant cannot do either. It is an AI,
not a person, and it can make mistakes: check its answers and its changes before
you rely on them.

## Open it

The **Assistant** button is in the top bar, before your user menu. It is there
only if a manager of the panel turned the assistant on and chose you as one of its
users (see [Settings](settings.md)). It opens the chat on the side, over the page
you are on: the assistant knows which project, instance or server you have open.

The first time, a notice — *Before you use the assistant* — tells you what the
assistant sends to the AI provider (see [Privacy and limits](privacy-and-limits.md));
press **I understand** to start. Next to the message field the panel always
shows that you are chatting with an AI and where your messages go.

The top of the panel shows what you have spent this month, approximately, and your
limit if the panel has one. The **Model** list in a conversation offers the models
a manager allowed.

When a manager turns the assistant off, or takes you out of its users, nothing new
can be asked: the button stays while you have conversations, to read or delete them
(see below).

## What it can do

- **Answer from the documentation**, with links to the pages it used, and from
  the panel's public code when the documentation is not enough.
- **Look at your panel**: projects, instances, jobs, alerts, logs.
- **Diagnose**: read a failed job or an instance's logs and explain what went
  wrong.
- **Operate**: deploy, rebuild, back up, clone a staging, change settings — what
  your role allows. Creating instances, deploying and rebuilding need the developer
  role.
- **Write Odoo modules** in your project's GitHub repository (developers and
  managers): see below.

Long operations are jobs: the conversation waits for them and goes on when they
end (*Waiting for a job…*). You can close the panel, or the browser: the assistant
keeps working, and the button shows a number when conversations have news for
you. What you write while it is working waits as *queued* and is read before its
next step. A conversation does one task at a time.

!!! info "IncubaCloud SaaS"
    On a plan whose panel sleeps while nobody uses it, a task can stop if the
    panel falls asleep: open the conversation once it is awake and press
    **Retry**.

## Confirmations

Almost every change waits for you to confirm it in the chat, whatever the
assistant asks. The exception is the work of trying something on a staging:
deploying, rebuilding or backing up a staging, keeping it, dismissing a single
alert, tags, and the code on the assistant's own branch. So, among others, these
**always** wait for you:

- deleting, restoring, moving, emptying a staging's mailbox, dismissing all alerts,
  and anything an [agent](../agents/confirmations.md) would need your approval for;
- **anything that reaches a production instance**, however the assistant names it:
  the card says *This changes production*;
- creating an instance (it takes the server's CPU, memory and disk), and running a
  command in an instance, a staging too;
- changing a project's or an instance's repositories or branches (the card lists
  what is added, removed and moved), creating or connecting a repository;
- writing to a branch that existed before the assistant created it, to a branch a
  production instance deploys from (the card names it), or to a public repository;
  opening a pull request in a public repository, or in a project that makes a
  preview of production for each pull request;
- after the assistant read something other people can write — logs, files of your
  repository, mail a staging caught, a command's output — every change, until you
  write again: that text could carry instructions the assistant must not follow.
  This includes the work on the assistant's own new branch and redeploying its
  staging, which otherwise need no card.

The assistant reads only repositories of your projects and instances, and writes only
to those of your projects. A branch counts as the assistant's when an agent of yours
created it, from any conversation or connected client: writing to it asks for no card,
except after the assistant read text others write in that task, until you write
again.

The card says what will happen, with **Confirm** and **Cancel**. Only you can
answer it, from your browser: the assistant cannot confirm its own actions. A card
lasts ten minutes, and once confirmed it is used once; writing a new message instead
of answering it cancels it, and the assistant is told.

## Writing a module

Ask for what you need, in your words: *"Add an Internal code field to contacts and
show it on the contact form"*. The assistant:

1. reads your project, its repository and its Odoo version;
2. creates a new branch, `assistant/<what it does>` unless you name it;
3. writes the module there, in one or a few commits;
4. offers to create a staging that deploys from that branch (a card), and deploys
   it;
5. installs the module on the staging with a command in the instance (a card: a
   deploy installs no module of yours), and updates it after each change;
6. when something fails, reads the logs, fixes, commits, rebuilds and updates;
7. when it works, gives you the staging's address and opens a pull request.

Once it has read your repository's files or the staging's logs, each of these steps
asks for your card, until you write again (see Confirmations).

Moving it to production is always a separate step that you ask for, and it is
confirmed. Your project needs a GitHub repository: without one, the assistant
offers to create a private one in your GitHub account (with the panel's personal
access token) and to connect it, or explains how to create it yourself.

The panel's GitHub App only reads your code: the panel writes with the personal
access token saved in **Settings → GitHub**. Never under `.github/` (workflows run
with your repository's secrets), and never a password or a key of the panel in a
file. A new branch or a pull request starts whatever workflows your repository
already runs on them.

## Stop, retry, continue

- **Stop** ends the task at the next step. What was done stays as it is: a commit
  on the branch, a staging.
- **Retry** goes on after an error or a stop (the AI provider did not answer, say).
- A task stops after a number of steps (30 unless a manager changed it) and asks
  whether to go on: **Go on** continues.

## Your conversations

The panel keeps each conversation 30 days after its last activity, and only you
can read them in the panel — not a manager either. **New conversation** opens
another; the trash icon deletes one (press **Delete it** to confirm), at any time,
also while the assistant is off for you. What the assistant does is in the audit log
under your name, *via agent Assistant (conversation N)*.

## When something goes wrong

| What you see | What to do |
|---|---|
| *The AI provider refused the panel's key.* | A manager changes it in **Settings → Assistant**. |
| *The AI provider's account has no credit left.* | A manager checks the account on the provider's site. |
| *The AI provider is busy.* / *is not answering.* | **Retry** in a moment. |
| *The panel could not reach the AI provider.* | The server cannot open the provider's address: a manager checks the server's access to the internet. |
| *You reached your monthly limit.* | It goes on next month, or when a manager raises the limit. |
| *The model … has no known price, and this panel has a monthly limit.* | Pick another model in the chat. A manager can remove the limit, or allow a model with a known price. |
| *The AI provider answered with another model than the one allowed.* | **Retry**. If it happens again, tell a manager of the panel. |
| *This conversation got long: I summarized its first part to go on.* | Nothing: it goes on. |
| *This conversation is too long, even summarized.* | Start a new conversation. |
| *The assistant stopped unexpectedly.* | **Retry**. |
| *The panel's GitHub access cannot write to this repository.* | A manager saves a personal access token that can write to it in **Settings → GitHub**. |
| *This confirmation expired.* | More than ten minutes passed: ask for the action again. |
