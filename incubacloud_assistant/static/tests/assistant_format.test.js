import { beforeEach, describe, expect, test } from "@odoo/hoot";
import { allowTranslations } from "@web/../tests/web_test_helpers";
import {
    LINK_HOSTS,
    buttonView,
    cardStateLabel,
    chatLabel,
    linkify,
    mergeEvent,
    messageView,
    modelOptions,
    noticeText,
    noticeTitle,
    providerWording,
    roleLabel,
    screenContext,
    settingsChanges,
    stateView,
    usageLabel,
} from "@incubacloud_assistant/assistant_format";

/**
 * assistant_format — what the assistant's button, panel and settings show.
 * Every decision of the interface is here, so it is checked without a panel.
 */
describe("assistant_format", () => {
    // Its labels are translated strings: read without a loaded catalogue
    // they throw, as they would before the web client loaded its own.
    beforeEach(() => allowTranslations());

    describe("the button", () => {
        test("hidden when the assistant is not available and nothing is kept", () => {
            const hidden = { visible: false, readOnly: false, badge: 0 };
            expect(buttonView({ available: false, conversations: 0 })).toEqual(hidden);
            expect(buttonView(null)).toEqual(hidden);
        });
        test("visible, with the unread count when something happened", () => {
            expect(buttonView({ available: true, unread: 0 })).toEqual({
                visible: true, readOnly: false, badge: 0,
            });
            expect(buttonView({ available: true, unread: 2 }).badge).toBe(2);
        });
        test("switched off with conversations kept: visible, to read them", () => {
            expect(buttonView({ available: false, conversations: 3 })).toEqual({
                visible: true, readOnly: true, badge: 0,
            });
        });
    });

    describe("the month's usage", () => {
        test("spent of the limit", () => {
            const label = usageLabel({ cost: 12.4, limit: 30, unpriced: false }, "en-US");
            expect(label).toInclude("€12.40");
            expect(label).toInclude("€30.00");
        });
        test("without a limit, what was spent", () => {
            expect(usageLabel({ cost: 1, limit: 0, unpriced: false }, "en-US")).not.toInclude("of");
        });
        test("unknown costs are said", () => {
            expect(usageLabel({ cost: 1, limit: 0, unpriced: true }, "en-US")).toInclude("unknown");
        });
    });

    describe("the models", () => {
        test("recommended or untested", () => {
            const options = modelOptions([
                { id: "claude-opus-5-5", name: "Claude Opus 5.5", recommended: true },
                { id: "gpt-x", name: "", recommended: false },
            ]);
            expect(options.map((o) => [o.id, o.label, o.tag])).toEqual([
                ["claude-opus-5-5", "Claude Opus 5.5", "Recommended"],
                ["gpt-x", "gpt-x", "Untested"],
            ]);
            expect(options.some((o) => o.disabled)).toBe(false);
        });
        test("under a monthly limit, a model without a price is shown disabled", () => {
            const [option] = modelOptions([
                { id: "gpt-y", name: "GPT Y", recommended: false, usable: false },
            ]);
            expect([option.disabled, option.tag]).toEqual([true, "Price unknown"]);
        });
    });

    describe("the conversation's state", () => {
        test("stop while it works or waits, retry after an error or a stop", () => {
            expect(stateView("working")).toEqual({
                label: "Working…", canStop: true, canRetry: false, busy: true,
            });
            expect(stateView("waiting_confirmation").canStop).toBe(true);
            expect(stateView("error").canRetry).toBe(true);
            expect(stateView("idle")).toEqual({ label: "", canStop: false, canRetry: false, busy: false });
        });
    });

    describe("the messages", () => {
        test("each role as the chat shows it", () => {
            expect(messageView({ role: "user", text: "hi", pending: true })).toEqual({
                kind: "user", text: "hi", queued: true,
            });
            expect(messageView({ role: "assistant", text: "", cost: 0.1 })).toBe(null);
            expect(messageView({ role: "tool", label: "Reading the logs", is_error: true })).toEqual({
                kind: "step", label: "Reading the logs", error: true,
            });
            expect(messageView({ role: "notice", text: "Shall I go on?", ask_continue: true }).askContinue)
                .toBe(true);
            const card = messageView({
                id: 9, role: "card", text: "Deploy.", card: { state: "pending", production: true },
            });
            expect([card.kind, card.open, card.production]).toEqual(["card", true, true]);
        });
        test("links are clickable, without the dot after them", () => {
            expect(linkify("See https://docs.incubacloud.io/backups/. Then go.")).toEqual([
                { text: "See " },
                { text: "https://docs.incubacloud.io/backups/", url: "https://docs.incubacloud.io/backups/" },
                { text: ". Then go." },
            ]);
        });
        test("only known hosts are links: any other address stays text", () => {
            const hosts = [...LINK_HOSTS, "apps.acme.test"];
            const parts = linkify(
                "Open https://apps.acme.test/web or https://evil.example/x?d=1",
                hosts
            );
            expect(parts.filter((part) => part.url).map((part) => part.url)).toEqual([
                "https://apps.acme.test/web",
            ]);
            expect(parts.map((part) => part.text).join("")).toInclude("https://evil.example/x?d=1");
        });
        test("each host by its own name: a subdomain of one is no link", () => {
            const hosts = [...LINK_HOSTS, "apps.acme.test"];
            const parts = linkify(
                "See https://x.apps.acme.test, https://gist.github.com/a and https://github.com/b",
                hosts
            );
            expect(parts.filter((part) => part.url).map((part) => part.url)).toEqual([
                "https://github.com/b",
            ]);
        });
    });

    describe("live events", () => {
        const view = { conversationId: 4, messages: [{ id: 1, role: "user" }], progress: "", state: "working" };
        test("progress of this conversation", () => {
            expect(mergeEvent(view, { conversation_id: 4, kind: "progress", label: "Deploying…" }).progress)
                .toBe("Deploying…");
        });
        test("a message is added once, an update replaces it", () => {
            const added = mergeEvent(view, { conversation_id: 4, kind: "message", message: { id: 2, role: "tool" } });
            expect(added.messages.map((m) => m.id)).toEqual([1, 2]);
            const again = mergeEvent(added, { conversation_id: 4, kind: "message", message: { id: 2, role: "tool", x: 1 } });
            expect(again.messages.length).toBe(2);
            expect(again.messages[1].x).toBe(1);
        });
        test("an idle state clears the progress; another conversation changes nothing", () => {
            const busy = { ...view, progress: "Thinking…" };
            expect(mergeEvent(busy, { conversation_id: 4, kind: "state", state: "idle" }).progress).toBe("");
            expect(mergeEvent(busy, { conversation_id: 5, kind: "state", state: "idle" })).toBe(busy);
        });
    });

    describe("what the panel sends", () => {
        test("the screen the person has open, ids only", () => {
            expect(
                screenContext({ route: "instance_detail", params: { project_id: 3, instance_id: 8, x: "y" } })
            ).toEqual({ route: "instance_detail", project_id: 3, instance_id: 8 });
        });
        test("the first-use notice is the approved text and names the provider", () => {
            expect(noticeTitle()).toBe("Before you use the assistant");
            const text = noticeText("Anthropic");
            expect(text).toInclude("The assistant is an AI, not a person");
            expect(text).toInclude("is sent to Anthropic, the provider of your panel's AI key");
            expect(text).toInclude("each one is deleted 30 days after its last activity.");
        });
        test("the fixed label next to the message field is the approved text", () => {
            expect(chatLabel("Google Gemini")).toBe(
                "You are chatting with an AI, not a person. It can make mistakes. " +
                    "Your messages and what it reads are sent to Google Gemini."
            );
        });
        test("with OpenRouter the notices also name the company behind the model", () => {
            expect(providerWording({ provider: "anthropic", provider_label: "Anthropic" })).toBe(
                "Anthropic"
            );
            expect(providerWording({ provider: "openrouter", provider_label: "OpenRouter" })).toBe(
                "OpenRouter and the company behind the selected model"
            );
            expect(providerWording(undefined)).toBe("");
        });
        test("settings: only what changed, the key only when typed", () => {
            const loaded = {
                enabled: false, provider: "anthropic", default_model: "a", monthly_limit: 0,
                step_cap: 30, allowed_models: ["a", "b"], user_ids: [1, 2], api_key: "",
                clear_api_key: "",
            };
            expect(settingsChanges(loaded, { ...loaded, allowed_models: ["b", "a"] })).toEqual({});
            expect(settingsChanges(loaded, { ...loaded, enabled: true, api_key: "sk-1" })).toEqual({
                enabled: true, api_key: "sk-1",
            });
            expect(settingsChanges(loaded, { ...loaded, user_ids: [2] })).toEqual({ user_ids: [2] });
            expect(
                settingsChanges(loaded, { ...loaded, clear_api_key: true, api_key: "sk-2" })
            ).toEqual({ api_key: "sk-2" });
        });
        test("roles by their panel name", () => {
            expect(roleLabel("group_cloud_developer")).toBe("Developer");
            expect(roleLabel("nope")).toBe("");
        });
        test("a closed card says how it closed", () => {
            expect(cardStateLabel("expired")).toBe("Expired");
            expect(cardStateLabel("pending")).toBe("");
        });
    });
});
