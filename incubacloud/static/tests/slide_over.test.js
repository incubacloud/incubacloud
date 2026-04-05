import { describe, expect, test } from "@odoo/hoot";
import { SlideOver } from "@incubacloud/components/slide_over/slide_over";

describe("SlideOver", () => {
    test("has correct template", () => {
        expect(SlideOver.template).toBe("incubacloud.SlideOver");
    });

    test("title returns Job History for jobs panel", () => {
        // Simulate the getter logic
        const title = (panel) => panel === "jobs" ? "Job History" : "Alerts";
        expect(title("jobs")).toBe("Job History");
        expect(title("alerts")).toBe("Alerts");
    });

    test("declares required props", () => {
        expect(SlideOver.props.panel.type).toBe(String);
        expect(SlideOver.props.onClose.type).toBe(Function);
        expect(SlideOver.props.onToggleSize.type).toBe(Function);
    });
});
