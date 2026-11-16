import { Interaction } from "@web/public/interaction";
import { registry } from "@web/core/registry";

/**
 * Keeps ``aria-pressed`` of every show password button in step with the
 * field it toggles, so a screen reader announces whether the password is
 * shown. Odoo's own ``web.show_password`` interaction changes the type of
 * the input on the same click; this one only mirrors that toggle in the
 * button's state and changes nothing else.
 */
export class ShowPasswordPressed extends Interaction {
    static selector = ".input-group";
    static selectorHas = ":scope > .o_show_password";
    dynamicContent = {
        ".o_show_password": {
            "t-on-click": () => {
                this.pressed = !this.pressed;
            },
            "t-att-aria-pressed": () => String(!!this.pressed),
        },
    };
}

registry
    .category("public.interactions")
    .add("incubacloud.show_password_pressed", ShowPasswordPressed);
