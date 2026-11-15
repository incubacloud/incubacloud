import { Component, useEffect, useState, onMounted, onWillUnmount } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { session } from "@web/session";
import { _t } from "@web/core/l10n/translation";
import { settingsIndex } from "../settings/settings_registry";
import { searchSettings } from "../settings/settings_search";
import { IcModal } from "../ic_modal/ic_modal";
import { RlSelect } from "../rl_select/rl_select";
import { SearchSelect } from "../search_select/search_select";
import { PasswordInput } from "../password_input/password_input";
import {
    firstStepsStore,
    loadFirstSteps,
    setFirstStepsHidden,
} from "../first_steps/first_steps";

// The control of each field a notification Save can stop on.
const NOTIF_FIELD_IDS = {
    telegramToken: "ic-notif-telegram-token",
    telegramChatId: "ic-notif-telegram-chat",
    webhookUrl: "ic-notif-webhook-url",
};

export class AppHeader extends Component {
    static template = "incubacloud.AppHeader";
    static components = { IcModal, RlSelect, SearchSelect, PasswordInput };
    static props = {
        alertCount:   { type: Number },
        currentRoute: { type: String },
        projectName:  { type: String },
        projectId:    { type: Number },
        // Passed down (rather than read off env) so the toggle's
        // aria-expanded re-renders with the menu.
        sidebarOpen:  { type: Boolean },
    };

    setup() {
        this.firstSteps = useState(firstStepsStore);
        this.state = useState({
            userName: session.name || session.username || "",
            userLogin: session.username || "",
            avatarUrl: session.uid ? `/web/image/res.users/${session.uid}/avatar_128` : '',
            avatarLoaded: false,
            showUserMenu: false,
            theme: document.documentElement.getAttribute("data-ic-theme") || "light",
            showProjectSwitcher: false,
            projects: [],
            projectSearch: "",
            // Global search (header, Ctrl+K)
            globalSearch: "",
            searchResults: [],
            showSearch: false,
            searchLoading: false,
            // Notification preferences modal
            showNotifModal: false,
            expandedSection: null,  // 'email' | 'telegram' | 'webhook' | null
            notifLevel: "failures",
            notifMode: "immediate",
            emailEnabled: true,
            notifMuted: [],        // [{id, name}] muted projects
            notifProjects: [],     // [{id, name}] pickable projects
            notifSaving: false,
            // Telegram
            // The card's switch: on while the channel is saved; off on
            // Save deletes the token and the chat ID.
            telegramEnabled: false,
            telegramTokenConfigured: false,
            _telegramToken: "",    // a new bot token typed, "" keeps the stored one
            telegramChatId: "",
            telegramDetecting: false,
            telegramTesting: false,
            // Webhook
            // The card's switch: on while the channel is saved; off on
            // Save deletes the URL and the signing secret.
            webhookEnabled: false,
            webhookSecretConfigured: false,
            _webhookSecret: "",    // a new signing secret typed, "" keeps the stored one
            webhookUrl: "",
            // The message of the field Save stopped on, by field:
            // telegramToken, telegramChatId or webhookUrl.
            notifErrors: {},
        });
        // The id of the field Save stopped on, focused on the next render.
        this.focusNext = null;
        useEffect(() => this.restoreFocus());

        this._closeMenus = (ev) => {
            if (!ev.target.closest(".ic-header-user-wrap")) {
                this.state.showUserMenu = false;
            }
            if (!ev.target.closest(".ic-header-switcher-wrap")) {
                this.state.showProjectSwitcher = false;
            }
            if (!ev.target.closest(".ic-header-search-wrap")) {
                this.state.showSearch = false;
            }
        };

        // Ctrl+K focuses the global search box. Registered in the
        // capture phase with stopImmediatePropagation so Odoo's own
        // command-palette hotkey never fires inside the SPA.
        this._onKeydown = (ev) => {
            if ((ev.metaKey || ev.ctrlKey) && (ev.key === "k" || ev.key === "K")) {
                ev.preventDefault();
                ev.stopImmediatePropagation();
                const el = document.querySelector(".ic-header-search-input");
                if (el) { el.focus(); el.select(); }
            }
        };

        onMounted(() => {
            document.addEventListener("click", this._closeMenus);
            document.addEventListener("keydown", this._onKeydown, true);
            // Override from env.userInfo once config loads
            const info = this.env.userInfo;
            if (info?.name) this.state.userName = info.name;
            if (info?.login) this.state.userLogin = info.login;
            if (info?.avatarUrl) this.state.avatarUrl = info.avatarUrl;
        });
        onWillUnmount(() => {
            document.removeEventListener("click", this._closeMenus);
            document.removeEventListener("keydown", this._onKeydown, true);
        });
    }

