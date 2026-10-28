# Agents

An AI agent — Claude Code, Claude, Claude Desktop, Cursor, VS Code, Codex —
can work on your panel for you: look at projects and instances, read logs,
deploy, back up, clone a staging, change settings. It connects to the
panel's **MCP address** and acts **as you**: with your role and your
projects' access, never more.

With a developer's role, an agent also works on your project's GitHub
repository: it reads it, creates branches, commits and opens pull requests;
writing to a branch that existed before waits for your approval. The panel has
an assistant of its own too, inside the panel: see [Assistant](../assistant/index.md).

What an agent does **not** get:

- **Secrets.** Passwords, keys and tokens never reach it, and it cannot set
  them: you set them in the panel. See [Secrets](secrets.md).
- **A free hand with what cannot be undone.** Deleting, restoring, moving,
  trusting a server's key, signing in to an instance, downloading a backup,
  running a command on production or on a server, and changing security
  settings wait for **your approval**. See [Approvals](confirmations.md).
- **Uploads and interactive terminals.** Uploading a backup to restore and
  the live terminals stay in the panel; agents run one command at a time.

Everything an agent changes is in the audit log under your name, followed
by *via agent* and which one.

- [Connect an agent](connect.md)
- [Approvals](confirmations.md)
- [Secrets](secrets.md)
- [Revoke an agent](revoke.md)
- [Limits](limits.md)
- [Troubleshooting](troubleshooting.md)
