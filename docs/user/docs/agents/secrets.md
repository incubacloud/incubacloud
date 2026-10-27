# Secrets

Passwords, keys and tokens — servers' passwords and SSH keys, databases'
passwords, backup destinations' keys, the GitHub App's key, notification
tokens — **never go to an agent and never come from one**.

- Where the panel shows a secret as *set* or *not set*, the agent sees the
  same: whether it is set, never its value.
- An agent that tries to set one is refused: *Secrets are changed in the
  panel*, naming the argument that carried it. A server an agent adds has
  no SSH credentials until you set them in the panel; a backup destination
  needs its key set in the panel.
- Whatever an agent reads — logs, command output, job logs, alerts,
  captured mail — has every secret the panel keeps replaced by `[secreto]`,
  as are a password or a token written into a web address. In captured
  mail, links that sign someone in or reset a password are replaced too.

!!! note "A safety net, not a guarantee"
    An agent allowed to run commands in an instance can read that
    instance's secrets, exactly as the terminal can, and could change them
    before showing them. Give that role to agents you trust.
