import { Component, useState, onMounted, onWillUnmount } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { session } from "@web/session";
import { _t } from "@web/core/l10n/translation";
import { IcModal } from "../ic_modal/ic_modal";

export class AppHeader extends Component {
    static template = "incubacloud.AppHeader";
    static components = { IcModal };
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
            showProjectSwitcher: false,
            projects: [],
            projectSearch: "",
            // Notification preferences modal
            showNotifModal: false,
            notifLevel: "failures",
            notifSaving: false,
        });

        this._closeMenus = (ev) => {
            if (!ev.target.closest(".ic-header-user-wrap")) {
                this.state.showUserMenu = false;
            }
            if (!ev.target.closest(".ic-header-switcher-wrap")) {
                this.state.showProjectSwitcher = false;
            }
        };

        onMounted(() => {
            document.addEventListener("click", this._closeMenus);
            // Override from env.userInfo once config loads
            const info = this.env.userInfo;
            if (info?.name) this.state.userName = info.name;
            if (info?.login) this.state.userLogin = info.login;
            if (info?.avatarUrl) this.state.avatarUrl = info.avatarUrl;
        });
        onWillUnmount(() => document.removeEventListener("click", this._closeMenus));
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
        this.env.toggleSlideOver("jobs", this._currentInstanceId());
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

    async openNotifModal() {
        this.state.showUserMenu = false;
        try {
            const prefs = await rpc("/cloud/get_user_preferences", {});
            this.state.notifLevel = prefs?.cloud_notification_level || "failures";
        } catch (_e) { console.debug("Preferences fetch skipped:", _e); }
        this.state.showNotifModal = true;
    }

    closeNotifModal() {
        this.state.showNotifModal = false;
    }

    async saveNotifPrefs() {
        this.state.notifSaving = true;
        try {
            await rpc("/cloud/save_user_preferences", {
                cloud_notification_level: this.state.notifLevel,
            });
            this.env.toast?.success(_t("Notification preferences saved"));
            this.state.showNotifModal = false;
        } catch (_) {
            this.env.toast?.error(_t("Failed to save notification preferences"));
        } finally {
            this.state.notifSaving = false;
        }
    }
}
