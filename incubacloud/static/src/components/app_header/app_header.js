import { Component, useState, onMounted, onWillUnmount } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { session } from "@web/session";
import { _t } from "@web/core/l10n/translation";
import { IcModal } from "../ic_modal/ic_modal";
import { RlSelect } from "../rl_select/rl_select";
import { SearchSelect } from "../search_select/search_select";
import { PasswordInput } from "../password_input/password_input";

export class AppHeader extends Component {
    static template = "incubacloud.AppHeader";
    static components = { IcModal, RlSelect, SearchSelect, PasswordInput };
    static props = {
        alertCount:   { type: Number },
        currentRoute: { type: String },
        projectName:  { type: String },
        projectId:    { type: Number },
    };

    setup() {
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
            telegramTokenConfigured: false,
            telegramChatId: "",
            telegramDetecting: false,
            telegramTesting: false,
            // Webhook
            webhookSecretConfigured: false,
            webhookUrl: "",
        });

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

    get telegramConfigured() {
        return this.state.telegramTokenConfigured;
    }

    get webhookConfigured() {
        return !!this.state.webhookUrl;
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
        // Persist current form values without closing the modal.
        // Used by Detect / Send test buttons so the backend sees
        // the values the user just typed.
        await rpc("/cloud/save_user_preferences", {
            cloud_notification_level: this.state.notifLevel,
            cloud_notification_mode: this.state.notifMode,
            cloud_email_enabled: this.state.emailEnabled,
            cloud_muted_project_ids: this.state.notifMuted.map((p) => p.id),
            cloud_telegram_bot_token: this.state._telegramToken || "",
            cloud_telegram_chat_id: this.state.telegramChatId || "",
            cloud_webhook_url: this.state.webhookUrl || "",
            cloud_webhook_secret: this.state._webhookSecret || "",
        });
    }

    async openNotifModal() {
        this.state.showUserMenu = false;
        this.state.expandedSection = null;
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
            const hasToken = (
                this.state._telegramToken || this.state.telegramTokenConfigured
            );
            const hasChatId = !!this.state.telegramChatId;
            if (hasToken && !hasChatId) {
                this.env.toast?.error(
                    _t("Telegram Chat ID is required when a bot token is configured"),
                );
                return;
            }
            if (hasChatId && !hasToken) {
                this.env.toast?.error(
                    _t("Telegram Bot Token is required when a chat ID is configured"),
                );
                return;
            }
            const hasSecret = (
                this.state._webhookSecret || this.state.webhookSecretConfigured
            );
            const hasUrl = !!this.state.webhookUrl;
            if (hasSecret && !hasUrl) {
                this.env.toast?.error(
                    _t("Webhook URL is required when a signing secret is configured"),
                );
                return;
            }
            await this._savePrefsSilent();
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
            await this._savePrefsSilent();
            const res = await rpc("/cloud/telegram_detect_chat_id", {});
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
            await this._savePrefsSilent();
            const res = await rpc("/cloud/telegram_send_test", {});
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

    /** Set from PasswordInput onChange — store in temp state for save. */
    onTelegramTokenChange(value) {
        this.state._telegramToken = value;
    }

    onWebhookSecretChange(value) {
        this.state._webhookSecret = value;
    }
}
