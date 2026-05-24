import { Component, useState, onMounted, onWillUnmount, onWillStart, useSubEnv } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { useService } from "@web/core/utils/hooks";
import { useDebouncedBus } from "../utils/use_debounced_bus";
import { createProjectStore } from "../store/project_store";
import { ProjectDashboard } from "../components/project_dashboard/project_dashboard";
import { ProjectDetail } from "../components/project_detail/project_detail";

import { HostsDashboard } from "../components/hosts_dashboard/hosts_dashboard";
import { HostDetail } from "../components/host_detail/host_detail";
import { InstanceDetail } from "../components/instance_detail/instance_detail";
import { Settings } from "../components/settings/settings";
import { BackupBackendsList } from "../components/backup_backend_detail/backup_backends_list";
import { BackupBackendDetail } from "../components/backup_backend_detail/backup_backend_detail";
import { SlideOver } from "../components/slide_over/slide_over";
import { AppHeader } from "../components/app_header/app_header";
import { ProjectSidebar } from "../components/project_sidebar/project_sidebar";
import { MainComponentsContainer } from "@web/core/main_components_container";
import { ToastContainer } from "../toast/toast_container";
import { createToastService } from "../toast/toast";

const _BASE = "/cloud";

// ── Route extension registry ─────────────────────────────────────────────────
// Private modules call registerRoute() to add SPA routes without modifying core.
const _routeExtensions = [];

/**
 * Register an additional SPA route from a third-party module.
 * @param {string}   name  - route identifier (e.g. 'custom_page')
 * @param {function} parse - (parts: string[]) => params object | null
 * @param {function} build - (params: object) => URL string
 */
export function registerRoute(name, parse, build) {
    _routeExtensions.push({ name, parse, build });
}

export function _parseRoute(pathname) {
    const path = pathname.startsWith(_BASE) ? pathname.slice(_BASE.length) : "/";
    const parts = path.split("/").filter(Boolean);

    if (!parts.length) return { route: "projects", params: {} };

    switch (parts[0]) {
        case "projects": {
            if (parts.length === 1) return { route: "projects", params: {} };
            if (parts[1] === "new") return { route: "new_project", params: {} };
            const pid = parseInt(parts[1]);
            if (isNaN(pid)) return { route: "projects", params: {} };
            if (parts[2] === "settings") return { route: "project_settings", params: { project_id: pid } };
            if (parts.length === 2) return { route: "project_detail", params: { project_id: pid } };
            if (parts[2] === "instances") {
                if (parts.length === 3) return { route: "project_detail", params: { project_id: pid } };
                if (parts[3] === "new") {
                    const env = new URLSearchParams(window.location.search).get("env") || undefined;
                    return { route: "create_instance", params: { project_id: pid, ...(env ? { environment: env } : {}) } };
                }
                const iid = parseInt(parts[3]);
                if (!isNaN(iid)) return { route: "instance_detail", params: { project_id: pid, instance_id: iid } };
            }
            return { route: "project_detail", params: { project_id: pid } };
        }
        case "hosts": {
            if (parts.length === 1) return { route: "hosts", params: {} };
            if (parts[1] === "new") return { route: "new_host", params: {} };
            const hid = parseInt(parts[1]);
            if (!isNaN(hid)) {
                return { route: "host_detail", params: { host_id: hid } };
            }
            return { route: "hosts", params: {} };
        }
        case "settings": {
            const tab = new URLSearchParams(window.location.search).get("tab") || undefined;
            return { route: "settings", params: tab ? { tab } : {} };
        }
        case "backup-backends": {
            if (parts.length === 1) return { route: "backup_backends", params: {} };
            if (parts[1] === "new") return { route: "new_backup_backend", params: {} };
            const bid = parseInt(parts[1]);
            if (!isNaN(bid)) return { route: "backup_backend_detail", params: { backend_id: bid } };
            return { route: "backup_backends", params: {} };
        }
        default: {
            for (const ext of _routeExtensions) {
                const params = ext.parse(parts);
                if (params !== null) return { route: ext.name, params };
            }
            return { route: "projects", params: {} };
        }
    }
}

