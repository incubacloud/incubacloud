import { Component, useState, onWillStart, onMounted, onWillUnmount, useEnv } from "@odoo/owl";
import { rpc } from "@web/core/network/rpc";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";

export class ProjectSidebar extends Component {
    static template = "incubacloud.ProjectSidebar";
    static props = {
        projectId:          { type: Number },
        currentRoute:       { type: String },
        selectedInstanceId: { type: Number },
    };

    setup() {
        this.env = useEnv();
        this.state = useState({
            projectName: "",
            odooVersion: "",
            instances: [],
            loading: true,
            cloneModal: null,  // { instanceId, name, loading, error }
        });

        this.orm = useService("orm");
        this._busService = useService("bus_service");

        this._onJobUpdate = async (payload) => {
            if (this._destroyed) return;
            try {
                // Refresh on every job state change so instance status dots
                // (deployed, running, provisioning) update in real time.
                await this._silentRefresh();
            } catch (_e) { console.debug("Bus handler skipped (component destroyed):", _e); }
        };
        this._busService.subscribe("cloud_jobs", this._onJobUpdate);

        // Register sidebar callbacks so instance_detail can call them
        onMounted(() => {
            this.env.registerSidebar?.(
                () => this._silentRefresh(),
                (excludeId) => this._getNextInstance(excludeId),
            );
        });

        onWillUnmount(() => {
            this._destroyed = true;
            this._busService.unsubscribe("cloud_jobs", this._onJobUpdate);
            this.env.unregisterSidebar?.();
        });

        onWillStart(() => this.load());
    }

    async load() {
        this.state.loading = true;
        try {
            const data = await rpc("/cloud/get_project_full", {
                project_id: this.props.projectId,
            });
            // Cache the full project data for instant instance switching
            this.env.setProjectCache?.(this.props.projectId, data);
            // Extract sidebar-level data
            const instances = Object.values(data.instances || {});
            this.state.projectName = data.project?.name || "";
            this.state.odooVersion = data.project?.odoo_version || "";
            this.state.instances = instances;
            this.env.setProjectName?.(data.project?.name || "");
        } catch (_e) { console.warn("Failed to load project:", _e); }
        this.state.loading = false;

        // Auto-redirect: if landing on project_detail and there are instances,
        // navigate to the first instance (prod > staging > dev)
        if (this.props.currentRoute === "project_detail" && this.state.instances.length) {
            const first = this._getNextInstance(null);
            if (first) {
                this.env.navigate("instance_detail", {
                    project_id: this.props.projectId,
                    instance_id: first.id,
                });
            }
        }
    }

    async _silentRefresh() {
        try {
            const data = await rpc("/cloud/get_project_full", {
                project_id: this.props.projectId,
            });
            this.env.setProjectCache?.(this.props.projectId, data);
            this.state.instances = Object.values(data.instances || {});
        } catch (_e) { console.debug("Silent refresh failed:", _e); }
    }

    async loadInstances() {
        await this._silentRefresh();
    }

    get grouped() {
        const groups = { production: [], staging: [] };
        for (const inst of this.state.instances) {
            const env = inst.environment || "staging";
            if (groups[env]) groups[env].push(inst);
        }
        return groups;
    }

    get selectedId() {
        return this.props.selectedInstanceId || null;
    }

    get activeSection() {
        const r = this.props.currentRoute;
        if (r === "instance_detail" || r === "create_instance") return "";
        if (r === "project_settings") return "project_settings";
        // Don't highlight Settings when onboarding is shown (no instances)
        if (r === "project_detail" && !this.state.instances.length) return "";
        return r;
    }

    goBack() {
        this.env.navigate("projects");
    }

    selectInstance(inst) {
        this.env.navigate("instance_detail", {
            project_id: this.props.projectId,
            instance_id: inst.id,
        });
    }

    goProjectSettings() {
        this.env.navigate("project_settings", { project_id: this.props.projectId });
    }

    createInstance(environment) {
        this.env.navigate("create_instance", {
            project_id: this.props.projectId,
            environment,
        });
    }

    envLabel(env) {
        return { production: _t("Production"), staging: _t("Staging") }[env] || env;
    }

    statusDot(inst) {
        if (inst.status === "provisioning") return "dot-provisioning";
        if (!inst.deployed) return "dot-neutral";
        if (inst.running) return "dot-ok";
        if (inst.status === "error") return "dot-error";
        return "dot-stopped";
    }

    cloneToStaging(inst) {
        this.state.cloneModal = {
            instanceId: inst.id,
            name: inst.name + '-staging',
            loading: false,
            error: null,
        };
    }

    async doClone() {
        const m = this.state.cloneModal;
        if (!m || m.loading || !m.name.trim()) return;
        m.loading = true;
        m.error = null;
        try {
            const res = await rpc("/cloud/clone_to_staging", {
                instance_id: m.instanceId,
                staging_name: m.name.trim(),
            });
            if (!res.ok) {
                m.error = res.error;
                m.loading = false;
                return;
            }
            this.state.cloneModal = null;
            await this.loadInstances();
            // Navigate to the new staging instance
            this.env.navigate("instance_detail", {
                project_id: this.props.projectId,
                instance_id: res.staging_id,
            });
        } catch (e) {
            m.error = e.data?.message || e.message || _t("Clone failed.");
            m.loading = false;
        }
    }

    _getNextInstance(excludeId) {
        const order = ["production", "staging"];
        for (const env of order) {
            for (const inst of this.grouped[env] || []) {
                if (inst.id !== excludeId) return inst;
            }
        }
        return null;
    }
}
