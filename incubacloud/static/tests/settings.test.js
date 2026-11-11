import { beforeEach, describe, expect, test } from "@odoo/hoot";
import { allowTranslations } from "@web/../tests/web_test_helpers";
import { _buildUrl, _parseRoute } from "@incubacloud/app/app";
import {
    changedKeys, changedValues, Settings, snapshot,
} from "@incubacloud/components/settings/settings";
import {
    blocksOf, fieldProblem, legacySection, sectionFor, sectionShown, settingField, settingsIndex,
    settingsMenu, settingsSections,
} from "@incubacloud/components/settings/settings_registry";
import {
    editDistance, fold, markMatch, scoreField, searchElsewhere, searchSettings, suggestion,
} from "@incubacloud/components/settings/settings_search";
import {
    coreForm, CORE_PARTS, coreSaveVals,
} from "@incubacloud/components/settings/core_settings_block";
import { GithubSettingsBlock } from "@incubacloud/components/settings/github_settings_block";
import { logSizeProblem, looksLikeRange, rangesProblem } from "@incubacloud/components/settings/core_sections";
import {
    CORE_RATE_DEFAULTS,
    CoreRatesTab,
} from "@incubacloud/components/core_rates_tab/core_rates_tab";

/**
 * Settings (ESTANDAR-FORMULARIOS, option A): a menu of sections, one page
 * each, a search over one index of declared fields, and a save bar that
 * covers the page. The pieces are pure enough to test without mounting.
 */

// The labels are lazy translations, read here as text.
beforeEach(() => allowTranslations());

/** An env as the panel gives one: who may see what. */
const env = (features = {}) => ({ features, permissions: { can_manage_settings: true } });

describe("Settings — the menu", () => {
    test("core's groups and sections, in order", () => {
        const menu = settingsMenu(env());
        const ids = menu.map((g) => g.id);
        expect(ids.slice(0, 3)).toEqual(["platform", "integrations", "limits"]);
        const platform = menu[0].sections.map((s) => s.id);
        expect(platform).toEqual(["general", "instances", "logs", "edge"]);
        const integrations = menu[1].sections.map((s) => s.id);
        expect(integrations.slice(0, 3)).toEqual(["github", "git-providers", "monitoring"]);
        expect(menu[2].sections.map((s) => s.id)).toEqual(["rates"]);
    });

    test("Monitoring hides where the platform runs observability", () => {
        const ids = (features) => settingsMenu(env(features))
            .flatMap((g) => g.sections.map((s) => s.id));
        expect(ids({}).includes("monitoring")).toBe(true);
        expect(ids({ observability: { configure: false } }).includes("monitoring")).toBe(false);
    });

    test("a section shows the blocks registered for it", () => {
        expect(blocksOf("logs", env()).map((b) => b.key)).toEqual(["core-logs"]);
        expect(blocksOf("rates", env())[0].Component).toBe(CoreRatesTab);
        expect(blocksOf("github", env())[0].Component).toBe(GithubSettingsBlock);
    });

    test("the old tabs land on their sections", () => {
        expect(sectionFor(undefined, undefined)).toBe("general");
        expect(sectionFor("logs", "github")).toBe("logs");
        expect(sectionFor(undefined, "git_providers")).toBe("git-providers");
        expect(sectionFor(undefined, "rates")).toBe("rates");
        expect(legacySection("no-such-tab")).toBe(undefined);
    });

    test("each module names its own old tabs", () => {
        // Core names no section of another module: a tab is found only in
        // the section its module registered.
        for (const [id, section] of settingsSections.getEntries()) {
            if (section.legacyTab) {
                expect(legacySection(section.legacyTab)).toBe(id);
            }
        }
    });

    test("a section of a module absent or hidden opens the first", () => {
        const agents = settingsSections.get("agents", null);
        if (agents) {
            settingsSections.remove("agents");
        }
        try {
            expect(sectionShown("agents", undefined, env())).toBe("general");
            expect(sectionShown(undefined, "agents", env())).toBe("general");
        } finally {
            if (agents) {
                settingsSections.add("agents", agents);
            }
        }
        const hidden = env({ observability: { configure: false } });
        expect(sectionShown("monitoring", undefined, hidden)).toBe("general");
        expect(sectionShown("monitoring", undefined, env())).toBe("monitoring");
        expect(sectionShown("logs", undefined, env())).toBe("logs");
    });
});

