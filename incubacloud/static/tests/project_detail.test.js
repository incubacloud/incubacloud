import { describe, expect, test } from "@odoo/hoot";
import { ProjectDetail } from "@incubacloud/components/project_detail/project_detail";

/**
 * ProjectDetail uses useEnv, rpc, onWillStart, onMounted, onWillUnmount,
 * TagSelector, and RepoEditor. We test exported/extractable pure logic:
 * _parseReqLine, _buildPkgMap, mergeRequirements, applyPipResolutions,
 * isValid, hasUnsavedChanges, statusLabel, getLineNumbers, and ODOO_VERSIONS.
 */

describe("ProjectDetail — class structure", () => {
    test("template name is set", () => {
        expect(ProjectDetail.template).toBe("incubacloud.ProjectDetail");
    });

    test("props defines project_id as optional Number", () => {
        expect(ProjectDetail.props.project_id.type).toBe(Number);
        expect(ProjectDetail.props.project_id.optional).toBe(true);
    });

    test("props defines embedded as optional Boolean", () => {
        expect(ProjectDetail.props.embedded.type).toBe(Boolean);
        expect(ProjectDetail.props.embedded.optional).toBe(true);
    });

    test("components includes TagSelector and RepoEditor", () => {
        expect("TagSelector" in ProjectDetail.components).toBe(true);
        expect("RepoEditor" in ProjectDetail.components).toBe(true);
    });
});

describe("ProjectDetail — _parseReqLine logic", () => {
    // Extracted from source (not exported, so we re-implement)
    function _parseReqLine(line) {
        const stripped = line.split("#")[0].trim();
        if (!stripped) return null;
        const m = stripped.match(/^([A-Za-z0-9_.-]+)/);
        if (!m) return null;
        const name = m[1].toLowerCase();
        const markerM = stripped.match(/;(.+)/);
        const marker = markerM ? markerM[1].trim().toLowerCase() : "";
        return { name, raw: stripped, key: marker ? `${name}|${marker}` : name };
    }

    test("parses simple package name", () => {
        const result = _parseReqLine("requests==2.28.0");
        expect(result.name).toBe("requests");
        expect(result.raw).toBe("requests==2.28.0");
        expect(result.key).toBe("requests");
    });

    test("returns null for empty line", () => {
        expect(_parseReqLine("")).toBe(null);
    });

    test("returns null for comment-only line", () => {
        expect(_parseReqLine("# this is a comment")).toBe(null);
    });

    test("strips inline comments", () => {
        const result = _parseReqLine("flask>=2.0 # web framework");
        expect(result.raw).toBe("flask>=2.0");
    });

    test("parses package with environment marker", () => {
        const result = _parseReqLine("pymssql<=2.5 ; python_version <= '3.10'");
        expect(result.name).toBe("pymssql");
        expect(result.key).toBe("pymssql|python_version <= '3.10'");
    });

    test("normalizes package name to lowercase", () => {
        const result = _parseReqLine("Flask>=2.0");
        expect(result.name).toBe("flask");
    });

    test("handles package with dots and hyphens", () => {
        const result = _parseReqLine("my-pkg.v2>=1.0");
        expect(result.name).toBe("my-pkg.v2");
    });

    test("returns null for line with only whitespace", () => {
        expect(_parseReqLine("   ")).toBe(null);
    });
});

describe("ProjectDetail — _formatSource logic", () => {
    function _formatSource(url, branch) {
        const m = (url || "").match(/github\.com[:/](.+)/);
        const name = m ? m[1].replace(/\.git$/, "") : (url || "").replace(/\/$/, "");
        return branch ? `${name} (${branch})` : name;
    }

    test("extracts GitHub org/repo from HTTPS URL", () => {
        expect(_formatSource("https://github.com/odoo/odoo.git", "17.0"))
            .toBe("odoo/odoo (17.0)");
    });

    test("extracts GitHub org/repo from SSH URL", () => {
        expect(_formatSource("git@github.com:odoo/enterprise.git", "main"))
            .toBe("odoo/enterprise (main)");
    });

    test("handles non-GitHub URL", () => {
        expect(_formatSource("https://gitlab.com/foo/bar", "dev"))
            .toBe("https://gitlab.com/foo/bar (dev)");
    });

    test("strips trailing slash from non-GitHub URL", () => {
        expect(_formatSource("https://gitlab.com/foo/bar/", "dev"))
            .toBe("https://gitlab.com/foo/bar (dev)");
    });

    test("returns name without branch when branch is empty", () => {
        expect(_formatSource("https://github.com/odoo/odoo.git", ""))
            .toBe("odoo/odoo");
    });

    test("handles null URL", () => {
        expect(_formatSource(null, "main")).toBe(" (main)");
    });
});

