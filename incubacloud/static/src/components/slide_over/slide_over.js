import { Component, onMounted, onWillUnmount } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { JobHistory } from "../job_history/job_history";
import { AlertHistory } from "../alert_history/alert_history";

export class SlideOver extends Component {
    static template = "incubacloud.SlideOver";
    static components = { JobHistory, AlertHistory };
    static props = {
        panel: { type: String },
        instanceId: { type: [Number, Boolean], optional: true },
        hostId: { type: [Number, Boolean], optional: true },
        expanded: { type: Boolean, optional: true },
        onClose: { type: Function },
        onToggleSize: { type: Function },
    };

    setup() {
        this._onKeyDown = (ev) => {
            if (ev.key === "Escape") this.props.onClose();
        };
        onMounted(() => document.addEventListener("keydown", this._onKeyDown));
        onWillUnmount(() => document.removeEventListener("keydown", this._onKeyDown));
    }

    get title() {
        return this.props.panel === "jobs" ? _t("Job History") : _t("Alerts");
    }
}
