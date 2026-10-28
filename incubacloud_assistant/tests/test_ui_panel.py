"""The button and the side panel, opened in a browser by each kind of user."""

from odoo.tests import HttpCase, tagged

from odoo.addons.queue_job.tests.common import trap_jobs

from .common import AssistantMixin

#: Checks run in the page; any failure is a console error. ``$`` finds one
#: element, ``withText`` the first of a selector holding a text.
_CHECK = """
(async () => {{
    const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
    const until = async (what, find) => {{
        for (let waited = 0; waited < 10000; waited += 50) {{
            const found = find();
            if (found) {{ return found; }}
            await sleep(50);
        }}
        throw new Error("{page}: no " + what);
    }};
    const $ = (selector) => document.querySelector(selector);
    const withText = (selector, text) => [...document.querySelectorAll(selector)]
        .find((element) => element.textContent.includes(text));
    const click = (element) => element.dispatchEvent(
        new MouseEvent("click", {{ bubbles: true }})
    );
    {body}
    console.log("test successful");
}})().catch((error) => console.error(error.message));
"""
#: The panel is drawn: its header, and no loader.
_READY = (
    "!!document.querySelector('.ic-header') && !document.querySelector('.cloud-loader')"
)


@tagged("post_install", "-at_install")
class TestAssistantPanel(AssistantMixin, HttpCase):
    """What a user sees of the assistant in the panel."""

    def setUp(self):
        """One user allowed, one not."""
        super().setUp()
        self.allowed = self._cloud_user("asis-ui-ok")
        self.denied = self._cloud_user("asis-ui-no")
        self._configure(users=[self.allowed])

    def _open(self, login, body):
        """Open the panel's projects as *login* and run the checks of *body*."""
        self.browser_js(
            "/cloud/projects",
            _CHECK.format(page="/cloud/projects", body=body),
            ready=_READY,
            login=login,
            timeout=120,
        )

    def _read_notice(self):
        """The allowed user read the first-use notice already."""
        self.allowed.sudo().write({"assistant_notice_at": "2026-10-28 00:00:00"})

    def test_the_button_shows_only_to_a_user_chosen(self):
        """The chosen user has it in the top bar; another user has nothing."""
        self._open(
            self.allowed.login,
            """
            await until("the button", () => $(".ic-asst-btn"));
        """,
        )
        self._open(
            self.denied.login,
            """
            await sleep(1500);
            if ($(".ic-asst-btn")) {
                throw new Error("the button shows to a user not chosen");
            }
        """,
        )

    def test_focus_goes_into_the_panel_and_back_and_esc_closes_it_from_inside(self):
        """Opened, the focus is in the panel; Esc elsewhere on the page leaves it."""
        self._read_notice()
        self._open(
            self.allowed.login,
            """
            const button = await until("the button", () => $(".ic-asst-btn"));
            click(button);
            const panel = await until("the panel", () => $(".ic-asst-panel"));
            await until("the focus in the panel", () => panel.contains(
                document.activeElement));
            const esc = () => new KeyboardEvent("keydown", {
                key: "Escape", bubbles: true, cancelable: true,
            });
            document.body.dispatchEvent(esc());
            await sleep(300);
            if (!$(".ic-asst-panel")) {
                throw new Error("Esc outside the panel closed it");
            }
            document.activeElement.dispatchEvent(esc());
            await until("the panel to close", () => !$(".ic-asst-panel"));
            await until("the focus back on the button", () =>
                document.activeElement === $(".ic-asst-btn"));
        """,
        )

    def test_switched_off_the_conversations_are_kept_to_read(self):
        """The button stays for what the person has: no new one, nothing to write."""
        self._read_notice()
        Conversation = self.env["incubacloud.assistant.conversation"]
        conversation = Conversation.with_user(self.allowed)._start()
        conversation.sudo().name = "Kept"
        self._configure(users=[self.allowed], enabled=False)
        self._open(
            self.allowed.login,
            """
            click(await until("the button", () => $(".ic-asst-btn")));
            click(await until("the conversation", () => withText(
                ".ic-asst-conv", "Kept"
            )));
            // The chat's own note: the list has another.
            await until("the chat's note", () => $(".ic-asst-off-chat"));
            if ($(".ic-asst-input") || $(".ic-asst-new")) {
                throw new Error("the person can write while the assistant is off");
            }
        """,
        )

    def test_first_use_notice_then_a_message_starts_working(self):
        """The notice names the provider; accepted, a message shows and it works."""
        with trap_jobs():
            self._open(
                self.allowed.login,
                """
                click(await until("the button", () => $(".ic-asst-btn")));
                const notice = await until("the notice", () => $(".ic-asst-notice"));
                if (!notice.textContent.includes("Anthropic")) {
                    throw new Error("the notice does not name the provider");
                }
                if (!notice.textContent.includes("Before you use the assistant")) {
                    throw new Error("the notice has no title");
                }
                click(notice.querySelector("button"));
                click(await until("new conversation", () => $(".ic-asst-new")));
                const input = await until("the input", () => $(".ic-asst-input"));
                const label = await until(
                    "the fixed label", () => $(".ic-asst-ai-label")
                );
                const said = label.textContent;
                if (!said.includes("You are chatting with an AI, not a person.")
                    || !said.includes("sent to Anthropic.")) {
                    throw new Error("the fixed label is not the approved one");
                }
                input.value = "What is a staging?";
                input.dispatchEvent(new Event("input", { bubbles: true }));
                click($(".ic-asst-send"));
                await until("the message", () => withText(
                    ".ic-asst-msg--user", "What is a staging?"
                ));
                await until("the working state", () => withText(
                    ".ic-asst-state", "Working"
                ));
            """,
            )
        conversation = (
            self.env["incubacloud.assistant.conversation"]
            .sudo()
            .search([("user_id", "=", self.allowed.id)])
        )
        self.assertEqual(conversation.state, "working")
        self.assertTrue(self.allowed.sudo().assistant_notice_at)

    def test_a_card_is_cancelled_from_the_chat(self):
        """The card says what happens, on production; Cancel withdraws it."""
        self._read_notice()
        Conversation = self.env["incubacloud.assistant.conversation"]
        conversation = Conversation.with_user(self.allowed)._start()
        approval, _token = (
            self.env["incubacloud.mcp.approval"]
            .with_user(self.allowed)
            ._open(
                "deploy_instance",
                "h",
                'This acts on the production instance "x".',
                "assistant",
                f"assistant:{conversation.id}",
            )
        )
        conversation._add_message(
            {
                "role": "card",
                "content": approval.message,
                "approval_id": approval.id,
                "card_state": "pending",
                "tool_call_id": "c1",
                "tool_name": "deploy_instance",
                "production": True,
            }
        )
        conversation.sudo().write({"name": "Deploy", "state": "waiting_confirmation"})
        with trap_jobs():
            self._open(
                self.allowed.login,
                """
                click(await until("the button", () => $(".ic-asst-btn")));
                click(await until("the conversation", () => withText(
                    ".ic-asst-conv", "Deploy"
                )));
                const card = await until("the card", () => $(".ic-asst-card"));
                if (!card.querySelector(".ic-asst-card-prod")) {
                    throw new Error("no production warning on the card");
                }
                click(card.querySelector(".ic-asst-cancel"));
                await until(
                    "the card closed", () => !$(".ic-asst-card .ic-asst-cancel")
                );
            """,
            )
        self.assertEqual(approval.sudo().state, "cancelled")
