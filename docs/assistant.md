# Assistant (`incubacloud_assistant`)

An AI assistant inside the panel: a chat that answers from the documentation, reads
logs, operates the panel and writes Odoo modules on a branch of the project's
repository, with the customer's own AI provider key and the user's own role. User
guide: [docs.incubacloud.io/assistant](https://docs.incubacloud.io/assistant/)
(`docs/user/docs/assistant/`).

## What it is

A separate module that depends on `incubacloud_mcp`, `bus` and `queue_job`: its tools
are the MCP's, so it needs the MCP's catalog, secret filter, confirmations and audit.
It adds:

- a button in the SPA's top bar (`AssistantButton`, before the user menu) that opens a
  side panel, and an **Assistant** tab in Settings, after the MCP's **Agents**, kept
  mounted once opened like core's tabs;
- the models `incubacloud.assistant.conversation`, `.message` and `.usage`, the
  engine (`incubacloud.assistant.engine`), the tools' view
  (`incubacloud.assistant.tools`) and the settings fields of `cloud.settings`;
- fifteen routes under `/cloud/assistant/` (see [`api-endpoints.md`](api-endpoints.md)),
  none of which an agent can call: the MCP excludes all of them.

Installing it adds nothing to a user's panel until a manager switches it on.

## A task, step by step

A message of the user starts a **task**: a chain of queue jobs of one step each, on the
channel `root.assistant`, run as the conversation's owner.

1. `conversation._post` stores the message, clears the `tainted` mark if nothing
   runs, and `_enqueue_step` queues `_assistant_step` (the state becomes `working`
   and `job_uuid` is the queue job's). What the user writes while a step runs is
   stored as *pending* and enters the context before the next call to the model.
2. `engine._step` runs `_run_step`, which never raises: a failure leaves the
   conversation in `error`, with Retry.
3. `_check` reads the settings and the key together, in one transaction, and stops
   the step when the person pressed Stop, the assistant is off or no longer theirs
   (`_assistant_available_for`), the monthly limit is reached, or the step cap is
   (a notice that offers *Go on*).
4. `_ask` builds the request (the fixed prompt, the tools the user's role sees, the
   turns), calls the provider with up to three retries (`RETRY_DELAYS`, or the
   provider's `Retry-After`), and on *context too long* summarizes the first half of
   the context once (`_summarize`). The pause between attempts happens outside any
   transaction.
5. `_record_answer` stores the answer, its cost and the month's usage;
   `_wrong_model` sets aside the answer of a model other than the one asked for.
6. `_run_calls` runs the model's tool calls in order, each in its own transaction
   (`_run_tool` inside `_durable`), which writes nothing of the conversation; the
   answer is recorded by `_record_call` in another. Before the tool runs, the call is
   marked in `running_call_id` on a cursor of its own: a step that dies between the
   tool and its answer leaves the mark, and the next step answers *outcome unknown*
   instead of running the tool again. A concurrency error in the tool is never
   retried (the tool may have acted outside the database: a command, a commit); the
   model reads it. The mark and the answer only write the database and are retried.
7. The next step is queued, or the task stops where it waits: a card
   (`waiting_confirmation`), a job (`waiting_job`), the person (`idle`, `stopped`)
   or a failure (`error`).

The states are written by the conversation (`_enqueue_step` sets `working`; `_halt`
sets the others) and by the card and job hooks. **`job_uuid`** is the queue job of
the step in course; every write of a step first checks (`_conversation_in`) that its
job is still the conversation's, so a step healed as lost and retried changes nothing
afterwards. `_heal` takes a step running longer than `STUCK_AFTER` (one hour), or whose
queue job ended with nothing after it, for lost: `error`, with Retry. It runs when the
conversation is read and in the daily cron.

## Why each tool runs in a request of the user's own

The panel's routes read `request.env`. Inside a queue job the live request is the
runner's (`/queue_job/runjob`): with the pinned `queue_job` (19.0.2.1.6, `da26647`) it
does `request.update_env(user=SUPERUSER_ID)`; in earlier versions it has no user.
Neither will do: a tool would run as the superuser, or fail. `request_scope.tool_request`
binds a request of the user's own — their environment, on the call's cursor — while the
tool runs, with the mark that audit rows and jobs read (`VIA_REQUEST_ATTR`). The MCP's
`incubacloud.mcp.server._tools_call` then runs the tool exactly as it runs an agent's:
role check, schema, secret filter, confirmation, savepoint, audit. The call context
names the version `assistant`, which no MCP client can declare, and the principal's
credential is the conversation (`assistant:<id>`). The routes a tool reaches limit by
user; their limits by address (`/mcp`, `/health`) are not in play.

## Confirmations

- `incubacloud.mcp.confirm._gate` answers a call whose context version is `assistant`
  with a **card** instead of the MCP's channels: one `incubacloud.mcp.approval` of the
  new channel `assistant`, bound to the user, the conversation, the tool and the
  arguments' fingerprint (which includes whether production is reached).
- A card is answered by `/cloud/assistant/confirm` or `cancel`, from the person's own
  request: `message._confirm_card` refuses any request that carries the agent mark, and
  the MCP excludes these routes. The model has no tool that confirms. A pending card is
  withdrawn when the person writes instead. The approval lasts ten minutes from the
  yes, not from the question: the step that uses it may wait in the queue.
- **The rule is open.** The MCP's own confirmations apply as they are (destructive and
  sensitive tools; the code tools' branch rule). On top, `assistant.tools._extra_confirmation`
  asks for every tool that is not read-only, except those of `_UNASKED` (deploy and rebuild,
  keep, back up a staging, dismiss one alert, tags, and the code tools, which keep the
  MCP's approvals), and only while every instance their arguments name is a staging.
  A test lists `_UNASKED` and checks that none is of the kinds section 12A of the
  Terms names (delete or restore data, incur charges, deploy to production, write to an
  existing branch, change security settings).
- **Production** is found by any id in the arguments (an instance, an alert, a
  repository line, a server): the card says *This changes production* and names the
  instance if the user can read it, and says there is one if not. Creating instances,
  `run_command`, and changing the repositories of a project or an instance always
  ask; the card lists what is added, removed and moved.
- **`tainted`.** After a tool marked `untrusted` (logs, repository files, mail, command
  output), `compare_config` or the end of a job, `conversation.tainted` is set; every
  change then asks until the person's next message enters the context — also the
  work on the agent's own new branch and redeploying its staging. `TAINT_COVERS_OWN_WORK`
  (`True`, the owner's decision of 28 October) is the one switch that decides whether
  the mark covers that work. Names and notes that `search` and `get_project` answer
  are written by the panel's own users under their roles and do not mark.
- The code tools read only repositories of projects or instances the user reads and
  write only to those of projects (`incubacloud.mcp.tools._authorized_repo`).

## Jobs

A tool that queues a job (deploy, rebuild, backup, clone) leaves the conversation in
`waiting_job`, the other calls of that answer are answered *not run* and the model asks
again when the job ends. `cloud.job._after_job_terminal` (the hook of core, in the
transaction that records the end) calls `engine._job_ended`: the outcome — state, error
and the last lines of the log, read as the owner and masked — becomes a message, the
conversation is `tainted` (a job's log is written by the code it ran) and the next step
is queued. A hook that failed is picked up by `_heal` when the conversation is read.
A job that never ends is the job watchdog's business: the conversation keeps waiting
and the user can Stop.

## Providers

`connectors/base.py` holds the one shape the engine speaks: `ChatRequest`, `Turn`,
`ToolCall`, `ChatResponse`, `Usage` and `ProviderError` with a `kind` (`invalid_key`,
`quota`, `rate_limited`, `unavailable`, `unreachable`, `context_too_long`,
`bad_request`). Anthropic, OpenAI, Gemini and OpenRouter are called over HTTP with
`requests` (`connectors/_http.py`: no redirect, 8 MB at most, no SDK, no streaming —
one step is one job that waits for the whole answer, up to 600 s between reads) at
fixed addresses; no setting takes a URL. OpenAI, Gemini and OpenRouter are marked beta
(`providers.PROVIDERS`).

- Anthropic: the API version is pinned (`anthropic-version: 2023-06-01`), with the
  beta that lets a model drop a thinking block whose conversation changed (a summary,
  tools that changed with the role), and prompt caching on the system block.
- **Only the allowed model.** A request names the model asked for, without `fallbacks`
  or a router (OpenRouter's routers and Gemini's `-latest` aliases are not offered).
  `same_model` compares the answer's model: the same id or a dated version of it.
  `_wrong_model` sets another answer aside (it never enters the conversation), counts
  what it cost, leaves the conversation in `error` and writes an audit row for the
  user.
- The key is read in sudo together with the provider (`_assistant_credentials`):
  a key is never sent to a provider it was not saved for. It is an `EncryptedChar`
  of the system group, so a manager cannot read it through the ORM, and the MCP's
  secret filter masks it wherever it appears.

## Costs and the monthly limit

`pricing.py` holds prices in US dollars per million tokens (Anthropic, OpenAI and
Gemini; OpenRouter's come with its listing), `EUR_PER_USD` and the date they were
last checked (`CHECKED_ON`). A model id matches itself or a dated version of itself,
never a prefix. Prices that change with the prompt's size are in `LONG_PROMPT`.
Without a price the cost is unknown: tokens are counted and the conversation shows
*≈ ?*.

`incubacloud.assistant.usage` keeps one row per user, month, provider and model, apart
from the conversations: deleting one does not undo the limit. The rows hold no content
and do not expire. The limit is one value for the panel, the same for every user;
each user sees theirs in the chat and a manager the table. **With a limit, a model
without a known price is not used** (`pricing.model_usable`): the selector shows it
disabled, `send` and `set_model` refuse it, the engine stops with a message that says
how to fix it, Settings does not let it be allowed or be the default, and saving a
limit with such a default is refused.

## Off

Switched off (or the user out of its users), nothing new is asked, confirmed, retried
or changed: `send`, `set_model`, `retry`, `confirm`, `cancel` and `accept_notice` answer
an error. The owner can still list, read, delete and stop their conversations, and the
button stays, read-only, while `status.conversations` is not zero, until the
30-day purge (`_cron_purge`, daily, which also heals lost tasks). A task in course stops at its
next step's check and says so. On IncubaCloud's own master the assistant is blocked:
`incubacloud_saas_manager` sets `incubacloud_assistant.blocked`, `_assistant_available_for`
is false and `save_settings` refuses to switch it on.

## Switching it on: the box, or section 12A

`save_settings` moves the assistant from off to on only with `accept_ai_terms: true` in
the same save: a box asked each time it is switched on. A new provider puts it off
(no model is allowed yet), so switching on again asks for the box too, also in the
same save as the provider. Three hooks of `cloud.settings` make the box what the
panel has to accept:

| Hook | Core alone (self-hosted) | Tenant panel (`incubacloud_tenant`) |
| --- | --- | --- |
| `_assistant_terms_label()` | «I understand that what the assistant reads is sent to my AI provider, and that it can make mistakes» | «I accept section 12A (AI features) of the Terms of Service» |
| `_assistant_terms_url()` | `""`: no link | The platform's Terms |
| `_assistant_record_terms_acceptance(acceptance)` | Kept in the panel, version empty | IncubaCloud records it on the customer's contact first and answers the version |

`get_settings` answers `terms_label` and `terms_url`; the tab shows the link only when
there is an address, and the refusal without the box quotes the label. The record is
four system-only fields (date, version, user, address). The tab's line under the form
names section 12A and the version when there is one, and otherwise says only when,
who and from where. A self-hosted panel has no contract with IncubaCloud: it never
links or names its Terms. `_assistant_available_for` requires that an acceptance
exists.

The assistant's settings fields are written only by the routes (`save_settings`,
the model refresh), never through the ORM by a manager.

## Limits that are not exact

- The monthly limit is checked before each call to the model, so a conversation in
  course can pass it by one call; a call that times out may be billed by the provider
  without the panel knowing; a failed answer the provider bills (its tokens arrive with
  the error) is counted; the summary of a long conversation is counted but the limit
  is not checked before it; a model without a known price counts as zero, which is why
  it cannot be used under a limit; the cache OpenRouter does not separate is counted as
  full input.
- On a tenant panel of 2 workers (`root:1`), the channel `root.assistant:1` adds no
  capacity: a step and a deploy wait for each other. Production sets
  `root.assistant:3`. Without a line for the channel in `[queue_job] channels`, its jobs
  share root's.
- A new branch or a pull request starts the workflows the repository already runs on
  them; the assistant cannot write under `.github/`.
- A free tenant whose panel falls asleep can lose a running task; the user retries it
  when it wakes.

## Sources

`sources.py` serves `search_docs`, `search_code` and `read_code`. The documentation is
`docs/user/docs/**/*.md` of core's repository, searched by keywords (in English), by
section, answering the page's link and anchor. The code is a **closed list** of
top-level entries of core's repository (`PUBLIC_ENTRIES`), found from the real path
of the `incubacloud` module: on the master and on tenant panels the private code of
saas is installed beside it, and nothing else on the disk is read. `AGENTS.md`, the
repository's tooling and notes (`NOT_READ`) stay out. Only text files of known
extensions, no hidden folder, `static/lib`, `site` or caches, 256 KB a file, 5 s a
search; a path with `..`, a link out of the folder or a binary file is refused. A test
compares the list with `git ls-files`. These tools are the assistant's own and are not
in the MCP's catalog: an external agent has its own means of reading public code.

## Extending

- **A provider** is a class with `@register` in `connectors/` that gives
  `list_models()` and `chat()` in the common shape, plus its row in `providers.PROVIDERS`
  and its prices in `pricing.py`.
- **A confirmation rule** goes in `incubacloud.assistant.tools._extra_confirmation`
  (return the text the person confirms), or in the tool's own `_confirm_<tool>` of the
  MCP if agents need it too.
- **A new MCP tool** is the assistant's with no more work: it sees the catalog,
  filtered by role. A test fails for a tool that writes and neither asks nor is
  reviewed in `_UNASKED`.
- **A module on top** that must not be reachable by agents classifies its routes in
  `incubacloud.mcp.catalog._route_policies()`, as the assistant does for its own.
