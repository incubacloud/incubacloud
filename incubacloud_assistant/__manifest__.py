{
    "name": "IncubaCloud Assistant",
    "version": "1.0.0",
    "summary": (
        "An AI assistant inside the panel, with the customer's own key and the "
        "user's role"
    ),
    "description": """
An assistant in the panel that answers with the user documentation and the
public code of the panel, reads logs, operates the platform and writes Odoo
modules on a branch of the project's repository, tested on a staging.

It runs with the AI provider key a manager sets (Anthropic, OpenAI, Google
Gemini or OpenRouter), for the users the manager chooses, with each user's
own role: the tools are the MCP's, through the same routes and checks as
the panel. Every change but trying code on a staging, anything on
production, and every change after reading text others write wait for
the user's confirmation in the chat. Conversations are kept 30 days and
only their owner reads them; every action is in the audit log.
""",
    "category": "Tools",
    "website": "https://www.incubacloud.io",
    "author": "IncubaCloud",
    "license": "Other proprietary",
    "depends": ["incubacloud_mcp", "bus", "queue_job"],
    "data": [],
    "assets": {
        "incubacloud.assets_prod": [
            "incubacloud_assistant/static/src/**/*",
        ],
        # JS unit tests (hoot), beside core's and the MCP's, without the SPA
        # registration.
        "web.assets_unit_tests": [
            "incubacloud_assistant/static/src/**/*.js",
            ("remove", "incubacloud_assistant/static/src/assistant_init.js"),
            "incubacloud_assistant/static/tests/**/*.test.js",
        ],
    },
    "installable": True,
    "auto_install": False,
}
