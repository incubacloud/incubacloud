# Repositories on GitLab

A project can deploy and rebuild from GitLab repositories, on gitlab.com or on
a GitLab server of your own, alone or mixed with GitHub repositories in the same
project.

## Add the server

**Settings → Git providers → Add a GitLab server.**

- **Base URL**: `https://gitlab.com`, or your server's address. https only; add
  the port or the path prefix if your server has one
  (`https://git.example.com:8443/gitlab`).
- **CA certificate**: only when a private CA signs your server's certificate.
  Paste the CA's certificate (PEM); the panel shows its SHA-256 fingerprint so
  you can check it. The panel never turns certificate checks off.
- **Credentials**: one token per namespace. The namespace is empty for the whole
  server, or a group, a subgroup or a project path (`acme`, `acme/odoo`,
  `acme/odoo/addons`). A repository uses the token of the longest namespace that
  covers it. A repository no token covers is read as a public one.

The token is stored encrypted and never shown again; leave the field empty to
keep it. It only travels to its own server: in a project that mixes GitLab and
GitHub, each repository is cloned with its own credential.

**Test connection**, with the URL of one of your repositories, checks two
things and says what failed in each:

- **git**: the panel can read the repository with its credential;
- **API**: the token answers on GitLab's API, with its scopes and expiry.

The panel has to reach the GitLab server over the network. A panel limited to
public addresses refuses a server with a private one.

## Which token

| What the panel does | Scope | Token types |
|---|---|---|
| Deploy, rebuild, list branches and modules, pin a commit, the assistant's reads | `read_repository` (Reporter role) | deploy token, project, group or personal token |
| Show a token's scopes and expiry | any scope | project, group or personal token |
| Register the webhook | `api` (Maintainer role; Owner for a group) | project, group or personal token |
| Write the preview's note on a merge request | `api` | project, group or personal token |

- **To deploy**, a **deploy token** or a **project access token** with
  `read_repository`, one per repository, is enough. A deploy token needs its own
  user name (`gitlab+deploy-token-N`) and does not answer on GitLab's API, so
  the panel shows it as *no API*; set its expiry by hand if it has one.
- Project, group and personal tokens expire (one year at most by default). The
  panel warns two weeks before a token it knows the expiry of runs out.
- On gitlab.com Free there are no project or group tokens: use a deploy token,
  or a personal token of a bot user.

## Repository URLs

Type the repository's address in the project's or the instance's repository
list, as `https://gitlab.com/acme/addons`, `gitlab.com/acme/addons` or
`git@gitlab.com:acme/addons.git`. The panel stores it as
`https://gitlab.com/acme/addons.git`: GitLab answers an address without `.git`
with a redirect, and the panel follows no redirects. A line shows its server
as **GitLab · gitlab.com**.

An address of a server that is not under Settings → Git providers is refused,
naming the server. Importing an instance from a host leaves such repositories
out and lists them: add the server and import again.

## Odoo 8.0 to 10.0

The images of these series carry a git too old to be trusted with a token. For
them the panel downloads every repository of the instance before the build,
with git of its own, and the image's git touches none: no token reaches the
image. A repository whose address redirects (a moved project) fails: correct
its address.

## Rebuild on push: webhooks

A GitLab repository rebuilds its instances on every push through a webhook of
the panel. After you save GitLab lines, the repository editor shows the state
of each one's webhook:

- **Register webhook**: with a credential that has the `api` scope and the
  **Maintainer** role in the project (**Owner** for a group), the panel creates
  the webhook in GitLab itself, with push and merge request events and SSL
  verification. Without that role, it says why and you set it up by hand.
- **Set up by hand**: a manager sees the URL and the secret under **Settings →
  Git providers → Webhooks**. In GitLab, open **Settings → Webhooks → Add new
  webhook**, paste both, tick **Push events** and **Merge request events**,
  keep **Enable SSL verification** and save. Then press **Test → Tag push
  events**: never **Push events**, which repeats your last push and rebuilds.
- A manager can register one webhook for a whole **group**, on GitLab tiers
  that have group webhooks: it covers every project below the group.

**Verify** asks GitLab for a test delivery (a tag push, which rebuilds nothing)
and the list shows when it arrived. GitLab allows five tests a minute per
webhook, so the panel waits 15 seconds between two.

GitLab never resends a delivery on its own: one that failed is lost, and on
gitlab.com four failures in a row disable the webhook for a while (forty, for
good). The panel checks every day and warns you when GitLab disabled it; press
**Verify** to enable it again, and rebuild by hand the instances that missed a
push. On a GitLab server of your own this switch is off unless its
administrator turned on `auto_disabling_web_hooks`.

The same delivery sent twice (GitLab's **Resend**) or the same push twice does
not rebuild twice: press **Rebuild** if you want another build.

Webhooks that arrive while the panel is stopped (GitLab's and GitHub's) are
lost: rebuild by hand after a push.

## Merge request previews

With **Preview instances for pull requests and merge requests** on in the
project's settings, a merge request gets a preview as a GitHub pull request
does (see [Pull request previews](../instances/index.md#pull-request-previews)):
a staging named `mr-<number>` on the production's host, with the merge
request's branch and a neutralized copy of the production's data. It needs the
repository's webhook, with **Merge request events**.

- **Opening or reopening** the merge request creates the preview, from each
  production instance whose own repository lines follow the project, unpinned.
  The project's lines, a template for new instances, do not count.
- **New commits** on its branch rebuild it. Editing the title, the labels or
  the reviewers, and approvals, do nothing.
- **Closing or merging** it deletes the preview, also while it is still being
  created: the panel waits for the step running then.
- Once the preview is up, the panel writes a note on the merge request with its
  address, and takes it off when the merge request closes. The note needs a
  credential of the repository with the `api` scope; without one the preview
  works without its note. If GitLab does not answer, the panel tries again for
  about 20 minutes; then, or at once if GitLab refuses the note, an alert says
  the note could not be written: on the preview for its address, on the
  production instance for a note saying why no preview was created. The next
  note that goes through resolves it, and so does closing the merge request.
- **A merge request from a fork gets no preview**: a preview runs the branch
  with a copy of production's data, so only branches of the project itself
  count. The panel says so in a note on the merge request.
- If a preview cannot be created, the merge request gets a note saying why and
  the production instance an alert. Fix the cause, then close and reopen the
  merge request.

## What the panel does not do with GitLab

- Importing a project from Odoo.sh reads GitHub only: Odoo.sh works with GitHub.
- The assistant reads GitLab repositories but does not write to them yet:
  branches, commits and merge requests come in a next version.
