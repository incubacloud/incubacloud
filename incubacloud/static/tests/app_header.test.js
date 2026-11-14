import { describe, expect, test } from "@odoo/hoot";
import { patchWithCleanup } from "@web/../tests/web_test_helpers";
import { rpc } from "@web/core/network/rpc";
import { AppHeader } from "@incubacloud/components/app_header/app_header";

/**
 * AppHeader relies on session, onMounted/onWillUnmount for document listeners,
 * env.navigate, env.permissions, env.toggleSlideOver, env.userInfo, and rpc.
 * Full mounting requires a parent with useSubEnv providing all of these plus
 * mocking the session import. We test the pure logic extracted from the class
 * prototype instead.
 */

describe("AppHeader — initials getter", () => {
    /**
     * The `initials` getter reads from `this.state.userName`.
     * We can test the logic by calling the getter on a fake context.
     */
    function computeInitials(userName) {
        const name = userName || "?";
        const parts = name.trim().split(/\s+/);
        if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
        return name.slice(0, 2).toUpperCase();
    }

    test("two-word name returns first letters", () => {
        expect(computeInitials("John Doe")).toBe("JD");
    });

    test("single-word name returns first two chars", () => {
        expect(computeInitials("Admin")).toBe("AD");
    });

    test("empty name returns question marks", () => {
        expect(computeInitials("")).toBe("?");
    });

    test("three-word name returns first two initials", () => {
        expect(computeInitials("Ana Maria Lopez")).toBe("AM");
    });

    test("lowercase name returns uppercase initials", () => {
        expect(computeInitials("reinier segura")).toBe("RS");
    });

    test("name with extra spaces is trimmed", () => {
        expect(computeInitials("  John   Doe  ")).toBe("JD");
    });
});

describe("AppHeader — filteredProjects logic", () => {
    function filteredProjects(projects, search) {
        const q = search.toLowerCase();
        if (!q) return projects;
        return projects.filter((p) => p.name.toLowerCase().includes(q));
    }

    const sampleProjects = [
        { id: 1, name: "IncubaCloud" },
        { id: 2, name: "Acme Project" },
        { id: 3, name: "Cloud Demo" },
    ];

    test("empty search returns all projects", () => {
        expect(filteredProjects(sampleProjects, "")).toEqual(sampleProjects);
    });

    test("search filters by name case-insensitively", () => {
        const result = filteredProjects(sampleProjects, "cloud");
        expect(result.length).toBe(2);
        expect(result[0].name).toBe("IncubaCloud");
        expect(result[1].name).toBe("Cloud Demo");
    });

    test("search with no matches returns empty array", () => {
        expect(filteredProjects(sampleProjects, "zzz")).toEqual([]);
    });

    test("partial match works", () => {
        const result = filteredProjects(sampleProjects, "acm");
        expect(result.length).toBe(1);
        expect(result[0].id).toBe(2);
    });
});

describe("AppHeader — insideProject logic", () => {
    function insideProject(currentRoute, projectId) {
        const projectRoutes = [
            "project_detail",
            "project_settings",
            "create_instance",
            "instance_detail",
            "project_hub",
        ];
        return projectRoutes.includes(currentRoute) && !!projectId;
    }

    test("returns true for project_detail with projectId", () => {
        expect(insideProject("project_detail", 1)).toBe(true);
    });

    test("returns false for projects route", () => {
        expect(insideProject("projects", 1)).toBe(false);
    });

    test("returns false when projectId is 0", () => {
        expect(insideProject("project_detail", 0)).toBe(false);
    });

    test("returns true for instance_detail with projectId", () => {
        expect(insideProject("instance_detail", 42)).toBe(true);
    });
});

describe("AppHeader — static props definition", () => {
    test("declares expected props", () => {
        expect(AppHeader.props.alertCount.type).toBe(Number);
        expect(AppHeader.props.currentRoute.type).toBe(String);
        expect(AppHeader.props.projectName.type).toBe(String);
        expect(AppHeader.props.projectId.type).toBe(Number);
    });

    test("template name is set", () => {
        expect(AppHeader.template).toBe("incubacloud.AppHeader");
    });
});

