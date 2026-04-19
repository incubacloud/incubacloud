import { Component, useState, useEnv, onWillUnmount } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { useDebounced } from "@web/core/utils/timing";
import { rpc } from '@web/core/network/rpc';
import { _t } from "@web/core/l10n/translation";
import { tagStyle, tagDotStyle } from "../tag_selector/tag_selector";
import { parseUTC } from "../../utils/dates";
import { TruncationBanner } from "../truncation_banner/truncation_banner";

export class HostsDashboard extends Component {
    static components = { TruncationBanner };

    setup() {
        this.env = useEnv();
        this.state = useState({
            hosts: [],
            visible_hosts: [],
            truncated: false,
            total: 0,
            limit: 200,
        });
        this.search = useDebounced((query) => {
            const q = query.toLowerCase();
            this.state.visible_hosts = this.state.hosts.filter(host => {
                if (host.name.toLowerCase().includes(q)) return true;
                const tags = host.tags || [];
                return tags.some(t => (t.name || "").toLowerCase().includes(q));
            });
        }, 300);
        this.orm = useService("orm");
        this.loadHosts();

        this._busService = useService("bus_service");
        this._onJobUpdate = async (payload) => {
            if (this._destroyed) return;
            try {
                const [job] = await this.orm.call("cloud.job", "load_jobs", [payload.id]);
                if (this._destroyed) return;
                if (job && ["done", "failed", "cancelled"].includes(job.state)) {
                    this.loadHosts();
                }
            } catch (_e) { console.debug("Bus handler skipped (component destroyed):", _e); }
        };
        this._busService.subscribe("cloud_jobs", this._onJobUpdate);
        this._busService.start();

        this._refreshTimer = setInterval(() => this.loadHosts(), 30000);
        onWillUnmount(() => {
            this._destroyed = true;
            clearInterval(this._refreshTimer);
            this._busService.unsubscribe("cloud_jobs", this._onJobUpdate);
        });
    }

    onSearchInput(event) {
        if (event.target.value.trim()) {
            this.search(event.target.value.trim());
        } else {
            this.state.visible_hosts = this.state.hosts;
        }
    }

    onAddHost() {
        this.env.navigate("new_host");
    }

    async loadHosts() {
        const prevFilter = this.state.visible_hosts.map(h => h.id);
        const resp = await rpc('/cloud/get_hosts', {});
        this.state.hosts = resp.hosts || resp || [];
        this.state.truncated = !!resp.truncated;
        this.state.total = resp.total || this.state.hosts.length;
        this.state.limit = resp.limit || 200;
        if (prevFilter.length && prevFilter.length < this.state.hosts.length) {
            this.state.visible_hosts = this.state.hosts.filter(h => prevFilter.includes(h.id));
        } else {
            this.state.visible_hosts = this.state.hosts;
        }
    }

    onViewHost(host) {
        this.env.navigate("host_detail", { host_id: host.id });
    }

    // ── Helpers ───────────────────────────────────────────────────────────

    tagBadgeStyle(tag) { return tagStyle(tag); }
    tagDotStyle(tag) { return tagDotStyle(tag); }

    barClass(value) {
        if (!value) return 'bar-empty';
        if (value >= 85) return 'bar-danger';
        if (value >= 65) return 'bar-warn';
        return 'bar-ok';
    }

    hasMetrics(host) {
        return host.cpu_cores > 0 || host.ram_total_gb > 0 || host.disk > 0;
    }

    formatRam(gb) {
        if (!gb) return '—';
        return gb % 1 === 0 ? `${gb} GB` : `${gb.toFixed(1)} GB`;
    }

    diskExtra(host) {
        const free = host.disk_free_gb;
        const pct  = host.disk;
        if (!free && !pct) return '';
        if (!pct || pct <= 0 || pct >= 100) return `${free} GB free`;
        const total = Math.round(free * 100 / (100 - pct));
        return `${free} GB free / ${total} GB`;
    }

    usagePct(host) {
        const r = Number(host.usage_ratio || 0);
        return Math.min(100, Math.round(r * 100));
    }

    usageExtra(host) {
        const cpu = Number(host.allocated_cpus || 0);
        const cores = Number(host.cpu_cores || 0);
        const ram = Number(host.allocated_ram_gb || 0);
        const ramTot = Number(host.ram_total_gb || 0);
        if (!cores && !ramTot) return '';
        return `${cpu}/${cores} vCPU · ${ram.toFixed(1)}/${ramTot.toFixed(1)} GB`;
    }

    statusLabel(status) {
        return {
            compatible: _t("Compatible"),
            degraded: _t("Degraded"),
            unsupported: _t("Unsupported"),
            checking: _t("Checking"),
            unknown: _t("Unknown"),
        }[status] || status;
    }

    timeAgo(dateStr) {
        if (!dateStr) return null;
        const diff = Math.floor((Date.now() - parseUTC(dateStr).getTime()) / 1000);
        if (diff < 60) return `${diff}s ago`;
        if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
        if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
        return `${Math.floor(diff / 86400)}d ago`;
    }
}

HostsDashboard.template = "incubacloud.HostsDashboard";
