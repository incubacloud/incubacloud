/** @odoo-module **/
import { Component, useState } from "@odoo/owl";
import { ProjectCard } from "../project_card/project_card";
import { useDebounced } from "@web/core/utils/timing";
import { ImportOdooshModal } from "../import_odoojs_modal/import_odoojs_modal";
import { useService } from "@web/core/utils/hooks";
import { rpc } from '@web/core/network/rpc';

export class ProjectDashboard extends Component {
    static template = "incubacloud.ProjectDashboard";
    static components = { ProjectCard };

    setup() {
        this.state = useState({
            projects: [],
            visible_projects: [],
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
        this.state.projects = await rpc('/cloud/get_projects', {});
        this.state.visible_projects = this.state.projects;
    }

    onSearchInput(event) {
        if (event.target.value.trim()) {
            this.search(event.target.value.trim());
        }else{
            this.state.visible_projects = this.state.projects;
        }
    }

}