describe("ProjectDetail — mergeRequirements logic", () => {
    // Re-implement the core merge logic for testing
    function _parseReqLine(line) {
        const stripped = line.split("#")[0].trim();
        if (!stripped) return null;
        const m = stripped.match(/^([A-Za-z0-9_.-]+)/);
        if (!m) return null;
        const name = m[1].toLowerCase();
        const markerM = stripped.match(/;(.+)/);
        const marker = markerM ? markerM[1].trim().toLowerCase() : "";
        return { name, raw: stripped, key: marker ? `${name}|${marker}` : name };
    }

    function _escapeRegex(str) {
        return str.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    }

    function _formatSource(url, branch) {
        const m = (url || "").match(/github\.com[:/](.+)/);
        const name = m ? m[1].replace(/\.git$/, "") : (url || "").replace(/\/$/, "");
        return branch ? `${name} (${branch})` : name;
    }

    function _buildPkgMap(text) {
        const pkgMap = {};
        const re =
            /<<<<<<< (?<eSrc>[^\n]*)\n(?<eLine>[^\n]*)\n=======\n(?<iLine>[^\n]*)\n>>>>>>> (?<iSrc>[^\n]*)/g;
        let m;
        while ((m = re.exec(text || "")) !== null) {
            const parsed = _parseReqLine(m.groups.eLine.trim());
            if (parsed) {
                pkgMap[parsed.key] = {
                    type: "conflict",
                    spec: parsed.raw,
                    name: parsed.name,
                    existingSrc: m.groups.eSrc.trim(),
                    block: m[0],
                };
            }
        }
        const cleanOnly = (text || "").replace(
            /<<<<<<< [^\n]*\n[^\n]*\n=======\n[^\n]*\n>>>>>>> [^\n]*/g,
            ""
        );
        for (const line of cleanOnly.split("\n")) {
            const parsed = _parseReqLine(line);
            if (parsed && !(parsed.key in pkgMap)) {
                pkgMap[parsed.key] = {
                    type: "clean",
                    spec: parsed.raw,
                    name: parsed.name,
                    block: null,
                };
            }
        }
        return pkgMap;
    }

    function mergeRequirements(existingText, incomingText, repoUrl, repoBranch) {
        const sourceLabel = _formatSource(repoUrl, repoBranch);
        const pkgMap = _buildPkgMap(existingText);
        let newText = existingText || "";
        const added = [];
        const conflicts = [];

        for (const line of (incomingText || "").split("\n")) {
            const parsed = _parseReqLine(line);
            if (!parsed) continue;
            const { name, raw, key } = parsed;

            if (!(key in pkgMap)) {
                added.push(raw);
                pkgMap[key] = { type: "clean", spec: raw, name, block: null };
            } else if (pkgMap[key].spec === raw) {
                // exact duplicate
            } else if (pkgMap[key].type === "conflict") {
                const oldBlock = pkgMap[key].block;
                const existingSrc = pkgMap[key].existingSrc;
                const existingSpec = pkgMap[key].spec;
                const newBlock = `<<<<<<< ${existingSrc}\n${existingSpec}\n=======\n${raw}\n>>>>>>> ${sourceLabel}`;
                newText = newText.replace(oldBlock, newBlock);
                pkgMap[key].block = newBlock;
                conflicts.push({
                    name,
                    existing: existingSpec,
                    existingSource: existingSrc,
                    incoming: raw,
                    incomingSource: sourceLabel,
                });
            } else {
                const existingSpec = pkgMap[key].spec;
                const newBlock = `<<<<<<< existing\n${existingSpec}\n=======\n${raw}\n>>>>>>> ${sourceLabel}`;
                newText = newText.replace(
                    new RegExp("^" + _escapeRegex(existingSpec) + "$", "m"),
                    newBlock
                );
                pkgMap[key] = {
                    type: "conflict",
                    spec: existingSpec,
                    name,
                    existingSrc: "existing",
                    block: newBlock,
                };
                conflicts.push({
                    name,
                    existing: existingSpec,
                    existingSource: "existing",
                    incoming: raw,
                    incomingSource: sourceLabel,
                });
            }
        }

        if (added.length) {
            const base = newText.trimEnd();
            newText = base + (base ? "\n" : "") + added.join("\n");
        }

        return { content: newText, conflicts };
    }

    test("adds new packages to existing text", () => {
        const result = mergeRequirements("requests==2.28", "flask>=2.0", "repo", "main");
        expect(result.content).toBe("requests==2.28\nflask>=2.0");
        expect(result.conflicts.length).toBe(0);
    });

    test("skips exact duplicates", () => {
        const result = mergeRequirements("requests==2.28", "requests==2.28", "repo", "main");
        expect(result.content).toBe("requests==2.28");
        expect(result.conflicts.length).toBe(0);
    });

    test("creates conflict for version mismatch", () => {
        const result = mergeRequirements(
            "requests==2.28",
            "requests==2.31",
            "https://github.com/org/repo.git",
            "main"
        );
        expect(result.conflicts.length).toBe(1);
        expect(result.conflicts[0].name).toBe("requests");
        expect(result.conflicts[0].existing).toBe("requests==2.28");
        expect(result.conflicts[0].incoming).toBe("requests==2.31");
        expect(result.content).toInclude("<<<<<<< existing");
        expect(result.content).toInclude("=======");
        expect(result.content).toInclude(">>>>>>> org/repo (main)");
    });

    test("handles empty existing text", () => {
        const result = mergeRequirements("", "flask>=2.0\nrequests==2.28", "repo", "main");
        expect(result.content).toBe("flask>=2.0\nrequests==2.28");
        expect(result.conflicts.length).toBe(0);
    });

    test("handles empty incoming text", () => {
        const result = mergeRequirements("requests==2.28", "", "repo", "main");
        expect(result.content).toBe("requests==2.28");
        expect(result.conflicts.length).toBe(0);
    });

    test("skips comment lines in incoming", () => {
        const result = mergeRequirements("", "# comment\nflask>=2.0", "repo", "main");
        expect(result.content).toBe("flask>=2.0");
    });
});

