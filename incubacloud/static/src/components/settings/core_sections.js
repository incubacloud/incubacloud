/** @odoo-module **/

/**
 * The panel's own groups, sections and blocks of Settings, with every field
 * they show (see settings_registry.js). Modules on top add theirs the same
 * way: a section of their own, or a block in one of these.
 */

import { _t } from "@web/core/l10n/translation";
import {
    settingsBlocks, settingsElsewhere, settingsGroups, settingsSections,
} from "./settings_registry";
import { CoreSettingsBlock, GitProvidersBlock } from "./core_settings_block";
import { GithubSettingsBlock } from "./github_settings_block";
import { CoreRatesTab } from "../core_rates_tab/core_rates_tab";

const IPV4 = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?:\/(\d{1,2}))?$/;
const IPV6 = /^[0-9a-f:.]+(?:\/(\d{1,3}))?$/i;

/**
 * Whether a line reads as an address or a CIDR range.
 *
 * Only the format: the server checks each range for good and says which
 * one it refused.
 *
 * @param {string} line one line of a range list
 * @returns {boolean}
 */
export function looksLikeRange(line) {
    const v4 = IPV4.exec(line);
    if (v4) {
        return v4.slice(1, 5).every((octet) => Number(octet) <= 255)
            && (v4[5] === undefined || Number(v4[5]) <= 32);
    }
    const v6 = IPV6.exec(line);
    return Boolean(v6) && line.includes(":") && (v6[1] === undefined || Number(v6[1]) <= 128);
}

/**
 * What is wrong with a list of ranges, one per line, or null.
 *
 * @param {string} value the list
 * @returns {string|null}
 */
export function rangesProblem(value) {
    const bad = String(value || "").split(/[\n,]+/).map((line) => line.trim())
        .filter((line) => line && !looksLikeRange(line));
    if (!bad.length) {
        return null;
    }
    return _t("Not a range: %s. Use one CIDR range per line, such as 198.51.100.0/24.", bad[0]);
}

/**
 * What is wrong with a Docker log size, or null.
 *
 * @param {string} value e.g. "10m"
 * @returns {string|null}
 */
export function logSizeProblem(value) {
    return /^[1-9]\d*[kmg]$/i.test(String(value ?? "").trim())
        ? null
        : _t("Use a whole number followed by k, m or g, such as 10m.");
}

const days = _t("days");
const perMin = _t("/min");
const perHour = _t("/h");

// ── Groups ──────────────────────────────────────────────────────────────────
settingsGroups.add("platform", { title: _t("Platform"), sequence: 10 });
settingsGroups.add("integrations", { title: _t("Integrations"), sequence: 20 });
settingsGroups.add("limits", { title: _t("Limits"), sequence: 30 });

// ── Platform ────────────────────────────────────────────────────────────────
settingsSections.add("general", {
    group: "platform", sequence: 10, title: _t("General"), legacyTab: "general",
    description: _t("Where new instances go and where their backups are kept by default."),
});
settingsBlocks.add("core-general", {
    section: "general", sequence: 10, Component: CoreSettingsBlock, props: { part: "general" },
    fields: [
        {
            id: "autoassign_enabled", type: "toggle", block: _t("Placement"),
            label: _t("Auto-assign hosts"),
            help: _t("New instances go to the host with the most free resources. Off: pick a host by hand."),
            synonyms: ["placement", "scheduler", "host selection", "capacity"],
        },
        {
            id: "default_backup_backend_id", type: "select", width: "m", block: _t("Backups"),
            label: _t("Default backup destination"),
            help: _t("Used by production instances when neither the project nor the instance names one."),
            synonyms: ["backup", "storage", "s3", "bucket", "destination", "restic", "duplicity"],
        },
        {
            id: "default_backup_alert_threshold_pct", type: "number", unit: "%", min: 0, max: 100,
            block: _t("Backups"), label: _t("Usage alert threshold"),
            help: _t("Email when a destination reaches this share of its quota. Each destination can override it."),
            synonyms: ["backup", "quota", "disk", "alert", "storage usage"],
        },
    ],
});

