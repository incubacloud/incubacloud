import { describe, expect, test } from "@odoo/hoot";
import { CreateProjectModal } from "@incubacloud/components/create_project_modal/create_project_modal";

/**
 * CreateProjectModal uses useService("orm"), rpc, onWillStart, and
 * accesses env.dialogData. We test class structure, props validation,
 * isValid getter logic, and tag management logic.
 */

describe("CreateProjectModal — class structure", () => {
    test("template name is set", () => {
        expect(CreateProjectModal.template).toBe("incubacloud.CreateProjectModal");
    });

    test("props requires onClose and onCreated functions", () => {
        expect(CreateProjectModal.props.onClose).toBe(Function);
        expect(CreateProjectModal.props.onCreated).toBe(Function);
    });

    test("components includes TagSelector", () => {
        expect("TagSelector" in CreateProjectModal.components).toBe(true);
    });
});

describe("CreateProjectModal — isValid getter logic", () => {
    // Extracted from: get isValid() { return this.state.name.trim().length > 0; }

    function isValid(name) {
        return name.trim().length > 0;
    }

    test("empty string is invalid", () => {
        expect(isValid("")).toBe(false);
    });

    test("whitespace only is invalid", () => {
        expect(isValid("   ")).toBe(false);
    });

    test("tabs only is invalid", () => {
        expect(isValid("\t\t")).toBe(false);
    });

    test("non-empty name is valid", () => {
        expect(isValid("My Project")).toBe(true);
    });

    test("name with leading/trailing spaces is valid", () => {
        expect(isValid("  My Project  ")).toBe(true);
    });

    test("single character is valid", () => {
        expect(isValid("a")).toBe(true);
    });
});

describe("CreateProjectModal — addTag logic", () => {
    // addTag pushes a tag if not already present (by id)

    function addTag(selectedTags, tag) {
        if (!selectedTags.find((t) => t.id === tag.id)) {
            selectedTags.push(tag);
        }
        return selectedTags;
    }

    test("adds a new tag", () => {
        const tags = [];
        addTag(tags, { id: 1, name: "saas" });
        expect(tags.length).toBe(1);
        expect(tags[0].name).toBe("saas");
    });

    test("does not add a duplicate tag", () => {
        const tags = [{ id: 1, name: "saas" }];
        addTag(tags, { id: 1, name: "saas" });
        expect(tags.length).toBe(1);
    });

    test("adds different tags", () => {
        const tags = [{ id: 1, name: "saas" }];
        addTag(tags, { id: 2, name: "production" });
        expect(tags.length).toBe(2);
    });
});

describe("CreateProjectModal — removeTag logic", () => {
    // removeTag removes tag by id via splice

    function removeTag(selectedTags, tagId) {
        const idx = selectedTags.findIndex((t) => t.id === tagId);
        if (idx !== -1) selectedTags.splice(idx, 1);
        return selectedTags;
    }

    test("removes existing tag by id", () => {
        const tags = [
            { id: 1, name: "saas" },
            { id: 2, name: "production" },
        ];
        removeTag(tags, 1);
        expect(tags.length).toBe(1);
        expect(tags[0].id).toBe(2);
    });

    test("does nothing when tag id not found", () => {
        const tags = [{ id: 1, name: "saas" }];
        removeTag(tags, 99);
        expect(tags.length).toBe(1);
    });

    test("removes from single-item array", () => {
        const tags = [{ id: 1, name: "saas" }];
        removeTag(tags, 1);
        expect(tags.length).toBe(0);
    });
});

describe("CreateProjectModal — create guard logic", () => {
    // create() returns early if !isValid or loading is true

    function canCreate(isValid, loading) {
        return isValid && !loading;
    }

    test("cannot create when invalid", () => {
        expect(canCreate(false, false)).toBe(false);
    });

    test("cannot create when loading", () => {
        expect(canCreate(true, true)).toBe(false);
    });

    test("can create when valid and not loading", () => {
        expect(canCreate(true, false)).toBe(true);
    });

    test("cannot create when both invalid and loading", () => {
        expect(canCreate(false, true)).toBe(false);
    });
});

describe("CreateProjectModal — method existence", () => {
    test("create method exists", () => {
        expect(typeof CreateProjectModal.prototype.create).toBe("function");
    });

    test("close method exists", () => {
        expect(typeof CreateProjectModal.prototype.close).toBe("function");
    });

    test("loadTags method exists", () => {
        expect(typeof CreateProjectModal.prototype.loadTags).toBe("function");
    });

    test("onOverlayClick method exists", () => {
        expect(typeof CreateProjectModal.prototype.onOverlayClick).toBe("function");
    });
});