describe("Settings — the URL", () => {
    test("a section and a field", () => {
        expect(_buildUrl("settings", {})).toBe("/cloud/settings");
        expect(_buildUrl("settings", { section: "logs" })).toBe("/cloud/settings/logs");
        expect(_buildUrl("settings", { section: "logs", field: "log_search_timeout_s" }))
            .toBe("/cloud/settings/logs#log_search_timeout_s");
        expect(_buildUrl("settings", { tab: "github" })).toBe("/cloud/settings/github");
    });

    test("the path names the section", () => {
        const { route, params } = _parseRoute("/cloud/settings/edge");
        expect(route).toBe("settings");
        expect(params.section).toBe("edge");
    });

    test("the page takes a section, a field and an old tab", () => {
        for (const name of ["section", "field", "tab"]) {
            expect(Settings.props[name].optional).toBe(true);
        }
    });
});

describe("Settings — the fields", () => {
    test("each is declared once, with a unique label", () => {
        const index = settingsIndex(env());
        const ids = index.map((f) => f.id);
        expect(new Set(ids).size).toBe(ids.length);
        const labels = index.map((f) => String(f.label));
        expect(new Set(labels).size).toBe(labels.length);
    });

    test("the three retentions no longer share a label", () => {
        expect(String(settingField("audit_log_retention_days").label)).toBe("Audit log retention");
        expect(String(settingField("job_retention_days").label)).toBe("Job retention");
        expect(String(settingField("job_log_retention_days").label)).toBe("Job log retention");
    });

    test("labels are in sentence case and carry no unit", () => {
        for (const field of settingsIndex(env())) {
            const label = String(field.label);
            expect(/\((days|hours|seconds|MB|%|EUR)\)$/.test(label)).toBe(false);
        }
    });

    test("every field the core forms hold is declared", () => {
        for (const keys of Object.values(CORE_PARTS)) {
            for (const key of keys) {
                expect(Boolean(settingField(key))).toBe(true);
            }
        }
        for (const key of Object.keys(CORE_RATE_DEFAULTS)) {
            expect(Boolean(settingField(key))).toBe(true);
        }
    });

    test("numbers are checked by their declaration", () => {
        const timeout = settingField("log_search_timeout_s");
        expect(fieldProblem(timeout, 3)).toBe("Must be 5 seconds or more.");
        expect(fieldProblem(timeout, 30)).toBe(null);
        expect(fieldProblem(timeout, "2.5")).toBe("Use a whole number.");
        const pct = settingField("default_backup_alert_threshold_pct");
        expect(fieldProblem(pct, 101)).toBe("Must be 100 or less.");
        expect(fieldProblem(pct, "")).toBe(null);
    });

    test("ranges and log sizes say how to fix them", () => {
        expect(looksLikeRange("198.51.100.0/24")).toBe(true);
        expect(looksLikeRange("2001:db8::/32")).toBe(true);
        expect(looksLikeRange("300.1.1.1")).toBe(false);
        expect(rangesProblem("198.51.100.0/24\n10.0.0.1")).toBe(null);
        expect(rangesProblem("198.51.100.0/24\nproxy")).toInclude("Not a range: proxy.");
        expect(logSizeProblem("10m")).toBe(null);
        expect(logSizeProblem("lots")).toInclude("such as 10m");
    });
});