settingsSections.add("instances", {
    group: "platform", sequence: 20, title: _t("Instances"),
    description: _t("What happens to instances over time: moves between hosts and unused stagings."),
});
settingsBlocks.add("core-instances", {
    section: "instances", sequence: 10, Component: CoreSettingsBlock, props: { part: "instances" },
    fields: [
        {
            id: "move_frozen_source_hours", type: "number", unit: _t("hours"), min: 0,
            block: _t("Moves between hosts"), label: _t("Keep the old copy"),
            help: _t("How long the stopped copy stays on the old host after a move. 0 removes it at the next hourly pass."),
            synonyms: ["move", "migration", "old host", "cleanup"],
        },
        {
            id: "move_cut_alert_seconds", type: "number", unit: _t("seconds"), min: 0,
            block: _t("Moves between hosts"), label: _t("Alert when a move's cut exceeds"),
            help: _t("Warn when an instance is unavailable longer than this during a move. 0 never warns."),
            synonyms: ["downtime", "move", "cutover"],
        },
        {
            id: "staging_autopurge_days", type: "number", unit: days, min: 0,
            block: _t("Staging autopurge"), label: _t("Delete unused stagings after"),
            help: _t("No panel action and no login inside it for this long. 0 turns it off."),
            synonyms: ["staging", "cleanup", "purge", "delete", "idle"],
        },
    ],
});

settingsSections.add("logs", {
    group: "platform", sequence: 30, title: _t("Logs & retention"),
    description: _t("How long the panel keeps its own records, and how instance logs are kept and read."),
});
settingsBlocks.add("core-logs", {
    section: "logs", sequence: 10, Component: CoreSettingsBlock, props: { part: "logs" },
    fields: [
        {
            id: "audit_log_retention_days", type: "number", unit: days, min: 0,
            block: _t("Panel records"), label: _t("Audit log retention"),
            help: _t("Older audit entries are purged. 0 keeps them all."),
            synonyms: ["audit", "history", "purge", "gdpr", "retention"],
        },
        {
            id: "job_retention_days", type: "number", unit: days, min: 0,
            block: _t("Panel records"), label: _t("Job retention"),
            help: _t("Finished jobs older than this are purged, and leave the instance timeline. 0 keeps them all."),
            synonyms: ["jobs", "timeline", "history", "purge", "retention"],
        },
        {
            id: "job_log_retention_days", type: "number", unit: days, min: 0,
            block: _t("Panel records"), label: _t("Job log retention"),
            help: _t("Log output of finished jobs. Active jobs are never purged. 0 keeps them all."),
            synonyms: ["job logs", "output", "purge", "retention"],
        },
        {
            id: "github_event_truncate_days", type: "number", unit: days, min: 0,
            block: _t("GitHub events"), label: _t("Payload truncation"),
            help: _t("The raw webhook payload is replaced by a short stub after this."),
            synonyms: ["webhook", "github", "payload", "retention"],
        },
        {
            id: "github_event_retention_days", type: "number", unit: days, min: 0,
            block: _t("GitHub events"), label: _t("Full row deletion"),
            help: _t("Events are deleted after this. Unprocessed or failed events are never deleted."),
            synonyms: ["webhook", "github", "events", "purge", "retention"],
        },
        {
            id: "odoo_log_archive_days", type: "number", unit: days, min: 1,
            block: _t("Instance logs"), label: _t("Odoo log archive"),
            help: _t("Dated log files kept on the host per instance."),
            synonyms: ["logs", "odoo log", "rotation", "retention"],
        },
        {
            id: "container_log_max_size", type: "text", width: "s", mono: true, placeholder: "10m",
            block: _t("Instance logs"), label: _t("Max size per container log"),
            help: _t("Database, backup and mail containers. Applies on the next deploy."),
            synonyms: ["docker", "json-file", "backup container", "disk"],
            validate: logSizeProblem,
        },
        {
            id: "container_log_max_file", type: "number", min: 1,
            block: _t("Instance logs"), label: _t("Container log files kept"),
            help: _t("Rotated files per container."),
            synonyms: ["docker", "rotation"],
        },
        {
            id: "log_download_max_mb", type: "number", unit: "MB", min: 1, advanced: true,
            block: _t("Instance logs"), label: _t("Download cap"),
            help: _t("Largest log file the panel sends in one download."),
            synonyms: ["logs", "download", "limit"],
        },
        {
            id: "log_search_max_files", type: "number", unit: days, min: 1, advanced: true,
            block: _t("Instance logs"), label: _t("Search: days swept"),
            help: _t("How far back a log search reads."),
            synonyms: ["logs", "search", "grep"],
        },
        {
            id: "log_search_timeout_s", type: "number", unit: "s", min: 5, advanced: true,
            minMessage: _t("Must be 5 seconds or more."),
            block: _t("Instance logs"), label: _t("Search timeout"),
            help: _t("A search that hits it says so rather than reporting no matches."),
            synonyms: ["logs", "search", "timeout"],
        },
    ],
});

