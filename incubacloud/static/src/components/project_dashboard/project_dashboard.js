/** @odoo-module **/
import { Component, useState } from "@odoo/owl";
import { ProjectCard } from "../project_card/project_card";
import { useDebounced } from "@web/core/utils/timing";
import { ImportOdooshModal } from "../import_odoojs_modal/import_odoojs_modal";
import { TruncationBanner } from "../truncation_banner/truncation_banner";
import { useService } from "@web/core/utils/hooks";
import { rpc } from '@web/core/network/rpc';

export class ProjectDashboard extends Component {
    static template = "incubacloud.ProjectDashboard";
    static components = { ProjectCard, TruncationBanner };

    setup() {
        this.state = useState({
            projects: [],
            visible_projects: [],
            truncated: false,
            total: 0,
            limit: 200,
            // Without these a failed fetch rendered the empty state, so a
            // network outage was indistinguishable from "you have no
            // projects" — the most alarming way to show a transient error.
            loading: true,
            error: "",
        });

        this.search = useDebounced((query) => {
            this.state.visible_projects = this.state.projects.filter(project =>
                project.name.toLowerCase().includes(query.toLowerCase())
            );
        }, 300);

        this.dialogService = useService("dialog");
        this.loadProjects();
    }

    openCreateProject() {
        this.env.navigate('new_project');
    }

    async openImportGitHub() {
        this.dialogService.add(ImportOdooshModal, {
            onCreated: () => {
                this.loadProjects();
            },
        });
    }

    async loadProjects() {
        this.state.error = "";
        this.state.loading = true;
        try {
            const data = await rpc('/cloud/get_projects', {});
            // Backend caps the result at 200 to protect against DoS/OOM.
            // Use .items and expose truncation meta so the banner can render.
            this.state.projects = data.items || [];
            this.state.visible_projects = this.state.projects;
            this.state.truncated = !!data.truncated;
            this.state.total = data.total || this.state.projects.length;
            this.state.limit = data.limit || 200;
        } catch (err) {
            const msg = err?.data?.message ?? err?.message;
            this.state.error = (typeof msg === "string" && msg)
                ? msg
                : "Could not load projects. Check your connection and retry.";
        } finally {
            this.state.loading = false;
        }
    }

    onSearchInput(event) {
        if (event.target.value.trim()) {
            this.search(event.target.value.trim());
        }else{
            this.state.visible_projects = this.state.projects;
        }
    }

}

