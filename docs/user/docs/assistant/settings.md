# Assistant settings

Managers set the assistant up in **Settings**, **Assistant**. It is off until you
turn it on. Changes are kept with the page's **Save changes**.

Turned off, nobody can ask it anything. Its users keep their conversations, to read
or delete them until they expire, 30 days after their last activity; the button
stays for them meanwhile.

## Provider and key

The assistant uses your own key of an AI provider: **Anthropic**, or **OpenAI**,
**Google Gemini** and **OpenRouter** (marked *beta*: not yet tested with the panel's
tools). The panel talks to each provider at its own fixed address and to no other:
there is no field for a web address. A self-hosted panel needs outbound access to
the provider's address (and to GitHub's, for code).

Paste the key under **Provider key** and save: the panel lists the provider's models
with it first, so a wrong key, or a panel that cannot reach the provider (a
self-hosted panel without internet access), is told at once. The key is stored
encrypted and never shown again; type a new one to replace it, or press **Remove the
key** (the assistant goes off).

Every user the assistant serves uses this one key, each with their own role.

**Changing the provider.** A key belongs to one provider, so type the new
provider's key in the same save. The models are the new provider's: the allowed
models and the default one start empty and the assistant goes off: **Assistant on**
cannot be ticked until the new key is saved. Choose the models, then switch it on
as the first time, box included, and save. Everyone sees the first-use notice
again, now naming the new provider.

## Switching it on

**Assistant on** needs a key, at least one allowed model and a default model, and a
box ticked with it. The box is asked **each time** the assistant is switched on, and
not again while it stays on. The panel records the date, who ticked it and from
which address, and shows it under the form.

!!! info "IncubaCloud SaaS"
    The box is **I accept section 12A (AI features) of the Terms of Service**, with
    a link to the Terms: they say what is sent to the AI provider, who pays for it,
    and which actions wait for confirmation. IncubaCloud records your acceptance on
    your account first, with the version of the Terms, and the panel shows it:
    *Section 12A of the Terms accepted on … by … from … (terms of …)*. If it could
    not be recorded, the assistant stays off and the panel says so; try again later.

On a self-hosted panel there are no IncubaCloud Terms to accept. The box is **I
understand that what the assistant reads is sent to my AI provider, and that it can
make mistakes**, with no link. It is recorded in the panel only, without a version:
*Last switched on, with the box ticked, on … by … from …*.

## Models

The list comes from the provider, with the key; **Update the list** reads it again.
Choose the models your users may pick (**Allowed**), and the default one
(**Default**). **Recommended** models were tested with the panel's tools;
**Untested** ones may fail, mostly at writing modules.

*Price unknown*: the panel does not know that model's price. Without a monthly
limit it can be used, and only its tokens are counted. With a limit it cannot: its
cost could not be counted against the limit, so you cannot allow it (*Not usable
with a monthly limit*), users see it disabled in the chat, and the default model
must have a known price.

The panel asks the provider for the model chosen and no other: it never lets the
provider fall back to another model, and an answer from a different one is set
aside and recorded in the audit log. A conversation whose model is no longer allowed
moves to the default one at its next message, and says so.

## Users

Only the users you tick see the assistant. Choose them one by one; their role
still decides what the assistant may do for them.

What asks for a confirmation is not a setting: the same rules hold on every panel
(see [the assistant](index.md#confirmations)). After the assistant reads text other
people can write — logs, repository files, mail, a command's output — every change
of that task asks the user, the work on its own new branch included, until the user
writes again.

## Limits

- **Monthly limit per user (EUR)**: one value, the same for every user: what each
  user's conversations may cost per month, in euros, approximately (0: no limit).
  When a user reaches it, the assistant stops for them until next month or until
  you raise it. Each user sees their own spending in the chat.
- **Steps per task**: calls to the model one task may make before it stops and
  asks whether to go on (30 by default, from 1 to 200).

## Usage

The table shows each user's tokens and approximate cost per month. Costs are
estimates from the provider's public prices, in euros; your provider's invoice is
what you pay. A row marked with * includes calls of models whose price is not known:
only their tokens count.