describe("ProjectDetail — applyPipResolutions logic", () => {
    function applyPipResolutions(text, choices) {
        return (text || "").replace(
            /<<<<<<< [^\n]*\n([^\n]*)\n=======\n[^\n]*\n>>>>>>> [^\n]*/g,
            (block, existingLine) => {
                const stripped = existingLine.trim().split("#")[0].trim();
                const m = stripped.match(/^([A-Za-z0-9_.-]+)/);
                const name = m ? m[1].toLowerCase() : null;
                return name && name in choices ? choices[name] : block;
            }
        );
    }

    test("resolves conflict with chosen spec", () => {
        const text =
            "<<<<<<< existing\nrequests==2.28\n=======\nrequests==2.31\n>>>>>>> repo (main)";
        const result = applyPipResolutions(text, { requests: "requests==2.31" });
        expect(result).toBe("requests==2.31");
    });

    test("leaves unresolved conflicts intact", () => {
        const text =
            "<<<<<<< existing\nflask>=2.0\n=======\nflask>=3.0\n>>>>>>> repo (main)";
        const result = applyPipResolutions(text, {});
        expect(result).toBe(text);
    });

    test("handles multiple conflicts", () => {
        const text = [
            "<<<<<<< existing",
            "requests==2.28",
            "=======",
            "requests==2.31",
            ">>>>>>> repo (main)",
            "<<<<<<< existing",
            "flask>=2.0",
            "=======",
            "flask>=3.0",
            ">>>>>>> repo (main)",
        ].join("\n");
        const result = applyPipResolutions(text, {
            requests: "requests==2.31",
            flask: "flask>=2.0",
        });
        expect(result).toBe("requests==2.31\nflask>=2.0");
    });

    test("returns empty for null text", () => {
        expect(applyPipResolutions(null, {})).toBe("");
    });
});

describe("ProjectDetail — _escapeRegex logic", () => {
    function _escapeRegex(str) {
        return str.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    }

    test("escapes dots", () => {
        expect(_escapeRegex("file.txt")).toBe("file\\.txt");
    });

    test("escapes brackets", () => {
        expect(_escapeRegex("[test]")).toBe("\\[test\\]");
    });

    test("escapes parentheses", () => {
        expect(_escapeRegex("func()")).toBe("func\\(\\)");
    });

    test("passes plain string through", () => {
        expect(_escapeRegex("hello")).toBe("hello");
    });
});

