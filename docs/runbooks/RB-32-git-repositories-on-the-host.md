# RB-32 — The repositories downloaded on the host

**Severity:** warning · **When:** «Fetch the repositories» fails or runs out
of time on a series, a series has to go back to cloning in the image, the
build record or old images of a host have to be cleared now, the personal
access token is rotated, the host's cache of trees takes too much disk, or
the instances still to be rebuilt are wanted.

From core 1.0.222 every series but 19.0 downloads each repository of
`repos.yaml` on the host, in doodba's image used as a helper, before the
image is built with `AGGREGATE=false`
([architecture](../architecture.md#safe-rebuild)). No credential enters the
image, its build or the instance's container. An instance moves to this at
its next deploy or rebuild; until then its image and its `repos.yaml` on the
host still hold the token they were built with.

## 1. Diagnose a failed or slow download

The job's log names the repository and the cause:

| Log line | Meaning | Action |
|---|---|---|
| `<alias>: the server redirects; update the repository's URL` | The server answered a redirect, which is never followed | Correct the line's URL in the panel |
| `<alias>: its URL is refused` | A credential for another server, or not over https | Correct the URL or the Git provider |
| `<alias>: the download failed twice (status N)` | git-aggregator failed; its last lines are above | Read them: a branch that does not exist, a merge conflict, a repository nobody reads |
| `... neither the GitHub App's installation nor the PAT reads it` | No credential sees the repository | Add it to the App's installation, or give the PAT access |
| `the step's budget of 1200s ran out ...` | The downloads together took longer than the budget | Rebuild again: what arrived is kept and only the rest is downloaded |
| `<alias>: the commit that arrived is not the one pinned` | A pinned commit the server does not hold | Correct the pin |
| `Refused: GitHub's API did not answer for <owner>/<repo> ...` | GitHub's API failed twice (timeout, 5xx, rate limit) while the panel asked who reads the repository | Run the job again; the repository is never fetched without credentials instead |
| `<alias>: its pinned commit could not be asked with its credential ...` | The host's cache has the tree, but this instance's credential no longer reads the repository | Nothing, if access was taken away on purpose: the download that follows fails naming it. Otherwise give access back |
| `an earlier download of this instance's repositories (PID N) is still running: it is stopped` | A run an earlier job left (its worker died) was still downloading | Nothing: the new one goes on |
| `<alias>: its tree could not be linked from the host's cache ...` | The cache and the instance are on different file systems, or the disk is full | Check `df -h ~`; a copy takes the tree's whole size per instance |

A rebuild whose download fails keeps the instance on its previous image, as
a failed build does. The trees may be left part new, part old; the next run
continues from the markers.

On the host, from the instance's directory:

```bash
$ ls -la odoo/custom/src/.ic-heads/                 # one marker per repository
$ cat odoo/custom/incubacloud-heads.txt             # the commit of each merge
$ cat odoo/custom/src/.ic-fetch-seconds             # the seconds of the last download
$ du -sh ~/.incubacloud/git-trees                   # the host's trees
```

## 2. Send a series back to cloning in the image

Without a new version of core: set the system parameter
`incubacloud.repos_in_image_series` (Settings → Technical → System
Parameters) to the series, separated by commas, for example `18.0,20.0`.
8.0 to 10.0 cannot go back: their image's git is not safe.

The next deploy or rebuild of each instance of that series removes its
downloaded trees, markers and `odoo/Dockerfile.dockerignore` (step «Record
repository heads») and the image clones again, with the token in
`repos.yaml`. Empty the parameter to return.

## 3. Clear the build record and old images now

The daily maintenance (`docker_prune`) removes Docker's record of past
builds, the build cache and the images no container uses. To do it at once
on a host:

```bash
$ docker buildx history rm --all
$ docker builder prune -af
```

Old images are removed by the daily maintenance once the instance runs a new
one.

## 4. The host's cache of trees

The trees live in `~/.incubacloud/git-trees` of the account the panel
deploys with, one per repository and commit; each instance's tree is made of
hard links to one. The account deploys for one panel only: two panels on one
account would share the trees, as they already share Docker.

- **Its size.** `du` counts a file once however many links it has, so
  `du -sh ~/.incubacloud/git-trees` is the cache's real size and a `du` of an
  instance's `odoo/custom/src` counts the same files again. What the cache
  and the instances take together:

  ```bash
  $ du -shc ~/.incubacloud/git-trees ~/*/*/odoo/custom/src
  ```

- **What the daily maintenance removes:** a tree no instance links any more
  (its `head` has one link) unchanged for two hours, an entry without `head`
  two hours old, and downloads cut short. While a job holds the cache it
  leaves them for the next night.
- **Empty it by hand**, when no job runs on the host: the instances keep their
  trees, which are links, and the next download of each commit fills it
  again.

  ```bash
  $ flock -x ~/.incubacloud/git-trees/.lock \
        find ~/.incubacloud/git-trees -mindepth 1 -maxdepth 1 ! -name .lock \
        -exec rm -rf {} +
  ```

## 5. Rotate the personal access token

Rotate it once every instance has been rebuilt with this version (section
6), so that no image, `repos.yaml` or copy of a host still holds one that
works. Settings → GitHub → personal access token: save the new one. The
GitHub App's tokens expire within the hour by themselves.

## 6. The instances still to be rebuilt

Instances whose last successful deploy or rebuild is older than the update
of the panel to 1.0.222 (replace the date):

```sql
db$ SELECT i.id, i.name, i.odoo_version, max(j.date_done) AS last_build
    FROM cloud_instance i
    LEFT JOIN cloud_job j ON j.instance_id = i.id AND j.state = 'done'
        AND j.job_type_id IN (SELECT id FROM cloud_job_type
                              WHERE code IN ('deploy_instance', 'rebuild_instance'))
    WHERE i.active AND i.odoo_version <> '19.0'
    GROUP BY i.id, i.name, i.odoo_version
    HAVING max(j.date_done) IS NULL OR max(j.date_done) < '2026-11-11'
    ORDER BY last_build NULLS FIRST;
```

Rebuild each from the panel.

## Rollback

- **Of a series:** section 2.
- **Of core to a version before 1.0.222:** an instance already moved does not
  build until, in its directory on the host, `odoo/Dockerfile.dockerignore`
  and the trees of `odoo/custom/src` are removed (everything but `private`,
  `repos.yaml` and `addons.yaml`, and the `.ic-*` entries):

  ```bash
  $ cd ~/<project>/<instance>
  $ rm -f odoo/Dockerfile.dockerignore
  $ find odoo/custom/src -mindepth 1 -maxdepth 1 \
        ! -name private ! -name repos.yaml ! -name addons.yaml \
        -exec rm -rf {} +
  ```

  The parameter of section 2 is the way intended; this one is for a
  rollback of core. The older core does not know the host's cache of trees
  either: once every instance is back, remove it too.

  ```bash
  $ rm -rf ~/.incubacloud/git-trees
  ```