settingsSections.add("edge", {
    group: "platform", sequence: 40, title: _t("Network edge"),
    description: _t("Which proxies in front of the panel are trusted to say who the caller is."),
});
settingsBlocks.add("core-edge", {
    section: "edge", sequence: 10, Component: CoreSettingsBlock, props: { part: "edge" },
    fields: [
        {
            id: "trusted_proxy_ranges", type: "textarea", width: "l", mono: true,
            placeholder: "198.51.100.0/24\n2001:db8::/32",
            block: _t("Trusted proxies"), label: _t("Trusted proxy ranges (override)"),
            help: _t("One CIDR range per line. Empty: the connecting address is used as it is."),
            synonyms: ["proxy", "cdn", "cloudflare", "x-forwarded-for", "real ip", "cidr"],
            validate: rangesProblem,
        },
        {
            id: "effective_proxy_ranges", type: "custom",
            block: _t("Trusted proxies"), label: _t("Applied"),
            synonyms: ["proxy", "cloudflare", "real ip"],
        },
    ],
});

// ── Integrations ────────────────────────────────────────────────────────────
settingsSections.add("github", {
    group: "integrations", sequence: 10, title: _t("GitHub"), legacyTab: "github",
    description: _t("The GitHub App that rebuilds on push and reads repositories, and a fallback token."),
});
settingsBlocks.add("core-github", {
    section: "github", sequence: 10, Component: GithubSettingsBlock,
    fields: [
        {
            id: "app_id", type: "text", width: "s", mono: true, advanced: true,
            block: _t("GitHub App"), label: _t("App ID"),
            help: _t("Filled in when the app is created from here."),
            synonyms: ["github app"],
        },
        {
            id: "installation_id", type: "text", width: "s", mono: true, advanced: true,
            block: _t("GitHub App"), label: _t("Installation ID"),
            help: _t("Detected after installing the app on the organization."),
            synonyms: ["github", "organization"],
        },
        {
            id: "webhook_secret", type: "secret", advanced: true,
            block: _t("GitHub App"), label: _t("Webhook secret"),
            help: _t("Signs every webhook GitHub sends."),
            synonyms: ["webhook", "signature", "hmac"],
        },
        {
            id: "private_key", type: "secret", rows: 6, width: "l", advanced: true,
            placeholder: "-----BEGIN RSA PRIVATE KEY-----",
            block: _t("GitHub App"), label: _t("Private key (PEM)"),
            help: _t("The app's key, used to mint installation tokens."),
            synonyms: ["pem", "rsa", "key"],
        },
        {
            id: "github_pat", type: "secret",
            block: _t("Personal access token"), label: _t("Personal access token"),
            help: _t("For private repositories the app cannot reach, such as odoo/enterprise."),
            synonyms: ["pat", "token", "enterprise", "private repo"],
        },
    ],
});

settingsSections.add("git-providers", {
    group: "integrations", sequence: 15, title: _t("Git providers"), legacyTab: "git_providers",
    description: _t("GitLab servers the panel reads, deploys and rebuilds from, each with its credentials."),
});
settingsBlocks.add("core-git-providers", {
    section: "git-providers", sequence: 10, Component: GitProvidersBlock,
    fields: [
        {
            id: "git_providers", type: "custom",
            block: _t("Git providers"), label: _t("GitLab servers"),
            help: _t("Each server is saved on its own, with its token and webhooks."),
            synonyms: ["gitlab", "git", "token", "self-hosted", "webhook"],
        },
    ],
});