describe("Settings — the search", () => {
    const index = settingsIndex(env());

    test("folds case and accents", () => {
        expect(fold("  Días  de RETENCIÓN ")).toBe("dias de retencion");
    });

    test("scores the label first, then synonyms, titles and help", () => {
        const field = {
            label: "Default backup destination", synonyms: ["s3"], blockTitle: "Backups",
            sectionTitle: "General", groupTitle: "Platform", help: "Used by production instances",
        };
        expect(scoreField(field, "default")).toBe(120);
        expect(scoreField(field, "backup")).toBe(100);
        expect(scoreField(field, "s3")).toBe(70);
        expect(scoreField(field, "platform")).toBe(50);
        expect(scoreField(field, "production")).toBe(30);
        expect(scoreField(field, "nothing")).toBe(0);
    });

    test("needs two characters", () => {
        expect(searchSettings(index, "b").total).toBe(0);
    });

    test("groups by section in the menu's order, three each", () => {
        const { total, groups } = searchSettings(index, "retention");
        expect(total > 3).toBe(true);
        const sections = groups.map((g) => g.section);
        expect(sections.indexOf("logs") >= 0).toBe(true);
        const logs = groups.find((g) => g.section === "logs");
        expect(logs.fields.length).toBe(3);
        expect(logs.fields.length + logs.more >= 3).toBe(true);
    });

    test("backup finds the default destination first", () => {
        const { groups } = searchSettings(index, "backup");
        expect(groups[0].section).toBe("general");
        expect(groups[0].fields[0].id).toBe("default_backup_backend_id");
    });

    test("a typo is offered the word it missed", () => {
        expect(searchSettings(index, "bakcup").total).toBe(0);
        expect(suggestion(index, "bakcup")).toBe("backup");
        expect(editDistance("bakcup", "backup")).toBe(2);
    });

    test("pages outside Settings are found too", () => {
        const entries = [{ key: "b", label: "Backup destinations", synonyms: ["s3"] }];
        expect(searchElsewhere(entries, "s3").length).toBe(1);
        expect(searchElsewhere(entries, "smtp").length).toBe(0);
    });

    test("marks the match in the label", () => {
        expect(markMatch("Default backup destination", "backup")).toEqual({
            before: "Default ", match: "backup", after: " destination",
        });
    });
});

describe("Settings — unsaved changes", () => {
    test("a snapshot tells what changed since", () => {
        const form = { a: 1, b: "x", c: [1] };
        const saved = snapshot(form);
        expect(changedKeys(form, saved)).toEqual([]);
        form.b = "y";
        form.c = [1, 2];
        expect(changedKeys(form, saved)).toEqual(["b", "c"]);
    });

    test("each core section loads its own fields only", () => {
        const form = coreForm("general", {
            autoassign_enabled: 1, default_backup_backend_id: 0, job_retention_days: 5,
        });
        expect(Object.keys(form)).toEqual(CORE_PARTS.general);
        expect(form.autoassign_enabled).toBe(true);
        expect(form.default_backup_backend_id).toBe(null);
        expect(form.default_backup_alert_threshold_pct).toBe(80);
    });

    test("a retention of 0 stays 0; a log bound of 0 is its default", () => {
        const form = coreForm("logs", { audit_log_retention_days: 0, log_search_timeout_s: 0 });
        expect(form.audit_log_retention_days).toBe(0);
        expect(form.log_search_timeout_s).toBe(30);
    });

    test("secrets come back empty", () => {
        const form = coreForm("monitoring", { metrics_remote_write_token: "x" });
        expect(form.metrics_remote_write_token).toBe("");
    });

    test("a block sends the fields it changed, not the whole form", () => {
        // A job may write the others meanwhile ("Enable observability"
        // fills the metrics URLs): sending them back would undo it.
        const form = coreForm("monitoring", { metrics_central_url: "" });
        const saved = snapshot(form);
        form.metrics_retention_days = 30;
        expect(coreSaveVals(changedValues(form, saved))).toEqual({ metrics_retention_days: 30 });
        expect(Object.keys(changedValues(form, undefined))).toEqual(CORE_PARTS.monitoring);
    });

    test("no default destination is sent as 0, and only when it changed", () => {
        const form = coreForm("general", { default_backup_backend_id: 4 });
        const saved = snapshot(form);
        expect(coreSaveVals(changedValues(form, saved))).toEqual({});
        form.default_backup_backend_id = null;
        expect(coreSaveVals(changedValues(form, saved))).toEqual({ default_backup_backend_id: 0 });
    });

    test("a save called off leaves the page as it was, and says nothing", async () => {
        const toasts = [];
        const block = { form: { github_webhook_allowlist: true } };
        const page = Object.create(Settings.prototype);
        page.env = { toast: { success: (message) => toasts.push(message) } };
        page.state = { saving: false, pageError: null };
        page.store = { saved: { Edge: snapshot(block.form) }, errors: {}, found: null };
        page._tabs = new Map([["Edge", {
            state: block,
            writeOnly: [],
            save: async () => {
                // The user called it off: the block puts its form back.
                block.form.github_webhook_allowlist = true;
                return { cancelled: true };
            },
        }]]);
        block.form.github_webhook_allowlist = false;
        expect(page.changedCount).toBe(1);
        await page.save();
        expect(page.state.pageError).toBe(null);
        expect(page.changedCount).toBe(0);
        expect(toasts).toEqual([]);
    });
});

