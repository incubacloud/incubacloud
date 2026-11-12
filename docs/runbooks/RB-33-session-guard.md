# RB-33: The session guard

**Severity:** planned
**Typical trigger:** switching the guard on for a panel of your own;
users signed out "for no reason" more than once; a
`session_guard_antiloop` alert; setting a signal to log only.
**Who runs it:** whoever runs the panel.

A stolen session cookie lets its holder in without a password or a
second factor. The guard (`incubacloud/models/cloud_session_guard.py`)
closes a signed-in session, on the server and without asking anything,
when it turns up:

- from **another country** than the one it was opened in;
- from **another browser family** (Chrome, Firefox, Safari… as Odoo's own
  user-agent parser names them; the version, the system and the device do
  not count);
- **idle past its limit**, measured on the server.

Closing destroys the session for whoever holds it — the owner and the
thief alike, since nothing tells them apart — and refuses the request
that showed the change. To the user it is an expired session: they sign
in again. On a page open to visitors (the sign-in, the website's home
or pricing) the request is served instead, signed out, as Odoo serves a
session it no longer accepts there (review of the result, 12-nov-2026,
M1). Nothing is shown about why, so as not to teach the rule to
whoever is being shut out. No question, no second factor, no
confirmation is ever asked (the platform owner's decision, 11-nov-2026).

Jobs, deployments and backups do not depend on a session and keep
running. API keys and agents' tokens are not sessions and are never
judged.

## Switches

System parameters (*Settings → Technical → System Parameters*), read from
Odoo's cache, so a change applies at once without a restart:

| Parameter | Values | Default in core |
|---|---|---|
| `incubacloud.session_guard.country` | `off`, `log`, `close` | `off` |
| `incubacloud.session_guard.browser` | `off`, `log`, `close` | `off` |
| `incubacloud.session_guard.inactivity` | `off`, `log`, `close` | `off` |

`log` writes what would have closed (see *The log*) and closes nothing:
the way to measure a signal before it closes, and the way back without a
deploy.

**Core alone is off by default.** The module may be installed on an
Odoo that is somebody's ERP, where signing in again is a password and a
second factor, not one click. Switched on, it applies to **every user of
the database, portal users included**.

## The rule

What a session is compared with is written into it on the first request
it makes signed in (Odoo's sign-in offers no place to hook): the user,
the country, the browser family, and its idle limit. A session opened
before the guard was switched on takes them from its next request, so
nobody is signed out by switching it on. A session whose user changes is
written again.

The comparison runs only on a request that adds a trace to the session
(Odoo adds one when the platform, the browser or the address is new): a
country cannot change without the address changing. No query is added
to any other request.

| What arrives | What happens |
|---|---|
| Another address or network, same country | Nothing. |
| Another version of the same browser, another system | Nothing. |
| Another country | Closes. |
| Tor (`T1`), on a session with a country | Closes. |
| A request that did not come through the CDN, on a session with a country (on a database whose countries come from the CDN) | Closes. |
| A country the CDN could not tell (`XX`, header absent) | Logged, not closed. |
| Another browser family | Closes. |
| A browser the parser cannot name (`curl`, a script), on a session with a family | Closes. |
| A session that has no country or family yet | Takes the first one seen. |

Only a difference between two values closes, with two exceptions decided
in the review of 12-nov-2026: Tor and a request that skipped the CDN
(A1), and an unnamed browser (M2). They are how a cookie is tried away
from its owner, and a silent value must not be a way around the check.
To automate against the panel, use an API key.

### Where the country comes from

One source per database, never both: two sources can disagree on one
address, and that would be a false close.

- **Behind a CDN** (`x_real_ip_header` and `x_real_ip_trusted` set, see
  [RB-25](RB-25-visitor-address-behind-a-cdn.md)): the CDN's header named
  by `x_real_ip_country_header` (Cloudflare: `CF-IPCountry`, with *IP
  Geolocation* on for the zone), believed only on the proof that the
  visitor's address is. Without that option no country is compared. A
  panel deployed by IncubaCloud on a host behind Cloudflare gets it on its
  next deploy.
- **Otherwise**: Odoo's GeoIP, when the image carries its database
  (`geoip_country_db`); without one, no country is compared. An update of
  the GeoIP database can move an address to another country, and the
  sessions using it then close once.

### Inactivity

Odoo's `sessions.max_inactivity_seconds` sets the cookie's lifetime and
the daily sweep of session files; a thief does not have to respect the
first, and the second runs once a day. With `inactivity` on, the guard
compares, on every request, the session's last activity (the latest of
its traces, before Odoo refreshes it) with its limit:
`incubacloud.session_guard.inactivity_seconds`, or Odoo's own parameter
when that is not set. Odoo refreshes a trace once an hour, so a limit can
fall up to an hour short. The limit is written into the session when it
is opened: a change of the parameter, or of the user's role, reaches the
sessions opened afterwards.

Lowering the guard's limit leaves the cookie at Odoo's lifetime: whoever
comes back after the guard's limit and before the cookie's brings a
session that is closed. Set `sessions.max_inactivity_seconds` to the same
value. The platform's master and panels set it to 2 days on every update
(12-nov-2026); operators of the master, at 12 hours, keep the 2-day
cookie.

## The anti-loop

Some connections change country without the user doing anything: IPv4
and IPv6 placed in two countries, a company proxy with exits in several.
Without a brake that user would be signed out every few minutes.

| Parameter (`incubacloud.session_guard.`) | Default | Why (12-nov-2026) |
|---|---|---|
| `antiloop_closes` | 3 | Three closes of one user by one signal; the next only logs. One is a trip, two can be a VPN going on and off, three in a day is a connection that alternates. |
| `antiloop_window_hours` | 24 | The window they are counted in: a day. |
| `antiloop_hold_days` | 7 | How long the signal then only logs for that user. Long enough for someone to look at it; it starts again while the same pair of values keeps showing up (review M4), so an alternating connection does not go through three closes every week. |

Closes are counted by **distinct session**, not by request: the parallel
requests of one page each load the session before the first of them
deletes it, and each sees the change (review A2). A hold logs every
request it lets through as `held`.

When a hold starts, a `session_guard_antiloop` alert is raised, **once
per user and pair of values**. It means: look at whether it is the
user's connection or someone else using their sessions. The hold is per
database: a user with an alternating connection learns it separately on
the master and on each panel.

The alert is raised once the hold is committed, on a connection of its
own and outside the user's lock (re-review of 12-nov-2026, M-a): it can
page somebody over Telegram or a webhook, which may take seconds, and the
other requests of the same page wait for that lock. Raised under it, they
waited past `LOCK_TIMEOUT` and closed the user the hold was for. If
raising it fails (`could not raise the anti-loop alert`), the hold stays
and that pair is not alerted again.

## Writes and failures

Every row is written on a database connection of its own, never on the
request's, and committed before the request is refused or served.

| Value (in code) | Value | Why (12-nov-2026, review of the result and its re-review) |
|---|---|---|
| `READ_COMMITTED` | on | The guard's connection is switched from Odoo's REPEATABLE READ to READ COMMITTED before anything runs on it. In REPEATABLE READ the snapshot is taken when the user's lock is asked for, before the wait: the parallel requests of one page then failed to write the rows the first had written, and two sessions of the user closing at once both missed the other's close in the anti-loop's count, so the hold came a close later. In READ COMMITTED each statement sees what was committed when it starts. Not on a test's connection, which is one transaction already begun. |
| `WRITE_ATTEMPTS` | 8 | In READ COMMITTED the requests of a page do not collide; the attempts stay for a connection left in REPEATABLE READ and for a deadlock. There, queued on the user's lock, the n-th request of a burst can need n connections, and Odoo 19's web client opens a page with more than three calls at once (behind the CDN, over HTTP/2, the browser does not hold them to six). Past eight the request closes the session, even for a user in a hold (A1). |
| `RETRY_PAUSE` | 10–50 ms, at random | The pause before trying again, so the requests that collided do not collide again together. |
| `LOCK_TIMEOUT` | 3 s | A close waits for another close of the same user under a lock and holds a second connection meanwhile. Past three seconds it closes without recording. |

**A failure of the guard lets the request through.** Odoo turns anything
raised while it checks a session into a 403 on every route, the sign-out
included; the guard logs `[session-guard] the guard failed on a request`
with the traceback and the request is served. A row that cannot be
written (`could not log`, `could not record the re-entry`) is logged the
same way. A close whose row cannot be written still closes (`closing it
anyway`). What a layer does on a close (`_session_guard_closed`) failing
keeps the row (`what a close does beyond this database failed`). A
session whose files cannot be deleted is closed anyway (`could not delete
the files of a closed session; closing it anyway`): the request is still
refused. A write that collides is one line at INFO, `a write collided
with a parallel request (<error>); trying again (n of 8)`, without
Odoo's `bad query` ERROR.

New code serving before its update has run has no `cloud_session_closure`
table: every pin and log fails and is logged, and the requests are served;
only a close still closes. Updating the module ends it.

## The log

`cloud.session.closure`, one row per session, signal, action and pair of
values (`hits` counts the requests that repeated it):

- **action** — `closed`, `logged` (the signal is in `log`, or the
  country could not be told) or `held` (the anti-loop);
- **pinned / seen** — `ES → FR`, `chrome → firefox`, `ES → direct`,
  `48h → 49h`;
- **re-entry** — seconds until the user signed in again, and from which
  country and browser, if within `reentry_window_hours`.

| Parameter (`incubacloud.session_guard.`) | Default | Why (12-nov-2026) |
|---|---|---|
| `reentry_window_hours` | 24 | A sign-in a day after the close says nothing about it. |
| `log_retention_days` | 90 | Enough for the fortnightly and monthly reviews; holds that ended as long ago go with them. |

How to read it:

| What follows the close | Reading |
|---|---|
| Re-entry within minutes from the **new** value, the old one never back | **False close**: the same person (a trip, a VPN). |
| Re-entry from the **old** value, the new one never signs in | **Probable theft**: someone else used the cookie. |
| Closes back and forth between the same two values | **Alternating connection**: the anti-loop takes it. |
| Nobody signs in again | No reading. |

A signal with more than **one false close per active user and month**, on
average, is a signal to put back to `log` and review.

```sql
-- closes and would-be closes of the last week, by signal and action
SELECT signal, action, count(*) FROM cloud_session_closure
 WHERE create_date > now() - interval '7 days' GROUP BY 1, 2;
-- false closes: back within an hour from the value that closed
SELECT signal, count(*) FROM cloud_session_closure
 WHERE action = 'closed' AND reentry_seconds < 3600
   AND seen_value IN (reentry_country, reentry_browser) GROUP BY 1;
```

## Known limits

- **A thief in the same country with the same browser family** is not
  closed. Copying the User-Agent and renting an address in the victim's
  country is cheap, and the parser sees almost everything as `chrome`.
- **A thief who also holds the user's Google or GitHub session** signs in
  again after a close. The close still signs the owner out, who notices.
- **A request of the closed session already in flight** when it closed
  may write the session's file back as it finishes. A layer can prevent it
  by making the session's token stale (`_session_guard_closed`); core
  alone cannot yet.
- **A browser extension or the developer tools' device emulation** that
  changes the User-Agent mid-session closes it. One sign-in.
- **Odoo 18** has the same trace and the same hook, but no rotation of
  the session id every 3 hours: an active session there has no other
  absolute limit than this inactivity.
- **Cloudflare's ranges out of date.** The proof of the country is the
  proof of the address (`x_real_ip_trusted`, [RB-25](RB-25-visitor-address-behind-a-cdn.md)).
  A request through an edge whose range is not listed counts as one that
  skipped the CDN: on a session with a country it closes, with
  `seen_value` `direct`. The anti-loop stops it after three closes per
  user. Symptom: closes `→ direct` from users who signed in again at once.
  Remedy: set `incubacloud.session_guard.country` to `log`, then bring the
  ranges up to date (managed hosts get them daily; the master's are in its
  production configuration).

## Rollback

Set the signal's parameter to `log` (keeps measuring) or `off`. Nothing
else changes: no session is touched by switching the guard off, and the
rows stay until their retention.