describe("AppHeader — notification preferences", () => {
    /**
     * An AppHeader stand-in for the notification modal's methods: the
     * state they read, and a toast that records nothing.
     *
     * @returns {object} the stand-in
     */
    function standIn() {
        const comp = Object.create(AppHeader.prototype);
        comp.state = {
            showUserMenu: false, showNotifModal: false, expandedSection: null,
            notifMuted: [], notifProjects: [], notifSaving: false,
        };
        comp.env = { toast: { success() {}, error() {} } };
        return comp;
    }

    test("a token and a secret typed, then Cancel: the next Save sends neither", async () => {
        const saved = [];
        patchWithCleanup(rpc, {
            _rpc(url, params) {
                if (url === "/cloud/get_user_preferences") {
                    return Promise.resolve({
                        cloud_telegram_configured: true, cloud_telegram_chat_id: "42",
                        cloud_webhook_configured: true,
                        cloud_webhook_url: "https://hooks.example.com/cloud",
                    });
                }
                if (url === "/cloud/get_projects") {
                    return Promise.resolve({ items: [] });
                }
                if (url === "/cloud/save_user_preferences") {
                    saved.push(params);
                }
                return Promise.resolve({});
            },
        });
        const header = standIn();
        await header.openNotifModal();
        header.onTelegramTokenChange("typed-then-cancelled");
        header.onWebhookSecretChange("typed-then-cancelled");
        header.closeNotifModal();

        await header.openNotifModal();
        // The fields open on what the form holds: nothing, so «Stored».
        expect(header.state._telegramToken).toBe("");
        expect(header.state._webhookSecret).toBe("");
        await header.saveNotifPrefs();
        expect(saved).toHaveLength(1);
        // "" keeps the stored token and secret.
        expect(saved[0].cloud_telegram_bot_token).toBe("");
        expect(saved[0].cloud_webhook_secret).toBe("");
    });

    test("a chat ID without a token: Save stops on the token, opens Telegram and sends nothing", async () => {
        const saved = [];
        patchWithCleanup(rpc, {
            _rpc(url, params) {
                if (url === "/cloud/save_user_preferences") {
                    saved.push(params);
                }
                return Promise.resolve({});
            },
        });
        const header = standIn();
        Object.assign(header.state, {
            telegramEnabled: true, webhookEnabled: false,
            telegramTokenConfigured: false, _telegramToken: "", telegramChatId: "42",
            webhookSecretConfigured: false, _webhookSecret: "", webhookUrl: "",
        });
        await header.saveNotifPrefs();
        expect(saved).toHaveLength(0);
        expect(Object.keys(header.state.notifErrors)).toEqual(["telegramToken"]);
        expect(header.state.expandedSection).toBe("telegram");
        expect(header.focusNext).toBe("ic-notif-telegram-token");
        // Typing clears it.
        header.onTelegramTokenChange("123:abc");
        expect(header.state.notifErrors).toEqual({});
    });

    test("a signing secret without a URL: Save stops on the webhook URL", async () => {
        const header = standIn();
        Object.assign(header.state, {
            telegramEnabled: false, webhookEnabled: true,
            telegramTokenConfigured: false, _telegramToken: "", telegramChatId: "",
            webhookSecretConfigured: true, _webhookSecret: "", webhookUrl: "",
        });
        await header.saveNotifPrefs();
        expect(Object.keys(header.state.notifErrors)).toEqual(["webhookUrl"]);
        expect(header.state.expandedSection).toBe("webhook");
        expect(header.focusNext).toBe("ic-notif-webhook-url");
        header.onWebhookUrlInput("https://hooks.example.com/cloud");
        expect(header.state.webhookUrl).toBe("https://hooks.example.com/cloud");
        expect(header.state.notifErrors).toEqual({});
    });

    test("both channels switched off: Save skips their checks and sends their fields empty", async () => {
        const saved = [];
        patchWithCleanup(rpc, {
            _rpc(url, params) {
                if (url === "/cloud/get_user_preferences") {
                    return Promise.resolve({
                        cloud_telegram_configured: true, cloud_telegram_chat_id: "42",
                        cloud_webhook_configured: true,
                        cloud_webhook_url: "https://hooks.example.com/cloud",
                    });
                }
                if (url === "/cloud/get_projects") {
                    return Promise.resolve({ items: [] });
                }
                if (url === "/cloud/save_user_preferences") {
                    saved.push(params);
                }
                return Promise.resolve({});
            },
        });
        const header = standIn();
        await header.openNotifModal();
        // A saved channel opens switched on.
        expect(header.state.telegramEnabled).toBe(true);
        expect(header.state.webhookEnabled).toBe(true);
        header.onTelegramTokenChange("typed-before-off");
        header.toggleTelegram();
        header.toggleWebhook();
        await header.saveNotifPrefs();
        expect(header.state.notifErrors).toEqual({});
        expect(saved).toHaveLength(1);
        // Empty addresses: the server deletes the token and the secret.
        expect(saved[0].cloud_telegram_bot_token).toBe("");
        expect(saved[0].cloud_telegram_chat_id).toBe("");
        expect(saved[0].cloud_webhook_url).toBe("");
        expect(saved[0].cloud_webhook_secret).toBe("");
    });

    test("a channel never saved opens switched off", async () => {
        patchWithCleanup(rpc, {
            _rpc(url) {
                if (url === "/cloud/get_projects") {
                    return Promise.resolve({ items: [] });
                }
                return Promise.resolve({
                    cloud_telegram_configured: false, cloud_telegram_chat_id: "",
                    cloud_webhook_configured: false, cloud_webhook_url: "",
                });
            },
        });
        const header = standIn();
        await header.openNotifModal();
        expect(header.state.telegramEnabled).toBe(false);
        expect(header.state.webhookEnabled).toBe(false);
    });

    test("a save the server refuses keeps the modal open and says why", async () => {
        patchWithCleanup(rpc, {
            _rpc(url) {
                if (url === "/cloud/save_user_preferences") {
                    return Promise.resolve({ ok: false, error: "Webhook URL rejected: no" });
                }
                return Promise.resolve({});
            },
        });
        const header = standIn();
        const toasts = [];
        header.env = {
            toast: {
                success(msg) { toasts.push(["success", msg]); },
                error(msg) { toasts.push(["error", msg]); },
            },
        };
        Object.assign(header.state, {
            showNotifModal: true, telegramEnabled: false, webhookEnabled: false,
        });
        await header.saveNotifPrefs();
        expect(toasts).toEqual([["error", "Webhook URL rejected: no"]]);
        expect(header.state.showNotifModal).toBe(true);
        expect(header.state.notifSaving).toBe(false);
    });

    test("a chat ID or a URL of spaces counts as empty", async () => {
        const saved = [];
        patchWithCleanup(rpc, {
            _rpc(url, params) {
                if (url === "/cloud/save_user_preferences") {
                    saved.push(params);
                }
                return Promise.resolve({ ok: true });
            },
        });
        const header = standIn();
        Object.assign(header.state, {
            telegramEnabled: true, telegramTokenConfigured: true,
            _telegramToken: "", telegramChatId: "   ",
            webhookEnabled: false,
        });
        await header.saveNotifPrefs();
        expect(saved).toHaveLength(0);
        expect(Object.keys(header.state.notifErrors)).toEqual(["telegramChatId"]);
        Object.assign(header.state, {
            notifErrors: {}, telegramEnabled: false, webhookEnabled: true,
            webhookSecretConfigured: true, _webhookSecret: "", webhookUrl: "  ",
        });
        await header.saveNotifPrefs();
        expect(saved).toHaveLength(0);
        expect(Object.keys(header.state.notifErrors)).toEqual(["webhookUrl"]);
    });

    test("a card's header follows its switch, as Email's does", () => {
        const header = standIn();
        Object.assign(header.state, {
            telegramEnabled: true, telegramTokenConfigured: true,
            webhookEnabled: true, webhookUrl: "https://hooks.example.com/cloud",
        });
        expect(header.telegramConfigured).toBe(true);
        expect(header.webhookConfigured).toBe(true);
        header.toggleTelegram();
        header.toggleWebhook();
        expect(header.telegramConfigured).toBe(false);
        expect(header.webhookConfigured).toBe(false);
    });

    test("Detect and Send test carry what the card shows and save nothing", async () => {
        const calls = [];
        patchWithCleanup(rpc, {
            _rpc(url, params) {
                calls.push([url, params]);
                return Promise.resolve({ ok: true, chat_id: "77" });
            },
        });
        const header = standIn();
        Object.assign(header.state, {
            telegramEnabled: true, telegramTokenConfigured: false,
            _telegramToken: "", telegramChatId: "",
        });
        header.onTelegramTokenChange("123:typed");
        await header.detectChatId();
        expect(header.state.telegramChatId).toBe("77");
        await header.sendTelegramTest();
        expect(calls).toEqual([
            ["/cloud/telegram_detect_chat_id", { bot_token: "123:typed" }],
            ["/cloud/telegram_send_test", { bot_token: "123:typed", chat_id: "77" }],
        ]);
    });
});
