# Limits

| What | Limit |
|---|---|
| Calls of all your agents together | 120 per minute by default (managers change it in **Settings**, **Rates**) |
| One answer | 64 KB; longer logs, lists, mail and command output come in pages, and the agent asks for the next one |
| A command (`run_command`, `run_host_command`) | 60 seconds; what it leaves running in the background is not stopped. The terminal's rate limits apply |
| An approval | 10 minutes, used once |
| A backup download link | 15 minutes, used once |
| A sign-in link to an instance | 60 seconds, used once |
| An agent's access (IncubaCloud SaaS) | Renewed by the client every few minutes while you do not revoke it; cut off after 30 days unused |

Long operations — deploy, rebuild, backups, clones, restores, moves —
answer at once with a job, which the agent follows until it ends.
