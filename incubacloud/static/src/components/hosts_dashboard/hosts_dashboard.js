import { Component, useState, useEnv, onWillUnmount } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { useDebounced } from "@web/core/utils/timing";
import { rpc } from '@web/core/network/rpc';
import { _t } from "@web/core/l10n/translation";
import { tagStyle, tagDotStyle } from "../tag_selector/tag_selector";
import { agoText, parseUTC } from "../../utils/dates";
import { TruncationBanner } from "../truncation_banner/truncation_banner";
import { useVisibilityRefresh } from "../../utils/use_visibility_refresh";
import { useDebouncedBus, refreshesOn } from "../../utils/use_debounced_bus";
import { mergeById } from "../../utils/merge_by_id";

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
        // event (coalesced to 300 ms) is correct and cheaper. Every
        // host is listed, so no host filter; plain output is dropped.
        const triggerRefresh = useDebouncedBus(() => {
            if (!this._destroyed) this._silentRefresh();
        });
        this._onJobUpdate = (payload) => {
            if (!refreshesOn(payload)) return;
            triggerRefresh(payload.id);
        };
        this._busService.subscribe("cloud_jobs", this._onJobUpdate);
        this._busService.start();

        onWillUnmount(() => {
            this._destroyed = true;
            this._busService.unsubscribe("cloud_jobs", this._onJobUpdate);
        });
        // Bus covers the happy path; ``visibilitychange`` refresh catches
        // events dropped while the tab was backgrounded.
        useVisibilityRefresh(() => {
            if (!this._destroyed) this._silentRefresh();
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

    /**
     * Fetch the fleet and write it into state. Owns no loading/error
     * presentation — the two callers below decide how a failure is
     * surfaced, which is the whole point of the split.
     */
    async _fetchHosts() {
        const prevFilter = new Set(this.state.visible_hosts.map(h => h.id));
        const filtered = prevFilter.size && prevFilter.size < this.state.hosts.length;
        const resp = await rpc('/cloud/get_hosts', {});
        // Merge instead of replace so a refresh only notifies the rows
        // that actually moved, and the cards the user is looking at
        // keep their identity.
        this.state.hosts = mergeById(this.state.hosts, resp.hosts || resp || []);
        this.state.truncated = !!resp.truncated;
        this.state.total = resp.total || this.state.hosts.length;
        this.state.limit = resp.limit || 200;
        // A background refresh must not silently clear an active
        // search. Whether a filter was in effect is decided from the
        // pre-fetch state — comparing list lengths afterwards, as this
        // used to, mistook "every host matches the query" for "no
        // filter" and reset the box.
        this.state.visible_hosts = filtered
            ? this.state.hosts.filter(h => prevFilter.has(h.id))
            : this.state.hosts;
    }

    /**
     * First paint and explicit Retry: show the skeleton while the
     * fetch is in flight, and render the error stub if it fails.
     */
    async loadHosts() {
        this.state.error = "";
        this.state.loading = true;
        try {
            await this._fetchHosts();
        } catch (err) {
            const msg = err?.data?.message ?? err?.message;
            this.state.error = (typeof msg === "string" && msg)
                ? msg
                : "Could not load hosts. Check your connection and retry.";
        } finally {
            this.state.loading = false;
        }
    }

    /**
     * Background refresh (bus event, tab refocus). Deliberately leaves
     * ``loading`` and ``error`` alone: flipping ``loading`` swaps the
     * rendered fleet for the skeleton on every event, which reads as a
     * flicker, and a transient failure must not replace good cards
     * with an error stub. Mirrors ``_silentRefresh`` in host_detail
     * and instance_detail.
     */
    async _silentRefresh() {
        try {
            await this._fetchHosts();
        } catch (_e) {
            console.debug("Silent refresh failed:", _e);
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
        if (!pct || pct <= 0 || pct >= 100) return _t("%s GB free", free);
        const total = Math.round(free * 100 / (100 - pct));
        return _t("%(free)s GB free / %(total)s GB", { free, total });
    }

    /**
     * A number of CPU cores, as a host's card reads it.
     *
     * @param {number} n the cores.
     * @returns {string} "4 cores"
     */
    coresLabel(n) {
        return _t("%s cores", n);
    }

    /**
     * A figure as a card writes it: one decimal at most, none when whole.
     *
     * @param {number} value
     * @returns {string} "0.9", "6"
     */
    formatNum(value) {
        return String(Math.round(Number(value || 0) * 10) / 10);
    }

    /**
     * Whether the host's live CPU and RAM usage can be shown: the server
     * sends them only while its last reading is recent.
     *
     * @param {Object} host a host as ``/cloud/get_hosts`` returns it.
     * @returns {boolean}
     */
    hasLiveUsage(host) {
        return host.usage_state === "live"
            && host.cpu_usage !== null && host.cpu_usage !== undefined
            && host.ram_usage !== null && host.ram_usage !== undefined;
    }

    /**
     * A usage percentage as the bar and its figure read it, whole.
     *
     * @param {number} value percent, 0-100.
     * @returns {number}
     */
    pct(value) {
        return Math.min(100, Math.max(0, Math.round(Number(value || 0))));
    }

    /**
     * The line under the CPU bar: cores in use of the host's, and the
     * vCPU its instances are allowed when there are any.
     *
     * @param {Object} host
     * @returns {string} "0.9 of 4 cores · allocated: 6 vCPU"
     */
    cpuExtra(host) {
        const cores = Number(host.cpu_cores || 0);
        const parts = [];
        if (cores) {
            parts.push(_t("%(used)s of %(total)s cores", {
                used: this.formatNum(host.cpu_usage * cores / 100),
                total: cores,
            }));
        }
        if (Number(host.allocated_cpus || 0)) {
            parts.push(_t("allocated: %s vCPU", this.formatNum(host.allocated_cpus)));
        }
        return parts.join(" · ");
    }

    /**
     * The line under the RAM bar: memory in use of the host's, and the
     * memory its instances are allowed when there are any.
     *
     * @param {Object} host
     * @returns {string} "9.9 of 16 GB · allocated: 12 GB"
     */
    ramExtra(host) {
        const total = Number(host.ram_total_gb || 0);
        const parts = [];
        if (total) {
            parts.push(_t("%(used)s of %(total)s GB", {
                used: this.formatNum(host.ram_usage * total / 100),
                total: this.formatNum(total),
            }));
        }
        if (Number(host.allocated_ram_gb || 0)) {
            parts.push(_t("allocated: %s GB", this.formatNum(host.allocated_ram_gb)));
        }
        return parts.join(" · ");
    }

    /**
     * Why a card shows no live CPU and RAM, in words: never a 0 that
     * reads as an idle host.
     *
     * @param {Object} host
     * @returns {string}
     */
    usageMissing(host) {
        if (host.usage_state === "off") {
            // A tenant's panel has no Monitoring section to turn them on in.
            if (this.metricsNotConfigurable()) {
                return _t("Live usage is not available on this panel.");
            }
            return _t("Metrics are off. Turn them on in Settings to see live usage.");
        }
        if (host.usage_state === "no_agents") {
            return _t("The metrics agents are not installed on this host.");
        }
        if (host.usage_state === "agents_failed") {
            return _t("The installation of the metrics agents on this host failed.");
        }
        const ago = this.timeAgo(host.usage_last_seen);
        // 15 minutes is USAGE_FRESH_SECONDS (cloud_host_metrics.py).
        return ago
            ? _t("No reading in the last 15 minutes (last: %s).", ago)
            : _t("No reading yet.");
    }

    /**
     * Whether this panel cannot turn metrics on, as a tenant's, whose
     * Monitoring section is hidden (``monitoring.js`` asks the same).
     *
     * @returns {boolean}
     */
    metricsNotConfigurable() {
        return this.env.features?.observability?.configure === false;
    }

    /**
     * The icon beside ``usageMissing``: where to look, at a glance.
     *
     * @param {Object} host
     * @returns {string} a key of the Relay icon set.
     */
    usageIcon(host) {
        if (host.usage_state === "off" && this.metricsNotConfigurable()) {
            return "lock";
        }
        // Not ``heartbeat``: the set draws it as the reload arrows.
        return {
            off: "settings",
            no_agents: "download",
            agents_failed: "alert",
        }[host.usage_state] || "clock";
    }

    /**
     * What the host's instances are allowed, for a card without live
     * usage; empty when it runs none.
     *
     * @param {Object} host
     * @returns {string} "Allocated: 6 vCPU · 12 GB"
     */
    allocatedText(host) {
        const cpu = Number(host.allocated_cpus || 0);
        const ram = Number(host.allocated_ram_gb || 0);
        if (!cpu && !ram) return "";
        return _t("Allocated: %(cpu)s vCPU · %(ram)s GB", {
            cpu: this.formatNum(cpu),
            ram: this.formatNum(ram),
        });
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
        return agoText(Math.floor((Date.now() - parseUTC(dateStr).getTime()) / 1000));
    }
}

HostsDashboard.template = "incubacloud.HostsDashboard";