export function _buildUrl(route, params = {}) {
    switch (route) {
        case "projects":          return `${_BASE}/projects`;
        case "new_project":        return `${_BASE}/projects/new`;
        case "project_detail":    return `${_BASE}/projects/${params.project_id}`;
        case "project_settings":  return `${_BASE}/projects/${params.project_id}/settings`;

        case "create_instance": {
            const qs = params.environment ? `?env=${params.environment}` : "";
            return `${_BASE}/projects/${params.project_id}/instances/new${qs}`;
        }
        case "instance_detail":   return `${_BASE}/projects/${params.project_id}/instances/${params.instance_id}`;
        case "hosts":             return `${_BASE}/hosts`;
        case "new_host":          return `${_BASE}/hosts/new`;
        case "host_detail":       return `${_BASE}/hosts/${params.host_id}`;

        case "settings":              return params.tab ? `${_BASE}/settings?tab=${params.tab}` : `${_BASE}/settings`;
        case "backup_backends":       return `${_BASE}/backup-backends`;
        case "new_backup_backend":    return `${_BASE}/backup-backends/new`;
        case "backup_backend_detail": return `${_BASE}/backup-backends/${params.backend_id}`;
        default: {
            for (const ext of _routeExtensions) {
                if (ext.name === route) return ext.build(params);
            }
            return _BASE;
        }
    }
}

export class Chrome extends Component {
    static components = { AppHeader, ProjectSidebar, ProjectDashboard, ProjectDetail, HostsDashboard, HostDetail, InstanceDetail, Settings, BackupBackendsList, BackupBackendDetail, SlideOver, MainComponentsContainer, ToastContainer };

    get isInsideProject() {
        const r = this.state.route;
        return ["project_detail", "project_settings", "create_instance", "instance_detail"].includes(r)
            && !!this.state.params.project_id;
    }
    static props = { disableLoader: Function };