settingsSections.add("monitoring", {
    group: "integrations", sequence: 20, title: _t("Monitoring"), legacyTab: "monitoring",
    description: _t("Metrics from every host to a central stack, read back for alerts and dashboards."),
    // Where the platform runs observability for the panel, it is not
    // configured here.
    visible: (env) => env.features?.observability?.configure !== false,
});
settingsBlocks.add("core-monitoring", {
    section: "monitoring", sequence: 10, Component: CoreSettingsBlock, props: { part: "monitoring" },
    fields: [
        {
            id: "central_host", type: "custom", block: _t("Observability"),
            label: _t("Where the central runs"),
            help: _t("VictoriaMetrics, Loki and a gateway, deployed as a job on the host picked here. Its server keeps the metrics history and the security logs, and cannot be deleted while the central runs on it."),
            synonyms: ["metrics", "victoriametrics", "loki", "prometheus", "observability"],
        },
        {
            id: "observability_status", type: "custom", block: _t("Observability"),
            label: _t("Status"), synonyms: ["metrics", "observability"],
        },
        {
            id: "central_retire", type: "custom", block: _t("Observability"),
            label: _t("Retire the central (the history is lost)"),
            help: _t("For a central whose server is lost, or a panel whose only server is going: the panel forgets it and observability goes off, so the server can be deleted."),
            synonyms: ["metrics", "central", "delete", "remove"],
        },
        {
            id: "metrics_enabled", type: "toggle", advanced: true, block: _t("Observability"),
            label: _t("Observability"),
            help: _t("Master switch. Everything is inert while it is off."),
            synonyms: ["metrics", "monitoring"],
        },
        {
            id: "metrics_central_url", type: "text", width: "l", mono: true, advanced: true,
            placeholder: "http://172.17.0.1:8428/r/", block: _t("Observability"),
            label: _t("Query URL"),
            help: _t("Read by the panel, so it must resolve from inside the panel container."),
            synonyms: ["metrics", "victoriametrics"],
        },
        {
            id: "metrics_remote_write_url", type: "text", width: "l", mono: true, advanced: true,
            placeholder: "https://metrics.example.com/w/", block: _t("Observability"),
            label: _t("Remote-write URL"),
            help: _t("Where each host's agent pushes. Agents on other hosts need a public HTTPS address."),
            synonyms: ["metrics", "agent", "push"],
        },
        {
            id: "metrics_remote_write_token", type: "secret", advanced: true, block: _t("Observability"),
            label: _t("Credential"),
            help: _t("Replacing it means redeploying the central and enrolling every host again."),
            synonyms: ["token", "metrics"],
        },
        {
            id: "metrics_retention_days", type: "number", unit: days, min: 1, advanced: true,
            block: _t("Observability"), label: _t("Metrics retention"),
            synonyms: ["retention", "metrics"],
        },
        {
            id: "metrics_collect_pg", type: "toggle", advanced: true, block: _t("Observability"),
            label: _t("Collect database figures"),
            help: _t("Each instance's connections, queries and lock waits, every minute, read on its server as root. Applies to every server within hours."),
            synonyms: ["postgresql", "database", "locks", "collector"],
        },
        {
            id: "metrics_central_public_host", type: "text", width: "m", mono: true, advanced: true,
            placeholder: "metrics.example.com", block: _t("Observability"),
            label: _t("Central's public name"),
            help: _t("For a central on a host of its own; point the name's DNS at that host first."),
            synonyms: ["dns", "metrics"],
        },
        {
            id: "metrics_central_single_path", type: "toggle", advanced: true, block: _t("Observability"),
            label: _t("One path, without a CDN"),
            help: _t("The central answers under its public name only, on its server's direct port and outside any CDN, with a certificate this panel signs. The panel and the agents move to it once the name answers."),
            synonyms: ["cdn", "certificate", "direct", "port", "9443"],
        },
        {
            id: "metrics_central_write_access", type: "select", width: "l", advanced: true,
            block: _t("Observability"),
            label: _t("Who can write to the central"),
            help: _t("With One path: only this panel's servers, by their address, or any address with its account's credential, for servers behind NAT or with an address that changes."),
            synonyms: ["allowlist", "nat", "firewall", "metrics"],
        },
        {
            id: "metrics_central_direct_host", type: "text", width: "m", mono: true, advanced: true,
            placeholder: "direct.metrics.example.com", block: _t("Observability"),
            label: _t("Central's direct name"),
            help: _t("For the platform's own servers, without the CDN: two levels under the zone, DNS only."),
            synonyms: ["dns", "direct", "cdn"],
        },
        {
            id: "acme_dns_cloudflare_token", type: "secret", advanced: true, block: _t("Observability"),
            label: _t("DNS token for direct certificates (Cloudflare)"),
            help: _t("Zone read and DNS edit on the zone only."),
            synonyms: ["acme", "letsencrypt", "certificate", "cloudflare"],
        },
        {
            id: "metrics_central_operator_sources", type: "textarea", width: "l", mono: true, rows: 2,
            advanced: true, placeholder: "203.0.113.10/32", block: _t("Observability"),
            label: _t("Who may delete series and read the fleet on the central"),
            help: _t("One range per line: the panel's public address, which deletes series and reads every account's. Used only with a public name."),
            synonyms: ["allowlist", "metrics"],
            validate: rangesProblem,
        },
    ],
});