    // ── Notification preference dots ────────────────────────────────────

    get emailConfigured() {
        return this.state.emailEnabled;
    }

    // Telegram's and the webhook's follow their switch, as Email's does.

    get telegramConfigured() {
        return this.state.telegramEnabled && this.state.telegramTokenConfigured;
    }

    get webhookConfigured() {
        return this.state.webhookEnabled && !!this.state.webhookUrl;
    }

    /**
     * Options of the "What to receive" selector, from most to least.
     * Built here rather than inline in the template so the labels go
     * through _t(): strings inside a template expression are never
     * translated.
     */
    get notifLevelOptions() {
        return [
            { value: "all", label: _t("All (every job outcome and every alert)") },
            {
                value: "failures_warnings",
                label: _t("Failures and warnings (failed jobs and every alert)"),
            },
            { value: "failures", label: _t("Failures only (failed jobs and critical alerts)") },
            { value: "none", label: _t("None") },
        ];
    }

    // ── UI helpers ───────────────────────────────────────────────────────

    onGlobalSearch(ev) {
        const value = ev.target.value;
        this.state.globalSearch = value;
        clearTimeout(this._searchTimeout);
        if (!value || value.trim().length < 2) {
            this.state.searchResults = [];
            this.state.showSearch = false;
            return;
        }
        this.state.showSearch = true;
        this.state.searchLoading = true;
        this._searchTimeout = setTimeout(async () => {
            try {
                const data = await rpc("/cloud/global_search", { query: value });
                this.state.searchResults = data.results || [];
            } catch (_e) {
                this.state.searchResults = [];
            }
            this.state.searchLoading = false;
        }, 220);
    }

    /**
     * The settings that match the global search: the same index as the
     * box atop Settings, for whoever may open Settings.
     *
     * @returns {object[]} up to five fields, best first
     */
    get settingsResults() {
        if (!this.env.permissions?.can_manage_settings) {
            return [];
        }
        const { groups } = searchSettings(settingsIndex(this.env), this.state.globalSearch, {
            perSection: 2,
        });
        return groups.flatMap((group) => group.fields).slice(0, 5);
    }

    /**
     * Open a setting the global search found, on its section.
     *
     * @param {object} field the index entry
     */
    goToSetting(field) {
        this.state.showSearch = false;
        this.state.globalSearch = "";
        this.state.searchResults = [];
        this.env.navigate("settings", { section: field.section, field: field.id });
    }

    searchResultIcon(type) {
        return type === "project" ? "projects" : (type === "host" ? "hosts" : "instance");
    }

    goToSearchResult(r) {
        this.state.showSearch = false;
        this.state.globalSearch = "";
        this.state.searchResults = [];
        this.env.navigate(r.route, r.params || {});
    }

    onSearchFocus() {
        if (this.state.searchResults.length) {
            this.state.showSearch = true;
        }
    }

    get initials() {
        const name = this.state.userName || "?";
        const parts = name.trim().split(/\s+/);
        if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
        return name.slice(0, 2).toUpperCase();
    }

    get currentProjectName() {
        return this.props.projectName || "";
    }

    get insideProject() {
        const projectRoutes = [
            "project_detail", "project_settings", "create_instance",
            "instance_detail", "project_hub",
        ];
        return projectRoutes.includes(this.props.currentRoute) && !!this.props.projectId;
    }

    goHome() {
        this.env.navigate("projects");
    }

    goProjects() {
        this.env.navigate("projects");
    }