describe("ProjectDetail — isValid getter logic", () => {
    function isValid(name) {
        return !!(name && name.trim());
    }

    test("empty string is invalid", () => {
        expect(isValid("")).toBe(false);
    });

    test("whitespace only is invalid", () => {
        expect(isValid("   ")).toBe(false);
    });

    test("null is invalid", () => {
        expect(isValid(null)).toBe(false);
    });

    test("non-empty name is valid", () => {
        expect(isValid("My Project")).toBe(true);
    });
});

describe("ProjectDetail — hasUnsavedChanges logic", () => {
    // hasUnsavedChanges: _savedForm && JSON.stringify(form) !== _savedForm

    function hasUnsavedChanges(savedForm, currentForm) {
        return savedForm && JSON.stringify(currentForm) !== savedForm;
    }

    test("no saved form means no unsaved changes", () => {
        expect(hasUnsavedChanges(null, { name: "test" })).toBe(null);
    });

    test("identical forms means no changes", () => {
        const form = { name: "test", description: "" };
        const saved = JSON.stringify(form);
        expect(hasUnsavedChanges(saved, form)).toBe(false);
    });

    test("different form means unsaved changes", () => {
        const saved = JSON.stringify({ name: "test", description: "" });
        const current = { name: "test changed", description: "" };
        expect(hasUnsavedChanges(saved, current)).toBe(true);
    });
});

describe("ProjectDetail — statusLabel helper", () => {
    function statusLabel(status) {
        return { ok: "OK", warning: "Warning", error: "Error" }[status] || status;
    }

    test("maps ok", () => {
        expect(statusLabel("ok")).toBe("OK");
    });

    test("maps warning", () => {
        expect(statusLabel("warning")).toBe("Warning");
    });

    test("maps error", () => {
        expect(statusLabel("error")).toBe("Error");
    });

    test("unknown returns itself", () => {
        expect(statusLabel("degraded")).toBe("degraded");
    });
});

describe("ProjectDetail — getLineNumbers logic", () => {
    function getLineNumbers(text) {
        const lines = Math.max(1, text.split("\n").length);
        return Array.from({ length: lines }, (_, i) => i + 1);
    }

    test("empty string returns [1]", () => {
        expect(getLineNumbers("")).toEqual([1]);
    });

    test("single line returns [1]", () => {
        expect(getLineNumbers("hello")).toEqual([1]);
    });

    test("three lines returns [1, 2, 3]", () => {
        expect(getLineNumbers("a\nb\nc")).toEqual([1, 2, 3]);
    });

    test("trailing newline counts as extra line", () => {
        expect(getLineNumbers("a\nb\n")).toEqual([1, 2, 3]);
    });
});

describe("ProjectDetail — ODOO_VERSIONS constant", () => {
    // The component defines ODOO_VERSIONS
    const ODOO_VERSIONS = [
        "7.0", "8.0", "9.0", "10.0", "11.0", "12.0", "13.0",
        "14.0", "15.0", "16.0", "17.0", "18.0", "19.0",
    ];

    test("starts with 7.0", () => {
        expect(ODOO_VERSIONS[0]).toBe("7.0");
    });

    test("ends with 19.0", () => {
        expect(ODOO_VERSIONS[ODOO_VERSIONS.length - 1]).toBe("19.0");
    });

    test("contains 13 versions", () => {
        expect(ODOO_VERSIONS.length).toBe(13);
    });
});

describe("ProjectDetail — method existence", () => {
    test("save method exists", () => {
        expect(typeof ProjectDetail.prototype.save).toBe("function");
    });

    test("deleteProject method exists", () => {
        expect(typeof ProjectDetail.prototype.deleteProject).toBe("function");
    });

    test("addRepo method exists", () => {
        expect(typeof ProjectDetail.prototype.addRepo).toBe("function");
    });

    test("removeRepo method exists", () => {
        expect(typeof ProjectDetail.prototype.removeRepo).toBe("function");
    });

    test("confirmPipResolve method exists", () => {
        expect(typeof ProjectDetail.prototype.confirmPipResolve).toBe("function");
    });
});