    setup() {
        const initial = _parseRoute(window.location.pathname);
        this.state = useState({
            route: initial.route,
            params: initial.params,
            features: {},
            role: 'stakeholder',
            permissions: {},
            userName: '',
            userLogin: '',
            avatarUrl: '',
            alertCount: 0,
            projectName: "",
            slideOver: null,  // { panel: "jobs"|"alerts", instanceId }
        });

        this._pollAlertCount = async () => {
            try {
                const data = await rpc('/cloud/get_alert_count', {});
                this.state.alertCount = data.count;
            } catch (_e) { console.debug("Alert count fetch skipped:", _e); }
        };
        this._busService = useService("bus_service");
        // Debounce overview invalidations: a burst of alert mutations
        // (e.g. dismiss-all-selected) would otherwise trigger as many
        // ``get_alert_count`` RPCs as there are rows affected.
        const triggerAlertRefresh = useDebouncedBus(() => this._pollAlertCount());
        this._onOverviewUpdate = () => triggerAlertRefresh();

        onWillStart(async () => {
            try {
                const config = await rpc("/cloud/get_config", {});
                this.state.features = config.features || {};
                this.state.role = config.role || 'stakeholder';
                this.state.permissions = config.permissions || {};
                if (config.user) {
                    this.state.userName = config.user.name || '';
                    this.state.userLogin = config.user.login || '';
                    this.state.avatarUrl = config.user.avatar_url || '';
                }
            } catch (_e) { console.warn("Failed to load initial config:", _e); }
        });

        // Slot for the current "form dirty" guard. Registered by
        // detail components via useNavGuard(). Only one slot — the SPA
        // shows a single page per route, so there is never a valid
        // reason for two guards at once. If two components register
        // we keep the latest (mirrors the user's focus).
        this._navGuard = null;

        // Remember the pre-popstate URL so we can rewind the browser
        // history when the user cancels a back/forward transition.
        this._lastAcceptedRoute = _parseRoute(window.location.pathname);

        this._onPopState = async () => {
            const parsed = _parseRoute(window.location.pathname);
            // popstate has already mutated history before we get the
            // event — if the guard rejects, we push the previous URL
            // back so the address bar matches reality.
            if (this._navGuard) {
                const ok = await this._navGuard();
                if (!ok) {
                    const prev = this._lastAcceptedRoute;
                    history.pushState(
                        null, "",
                        _buildUrl(prev.route, prev.params),
                    );
                    return;
                }
            }
            this.state.route = parsed.route;
            this.state.params = parsed.params;
            this._lastAcceptedRoute = parsed;
        };

        onMounted(() => {
            this.props.disableLoader();
            window.addEventListener("popstate", this._onPopState);
            this._pollAlertCount();
            this._busService.subscribe("cloud_overview", this._onOverviewUpdate);
            this._busService.start();
        });

        onWillUnmount(() => {
            window.removeEventListener("popstate", this._onPopState);
            this._busService.unsubscribe("cloud_overview", this._onOverviewUpdate);
        });

        const appState = this.state;
        const pollAlertCount = this._pollAlertCount;
        let _alertReturnUrl = null;
        // Toast notification service
        const { toastApi, toasts, dismissToast } = createToastService();

        // Single source of truth for project data on the SPA side.
        // Owns the cloud_jobs bus subscription that drives the
        // ``/cloud/get_project_full`` cache; replaces the eight
        // ad-hoc env helpers (project cache + sidebar callback
        // registry) the codebase used to wire by hand. See
        // ``store/project_store.js`` for the contract.
        const projectStore = createProjectStore({ busService: this._busService });
        useSubEnv({
            toast: toastApi,
            toasts,
            dismissToast,
            projectStore,
            setAlertReturnUrl: (url) => { _alertReturnUrl = url; },
            getAlertReturnUrl: () => _alertReturnUrl,
            get alertCount() { return appState.alertCount; },
            get role() { return appState.role; },
            get permissions() { return appState.permissions; },
            get userInfo() {
                return {
                    name: appState.userName,
                    login: appState.userLogin,
                    avatarUrl: appState.avatarUrl,
                };
            },
            get currentRoute() {
                return {
                    route: appState.route,
                    params: appState.params,
                    projectName: appState.projectName,
                };
            },
            setProjectName: (name) => { appState.projectName = name; },
            refreshAlertCount: () => pollAlertCount(),
            toggleSlideOver: (panel, instanceId = false, hostId = false) => {
                if (appState.slideOver && appState.slideOver.panel === panel) {
                    appState.slideOver = null;
                } else {
                    appState.slideOver = { panel, instanceId, hostId, expanded: false };
                }
            },
            closeSlideOver: () => {
                appState.slideOver = null;
            },
            navigate: async (route, params = {}) => {
                if (route === "logout") {
                    // ``beforeunload`` (still registered by useNavGuard)
                    // fires for full-page reloads, so logout inherits
                    // the same warning for free — no special casing.
                    window.location.href = "/";
                    return;
                }
                if (this._navGuard) {
                    const ok = await this._navGuard();
                    if (!ok) return;
                }
                const url = _buildUrl(route, params);
                history.pushState(null, "", url);
                appState.route = route;
                appState.params = params;
                this._lastAcceptedRoute = { route, params };
                // Drop project state when navigating away so the store
                // doesn't keep refreshing in the background after we
                // leave the project context.
                const projectRoutes = [
                    "project_detail", "project_settings", "create_instance",
                    "instance_detail", "project_hub",
                ];
                if (!projectRoutes.includes(route)) {
                    appState.projectName = "";
                    projectStore.invalidate();
                }
            },
            setNavGuard: (fn) => { this._navGuard = fn; },
            clearNavGuard: () => { this._navGuard = null; },
            get features() { return appState.features; },
        });
    }

    closeSlideOver() {
        this.state.slideOver = null;
    }

    toggleSlideOverSize() {
        if (this.state.slideOver) {
            this.state.slideOver.expanded = !this.state.slideOver.expanded;
        }
    }
}

Chrome.template = "incubacloud.AppShell";
