/**
 * Tests for IcConfirmDialog's initial focus.
 *
 * The dialog is where an irreversible action gets its last check, and
 * until now it opened with Confirm focused for muscle memory — which
 * means an Enter pressed before the text was read confirmed a delete.
 * Destructive dialogs now open on Cancel, so that same reflex backs
 * out; everything else keeps Confirm, because making every ordinary
 * confirmation cost an extra Tab teaches people to Tab-Enter blindly
 * and dissolves the barrier where it matters.
 *
 * Why no mounting: ``mountWithCleanup`` pulls in the bus/discuss/mail
 * mock stack (see use_safe_poll.test.js) for a decision that is one
 * boolean. ``initialFocusTarget`` is static and pure precisely so the
 * contract can be asserted directly.
 */
import { describe, expect, test } from "@odoo/hoot";

import { IcConfirmDialog } from "@incubacloud/components/ic_confirm_dialog/ic_confirm_dialog";

const CANCEL = { id: "cancel" };
const CONFIRM = { id: "confirm" };

describe("IcConfirmDialog initial focus", () => {
    test("a destructive dialog opens focused on Cancel", () => {
        expect(
            IcConfirmDialog.initialFocusTarget(true, CANCEL, CONFIRM)
        ).toBe(CANCEL);
    });

    test("an ordinary dialog opens focused on Confirm", () => {
        expect(
            IcConfirmDialog.initialFocusTarget(false, CANCEL, CONFIRM)
        ).toBe(CONFIRM);
    });

    test("isDanger omitted is not destructive", () => {
        // ``isDanger`` is an optional prop, so undefined is the common
        // case for every non-destructive consumer.
        expect(
            IcConfirmDialog.initialFocusTarget(undefined, CANCEL, CONFIRM)
        ).toBe(CONFIRM);
    });

    test("isDanger stays an optional boolean prop", () => {
        expect(IcConfirmDialog.props.isDanger.type).toBe(Boolean);
        expect(IcConfirmDialog.props.isDanger.optional).toBe(true);
    });
});