    toggleUserMenu(ev) {
        ev.stopPropagation();
        this.state.showUserMenu = !this.state.showUserMenu;
        this.state.showProjectSwitcher = false;
        // Off the home page nothing asked for the first steps yet.
        if (this.state.showUserMenu && !this.firstSteps.loaded) {
            loadFirstSteps();
        }
    }

    /** @returns {boolean} whether the menu offers the first steps: on, and something left */
    get firstStepsInMenu() {
        const data = this.firstSteps.data;
        return !!(data && data.enabled && data.pending);
    }

    /** Open the first steps card again and go to the home page, where it is. */
    async openFirstSteps() {
        this.state.showUserMenu = false;
        await setFirstStepsHidden(false);
        this.env.navigate("projects");
    }

    /** Switch between the light and dark UI theme and persist the choice. */
    toggleTheme() {
        const next = this.state.theme === "light" ? "dark" : "light";
        this.state.theme = next;
        document.documentElement.setAttribute("data-ic-theme", next);
        try { localStorage.setItem("ic-theme", next); } catch (e) { /* private mode */ }
        this.state.showUserMenu = false;
    }

    async toggleProjectSwitcher(ev) {
        ev.stopPropagation();
        this.state.showUserMenu = false;
        this.state.showProjectSwitcher = !this.state.showProjectSwitcher;
        if (this.state.showProjectSwitcher && !this.state.projects.length) {
            const data = await rpc("/cloud/get_projects", {});
            this.state.projects = data.items || [];
        }
        this.state.projectSearch = "";
    }

    get filteredProjects() {
        const q = this.state.projectSearch.toLowerCase();
        if (!q) return this.state.projects;
        return this.state.projects.filter(p => p.name.toLowerCase().includes(q));
    }

    onProjectSearch(ev) {
        const value = ev.target.value;
        clearTimeout(this._projectSearchTimeout);
        this._projectSearchTimeout = setTimeout(() => {
            this.state.projectSearch = value;
        }, 300);
    }

    selectProject(project) {
        this.state.showProjectSwitcher = false;
        if (project.first_production_instance_id) {
            this.env.navigate("instance_detail", {
                project_id: project.id,
                instance_id: project.first_production_instance_id,
            });
        } else {
            this.env.navigate("project_detail", { project_id: project.id });
        }
    }

    createProject() {
        this.state.showProjectSwitcher = false;
        this.env.navigate("new_project");
    }

    openJobs() {
        this.env.toggleSlideOver("jobs");
    }

    goToAlerts() {
        this.env.toggleSlideOver("alerts", this._currentInstanceId());
    }

    _currentInstanceId() {
        const projectRoutes = ["instance_detail"];
        if (projectRoutes.includes(this.props.currentRoute)) {
            const match = window.location.pathname.match(/\/instances\/(\d+)/);
            if (match) return parseInt(match[1]);
        }
        return false;
    }

    /** Open Sessions and access. */
    goToSessions() {
        this.state.showUserMenu = false;
        this.env.navigate("sessions");
    }

    onAvatarLoad() {
        this.state.avatarLoaded = true;
    }

    exitToErp() {
        this.state.showUserMenu = false;
        window.location.href = '/odoo';
    }

    // ── Notification preferences accordion ──────────────────────────────

    toggleSection(name) {
        this.state.expandedSection = (
            this.state.expandedSection === name ? null : name
        );
    }

    async _savePrefsSilent() {
        // Persist current form values without closing the modal, and
        // answer what the server answered. A
        // channel switched off goes with its address empty, which the
        // server takes as off: it deletes the channel's secret too.
        const telegram = this.state.telegramEnabled;
        const webhook = this.state.webhookEnabled;
        return await rpc("/cloud/save_user_preferences", {
            cloud_notification_level: this.state.notifLevel,
            cloud_notification_mode: this.state.notifMode,
            cloud_email_enabled: this.state.emailEnabled,
            cloud_muted_project_ids: this.state.notifMuted.map((p) => p.id),
            cloud_telegram_bot_token: (telegram && this.state._telegramToken) || "",
            cloud_telegram_chat_id: (telegram && this.state.telegramChatId) || "",
            cloud_webhook_url: (webhook && this.state.webhookUrl) || "",
            cloud_webhook_secret: (webhook && this.state._webhookSecret) || "",
        });
    }