// ── Limits ──────────────────────────────────────────────────────────────────
settingsSections.add("rates", {
    group: "limits", sequence: 10, title: _t("Rate limits"), legacyTab: "rates",
    description: _t("Requests per minute in a tumbling window unless a field says per hour. Changes apply at once."),
});
/**
 * A rate limit's declaration: a whole number of requests, 1 or more.
 *
 * @param {string} id the field
 * @param {string} label its label
 * @param {object} [extra] unit, advanced, synonyms
 * @returns {object}
 */
function rate(id, label, extra = {}) {
    return {
        id, label, type: "number", unit: perMin, min: 1, block: _t("Core endpoints"),
        ...extra, synonyms: ["rate", "throttle", "limit", ...(extra.synonyms || [])],
    };
}
settingsBlocks.add("core-rates", {
    section: "rates", sequence: 10, Component: CoreRatesTab,
    fields: [
        rate("rate_limit_webhook_per_min", _t("GitHub webhook (per client IP)"), { synonyms: ["webhook"] }),
        rate("rate_limit_terminal_per_min", _t("Terminal open (per instance)"), { synonyms: ["shell", "console"] }),
        rate("rate_limit_terminal_user_per_min", _t("Terminal open (per user)"), { synonyms: ["shell"] }),
        rate("rate_limit_host_console_per_min", _t("Host console sessions (per user, per host)"), { synonyms: ["ssh"] }),
        rate("rate_limit_connect_per_min", _t("Connect as user (per instance)"), { synonyms: ["impersonate", "login as"] }),
        rate("rate_limit_connect_user_per_min", _t("Connect as user (per user)"), { synonyms: ["impersonate"] }),
        rate("rate_limit_logs_per_min", _t("Log reads (per user)"), { synonyms: ["logs"] }),
        rate("rate_limit_mail_reads_per_min", _t("Mailbox reads on staging (per user)"), { synonyms: ["mail", "email"] }),
        rate("rate_limit_log_search_per_min", _t("Log searches and downloads (per user)"), { synonyms: ["logs", "search"] }),
        rate("rate_limit_github_previews_per_hour", _t("GitHub import previews (per user)"), {
            unit: perHour, advanced: true, synonyms: ["import", "github"],
        }),
        rate("rate_limit_github_imports_per_hour", _t("GitHub project imports (per user)"), {
            unit: perHour, advanced: true, synonyms: ["import", "github"],
        }),
        rate("rate_limit_git_reads_per_hour", _t("Git provider reads (per user)"), {
            unit: perHour, advanced: true, synonyms: ["gitlab", "git"],
        }),
    ],
});

// ── Elsewhere in the panel ──────────────────────────────────────────────────
settingsElsewhere.add("backup_destinations", {
    label: _t("Backup destinations"), where: _t("Page · the list of backup destinations"),
    synonyms: ["backup", "storage", "s3", "bucket", "destination"], route: "backup_backends",
});
settingsElsewhere.add("instance_backup_backend", {
    label: _t("Backup destination of an instance"), where: _t("Instance › Networking, on each instance"),
    synonyms: ["backup", "storage"], route: "projects",
});
settingsElsewhere.add("project_backup_backend", {
    label: _t("Backup destination of a project"), where: _t("Project › Settings › General, on each project"),
    synonyms: ["backup"], route: "projects",
});
settingsElsewhere.add("instance_backups", {
    label: _t("Backups of an instance"), where: _t("Instance › Backups, on each instance"),
    synonyms: ["backup", "restore", "snapshot"], route: "projects",
});
settingsElsewhere.add("instance_smtp", {
    label: _t("SMTP host of an instance"), where: _t("Instance › Networking, on each instance"),
    synonyms: ["smtp", "mail", "email"], route: "projects",
});
settingsElsewhere.add("hosts", {
    label: _t("Hosts"), where: _t("Page · the servers the panel deploys to"),
    synonyms: ["server", "vps", "ssh", "traefik"], route: "hosts",
});