describe("Settings — GitHub", () => {
    /**
     * A GitHub block whose loading and detection only record that they ran.
     *
     * @param {string} afterLoad the installation id the reload finds
     * @param {string} afterDetect the installation id the detection takes
     * @returns {{block: object, calls: string[]}}
     */
    function blockCalling(afterLoad, afterDetect) {
        const calls = [];
        const block = Object.create(GithubSettingsBlock.prototype);
        block.state = { configured: true, form: { installation_id: "" } };
        block.env = {
            toast: {
                success: () => calls.push("success"),
                info: () => calls.push("info"),
            },
        };
        block.load = async () => {
            calls.push("load");
            block.state.form.installation_id = afterLoad;
        };
        block.detectInstallation = async () => {
            calls.push("detect");
            block.state.form.installation_id = afterDetect;
        };
        return { block, calls };
    }

    test("the steps follow the App", () => {
        const { block } = blockCalling("", "");
        block.state.configured = false;
        expect(block.currentStep).toBe(1);
        block.state.configured = true;
        expect(block.currentStep).toBe(2);
        block.state.form.installation_id = "77";
        expect(block.currentStep).toBe(3);
    });

    test("an installation the webhook did not take is detected", async () => {
        const { block, calls } = blockCalling("", "5151");
        await block.refresh();
        expect(calls).toEqual(["load", "detect", "success"]);
    });

    test("one the webhook took needs no detection", async () => {
        const { block, calls } = blockCalling("5151", "");
        await block.refresh();
        expect(calls).toEqual(["load", "success"]);
    });

    test("none anywhere is said", async () => {
        const { block, calls } = blockCalling("", "");
        await block.refresh();
        expect(calls).toEqual(["load", "detect", "info"]);
    });
});

describe("Settings — rate limits", () => {
    test("preview and import defaults are separate hourly caps", () => {
        expect(CORE_RATE_DEFAULTS.rate_limit_github_previews_per_hour).toBe(10);
        expect(CORE_RATE_DEFAULTS.rate_limit_github_imports_per_hour).toBe(5);
        expect(String(settingField("rate_limit_github_imports_per_hour").unit)).toBe("/h");
        expect(settingField("rate_limit_github_imports_per_hour").advanced).toBe(true);
    });

    test("a limit under 1 is refused before it is sent", () => {
        expect(fieldProblem(settingField("rate_limit_webhook_per_min"), 0)).toBe("Must be 1 or more.");
    });
});