    /**
     * Turn the Telegram card's switch; off, the card hides its fields and
     * Save deletes the stored token and chat ID.
     */
    toggleTelegram() {
        this.state.telegramEnabled = !this.state.telegramEnabled;
        this.state.notifErrors = {};
    }

    /**
     * Turn the webhook card's switch; off, the card hides its fields and
     * Save deletes the stored URL and signing secret.
     */
    toggleWebhook() {
        this.state.webhookEnabled = !this.state.webhookEnabled;
        this.state.notifErrors = {};
    }

    /**
     * Open the notification preferences on what is saved: a token or a
     * secret typed before a Cancel is dropped, so the next Save keeps the
     * stored ones.
     */
    async openNotifModal() {
        this.state.showUserMenu = false;
        this.state.expandedSection = null;
        this.state.notifErrors = {};
        this.state._telegramToken = "";
        this.state._webhookSecret = "";
        try {
            const prefs = await rpc("/cloud/get_user_preferences", {});
            this.state.notifLevel = prefs?.cloud_notification_level || "failures";
            this.state.notifMode = prefs?.cloud_notification_mode || "immediate";
            this.state.emailEnabled = prefs?.cloud_email_enabled !== false;
            this.state.notifMuted = prefs?.cloud_muted_projects || [];
            this.state.telegramTokenConfigured = prefs?.cloud_telegram_configured || false;
            this.state.telegramChatId = prefs?.cloud_telegram_chat_id || "";
            this.state.webhookSecretConfigured = prefs?.cloud_webhook_configured || false;
            this.state.webhookUrl = prefs?.cloud_webhook_url || "";
            this.state.telegramEnabled = (
                this.state.telegramTokenConfigured || !!this.state.telegramChatId
            );
            this.state.webhookEnabled = (
                this.state.webhookSecretConfigured || !!this.state.webhookUrl
            );
        } catch (_e) { console.debug("Preferences fetch skipped:", _e); }
        try {
            const data = await rpc("/cloud/get_projects", {});
            this.state.notifProjects = (data?.items || []).map(
                (p) => ({ id: p.id, name: p.name }),
            );
        } catch (_e) { console.debug("Projects fetch skipped:", _e); }
        this.state.showNotifModal = true;
    }

    closeNotifModal() {
        this.state.showNotifModal = false;
    }

    /** Projects still pickable in the mute selector (not muted yet). */
    get mutableProjects() {
        const muted = new Set(this.state.notifMuted.map((p) => p.id));
        return this.state.notifProjects
            .filter((p) => !muted.has(p.id))
            .map((p) => ({ value: String(p.id), label: p.name }));
    }

    /**
     * Add the SearchSelect pick to the muted list.
     * @param {string} value stringified project id from the selector
     */
    addMutedProject(value) {
        const id = parseInt(value, 10);
        const project = this.state.notifProjects.find((p) => p.id === id);
        if (project && !this.state.notifMuted.some((p) => p.id === id)) {
            this.state.notifMuted.push({ id: project.id, name: project.name });
        }
    }

    /**
     * Remove a project chip from the muted list.
     * @param {number} id project id
     */
    removeMutedProject(id) {
        this.state.notifMuted = this.state.notifMuted.filter(
            (p) => p.id !== id,
        );
    }

