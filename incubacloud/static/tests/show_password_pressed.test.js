import { startInteractions, setupInteractionWhiteList } from "@web/../tests/public/helpers";
import { describe, expect, test } from "@odoo/hoot";
import { click, press, queryOne } from "@odoo/hoot-dom";

setupInteractionWhiteList(["web.show_password", "incubacloud.show_password_pressed"]);

describe.current.tags("interaction_dev");

const template = `
    <div class="input-group">
        <input type="password" id="password" class="form-control" name="password"/>
        <button type="button" class="btn o_show_password" aria-label="Show password">
            <i class="fa fa-eye" aria-hidden="true"></i>
        </button>
    </div>
`;

test("the button starts not pressed and keeps its label", async () => {
    await startInteractions(template);
    expect(".o_show_password").toHaveAttribute("aria-pressed", "false");
    expect(".o_show_password").toHaveAttribute("aria-label", "Show password");
});

test("a click presses it while the password is shown, and another releases it", async () => {
    await startInteractions(template);
    const button = queryOne(".o_show_password");
    await click(button);
    expect("input").toHaveAttribute("type", "text");
    expect(button).toHaveAttribute("aria-pressed", "true");
    await click(button);
    expect("input").toHaveAttribute("type", "password");
    expect(button).toHaveAttribute("aria-pressed", "false");
});

test("the keyboard toggles it too", async () => {
    await startInteractions(template);
    const button = queryOne(".o_show_password");
    button.focus();
    await press("Enter");
    expect("input").toHaveAttribute("type", "text");
    expect(button).toHaveAttribute("aria-pressed", "true");
});
