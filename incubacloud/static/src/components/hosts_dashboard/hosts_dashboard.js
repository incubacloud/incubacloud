import { Component, useState, useEnv, onWillUnmount } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { useDebounced } from "@web/core/utils/timing";
import { rpc } from '@web/core/network/rpc';
import { _t } from "@web/core/l10n/translation";
import { tagStyle, tagDotStyle } from "../tag_selector/tag_selector";
import { parseUTC } from "../../utils/dates";
import { TruncationBanner } from "../truncation_banner/truncation_banner";
import { useVisibilityRefresh } from "../../utils/use_visibility_refresh";
import { useDebouncedBus } from "../../utils/use_debounced_bus";

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
            // See project_dashboard: a failed fetch must not render as an
            // empty fleet.
            loading: true,
            error: "",
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
        // Debounced reload. We used to fetch the job details first to
        // check ``state in (done, failed, cancelled)`` — but hosts
        // show metrics and last-job info that can change on any
        // transition, not just terminal ones. Refreshing on every
        // event (coalesced to 300 ms) is correct and cheaper.
        const triggerRefresh = useDebouncedBus(() => {
            if (!this._destroyed) this.loadHosts();
        });
        this._onJobUpdate = (payload) => triggerRefresh(payload.id);
        this._busService.subscribe("cloud_jobs", this._onJobUpdate);
        this._busService.start();

        onWillUnmount(() => {
            this._destroyed = true;
            this._busService.unsubscribe("cloud_jobs", this._onJobUpdate);
        });
        // Bus covers the happy path; ``visibilitychange`` refresh catches
        // events dropped while the tab was backgrounded.
        useVisibilityRefresh(() => {
            if (!this._destroyed) this.loadHosts();
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
        this.state.error = "";
        this.state.loading = true;
        try {
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
        } catch (err) {
            const msg = err?.data?.message ?? err?.message;
            this.state.error = (typeof msg === "string" && msg)
                ? msg
                : "Could not load hosts. Check your connection and retry.";
        } finally {
            this.state.loading = false;
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
        if (value >= 70) return 'bar-warn';
        return 'bar-ok';
    }

    /**
     * Map a metric percentage to the Relay ``rl-mbar-fill`` band class.
     *
     * The Relay metric-bar primitive uses bare ``warn``/``crit`` modifier
     * classes (empty string = the default green). Thresholds match the
     * shared convention: warn >= 70%, crit >= 85%.
     *
     * @param {number} value percentage (0-100)
     * @returns {string} "" | "warn" | "crit"
     */
    mbarClass(value) {
        if (value >= 85) return 'crit';
        if (value >= 70) return 'warn';
        return '';
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