    async saveNotifPrefs() {
        this.state.notifSaving = true;
        try {
            const telegram = this.state.telegramEnabled;
            const hasToken = telegram && (
                this.state._telegramToken || this.state.telegramTokenConfigured
            );
            // The server trims them: spaces alone are no address.
            const hasChatId = telegram && !!this.state.telegramChatId.trim();
            if (hasToken && !hasChatId) {
                this._notifFieldError(
                    "telegramChatId", "telegram",
                    _t("Telegram Chat ID is required when a bot token is configured"),
                );
                return;
            }
            if (hasChatId && !hasToken) {
                this._notifFieldError(
                    "telegramToken", "telegram",
                    _t("Telegram Bot Token is required when a chat ID is configured"),
                );
                return;
            }
            const webhook = this.state.webhookEnabled;
            const hasSecret = webhook && (
                this.state._webhookSecret || this.state.webhookSecretConfigured
            );
            const hasUrl = webhook && !!this.state.webhookUrl.trim();
            if (hasSecret && !hasUrl) {
                this._notifFieldError(
                    "webhookUrl", "webhook",
                    _t("Webhook URL is required when a signing secret is configured"),
                );
                return;
            }
            const res = await this._savePrefsSilent();
            if (res?.ok === false) {
                // Nothing was saved: the modal stays open on what to fix.
                this.env.toast?.error(
                    res.error || _t("Failed to save notification preferences"),
                );
                return;
            }
            this.env.toast?.success(_t("Notification preferences saved"));
            this.state.showNotifModal = false;
        } catch (_) {
            this.env.toast?.error(_t("Failed to save notification preferences"));
        } finally {
            this.state.notifSaving = false;
        }
    }

    async detectChatId() {
        this.state.telegramDetecting = true;
        try {
            // The token typed goes with the check instead of being saved
            // first: the server keeps a token only with a chat ID.
            const res = await rpc("/cloud/telegram_detect_chat_id", {
                bot_token: this.state._telegramToken || "",
            });
            if (res?.ok) {
                this.state.telegramChatId = res.chat_id || "";
                this.env.toast?.success(_t("Chat ID detected"));
            } else {
                this.env.toast?.error(res?.error || _t("Could not detect chat ID"));
            }
        } catch (_) {
            this.env.toast?.error(_t("Failed to detect chat ID"));
        } finally {
            this.state.telegramDetecting = false;
        }
    }

    async sendTelegramTest() {
        this.state.telegramTesting = true;
        try {
            // What the card shows, saved or not; the test saves nothing.
            const res = await rpc("/cloud/telegram_send_test", {
                bot_token: this.state._telegramToken || "",
                chat_id: this.state.telegramChatId || "",
            });
            if (res?.ok) {
                this.env.toast?.success(_t("Test message sent"));
            } else {
                this.env.toast?.error(res?.error || _t("Failed to send test message"));
            }
        } catch (_) {
            this.env.toast?.error(_t("Failed to send test message"));
        } finally {
            this.state.telegramTesting = false;
        }
    }

    /**
     * Show a Save refusal under its field: the field's card opens, the
     * control is marked invalid and described by the message, and the
     * focus goes to it on the next render. The toast still says it.
     *
     * @param {string} field telegramToken, telegramChatId or webhookUrl
     * @param {string} section the card holding it: telegram or webhook
     * @param {string} message what to fix
     */
    _notifFieldError(field, section, message) {
        this.state.notifErrors = { [field]: message };
        this.state.expandedSection = section;
        this.focusNext = NOTIF_FIELD_IDS[field];
        this.env.toast?.error(message);
    }

    /**
     * Give the focus to the field a Save refusal named, once rendered.
     * Does nothing unless a refusal asked for it.
     */
    restoreFocus() {
        const id = this.focusNext;
        this.focusNext = null;
        if (id) {
            document.getElementById(id)?.focus();
        }
    }

    /** Set from PasswordInput onChange — store in temp state for save. */
    onTelegramTokenChange(value) {
        this.state._telegramToken = value;
        this.state.notifErrors = {};
    }

    onWebhookSecretChange(value) {
        this.state._webhookSecret = value;
        this.state.notifErrors = {};
    }

    /**
     * Take the chat ID typed; the refusals tie it to the token, so a
     * change clears them.
     *
     * @param {string} value the chat ID as typed
     */
    onTelegramChatIdInput(value) {
        this.state.telegramChatId = value;
        this.state.notifErrors = {};
    }

    /**
     * Take the webhook URL typed; the refusal ties it to the signing
     * secret, so a change clears it.
     *
     * @param {string} value the URL as typed
     */
    onWebhookUrlInput(value) {
        this.state.webhookUrl = value;
        this.state.notifErrors = {};
    }
}
