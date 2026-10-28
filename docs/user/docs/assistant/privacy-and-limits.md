# Privacy and limits

## What goes to the AI provider

To answer, the assistant sends to the provider your panel uses — its name is in the
first-use notice and in the label next to the message field:

- your messages, and the screen you have open (the project, instance or server);
- what its tools read: your projects' and instances' settings, jobs, logs, mail a
  staging caught, files of your repository, pages of the documentation and of the
  panel's public code.

With OpenRouter, the notice says so: what you send goes to OpenRouter and to the
company behind the model chosen.

The panel's secrets — passwords, keys, tokens — never go: they are masked in
everything the assistant reads, and the assistant cannot set them. Do not paste a
password or a key into the chat: the panel masks its own secrets, not yours.

The key and the contract with the provider are your organisation's, and so is
the cost: the provider invoices you, and the consumption the panel shows is only an
estimate. What the provider does with what it receives is set by your contract with
it; check it. IncubaCloud does not use your messages, your conversations or what the
assistant reads to train AI models.

## What stays in the panel

- **Conversations**: 30 days after their last activity, then deleted. Only their
  owner reads them in the panel, also while the assistant is off; a manager does
  not. They are stored in the panel's database without encryption: whoever
  administers the server or the database itself can reach them. A deleted user's
  conversations go with them.
- **Actions**: in the audit log, under the user's name, *via agent Assistant
  (conversation N)*.
- **Usage**: tokens and approximate cost per user and month, for the managers. It
  holds no message, so it stays when a conversation is deleted: deleting one does
  not give the month's limit back.
- **The box ticked to switch it on** (on IncubaCloud SaaS, your acceptance of
  section 12A of the Terms): when, who and from where, at the moment the assistant
  was switched on.

## What it cannot do

- More than your role allows.
- Change anything without your confirmation in the chat, except trying code on a
  staging; and after it read logs, files or mail others write, nothing at all
  until you write again.
- Deploy to production on its own.
- Read anything of the server outside the panel's public code and the
  documentation.
- Follow instructions it finds in logs, files or mail: it treats them as data,
  and an action they ask for still needs your confirmation.

## Limits

| What | Limit |
|---|---|
| Spending | A monthly limit per user, set by a manager in euros (none by default). Approximate: it is checked before each call to the model, so one call in progress can take a conversation a little past it; a call that times out may be billed by the provider without the panel knowing, and the summary of a long conversation is counted, but the limit is not checked before it. A model with no known price counts only tokens, so it cannot be used under a limit. |
| One task | 30 steps unless a manager changed it (1 to 200); then it asks whether to go on. |
| Your messages | 20 a minute; one is cut at 20,000 characters. |
| A confirmation | Ten minutes, used once. |
| A command in an instance | 60 seconds. |
| One commit | 50 files and 180,000 characters. |
| A conversation | When it gets too long for the model, the assistant summarizes its first part and goes on, and says so. The messages stay for you. |
| Panel code and documentation | Read from the public repository of the panel only, 256 KB a file. |
| Conversations kept | 30 days after the last activity. |
