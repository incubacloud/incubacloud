# IncubaCloud MCP – Continuous Recovery

Bridge between `incubacloud_mcp` and `incubacloud_pitr`, installed on its own when both
are. Adds three tools: `get_pitr` (state, objective and window), `configure_pitr`
(switch on or off, change the objective; managers) and `pitr_restore` (restore to a
moment; managers, with the user's approval — the approval replaces typing the instance's
name).
