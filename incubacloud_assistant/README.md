# IncubaCloud Assistant (`incubacloud_assistant`)

An AI assistant inside the panel: a button in the top bar opens the chat in a
side panel; Settings → Assistant (managers) sets the provider, the key, the
models, who may use it and the limits.

- **Your key, your provider.** Anthropic, or OpenAI, Google Gemini and OpenRouter
  (beta), at fixed addresses. The key is encrypted and never goes back to the
  browser.
- **The user's role, never more.** The tools are the MCP's (`incubacloud_mcp`),
  through the panel's own routes and checks, plus searching the user
  documentation and the panel's public code.
- **Confirmation in the chat** for every change but trying code on a staging, for
  anything on production, and for every change after reading logs, files or mail
  others write. Code tools work only on repositories of the user's projects.
- **Code** on a branch of the project's repository, tested on a staging, with a
  pull request; production is always a separate step the user asks for.
- **Private**: each conversation is its owner's only, kept 30 days; actions are
  in the audit log, *via agent* `Assistant (conversation N)`.
- **Spend under control**: cost shown per conversation and per month, an optional
  monthly limit, the same for every user (models without a known price cannot be used
  under it), and a cap of steps per task.

See `docs/assistant.md` (technical) and `docs/user/docs/assistant/` (users).
