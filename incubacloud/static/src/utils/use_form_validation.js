/**
 * useFormValidation — tiny hook that runs a rules map against a form
 * object, tracks per-field touched state, and exposes helpers for
 * the template to render error messages and ``.has-error`` classes.
 *
 * Why this exists:
 *   Before this hook, forms either returned a boolean (``isValid``
 *   getter — silent failure on save) or an array of missing labels
 *   (toast "Required fields missing: Name, Port"). Neither marked
 *   the offending inputs red, neither checked formats, and every
 *   component hand-rolled its own validation. Ambition for P1.19 is
 *   to unify the pattern, add per-field visual feedback, and plug
 *   in format validators (email, port, hostname, slug…).
 *
 * Rules shape:
 *   A function ``() => { fieldName: [validator, ...] }`` — passed as
 *   a factory rather than a plain object so consumers can branch on
 *   component state (e.g. ``this.isNew ? [required()] : []``) and
 *   have the rules re-evaluated on every validate() call.
 *
 *   Each validator is ``(value, form) => errorMessage | null``.
 *   See ``utils/validators.js`` for the bundled ones.
 *
 * API:
 *   - ``validate(form)`` → ``{errors, isValid, firstError}``. Runs
 *     every rule against the passed form, stores the errors map in
 *     reactive state, and marks every rule-bearing field as
 *     "touched" so the template renders red borders immediately.
 *   - ``fieldError(name)`` → ``string | null``. Only non-null once a
 *     field has been touched (explicit ``touch(name)`` or via a
 *     failed ``validate()``). Safe to call from the template for
 *     every render.
 *   - ``touch(name)`` → mark a single field as touched (e.g. from a
 *     ``t-on-blur`` handler) so its error, if any, becomes visible
 *     without waiting for a save attempt.
 *   - ``clearErrors()`` → wipe errors and touched. Call after a
 *     successful save or load so stale flags don't persist across
 *     form reloads.
 */
import { useState } from "@odoo/owl";

export function useFormValidation(rulesFactory) {
    const state = useState({
        errors: {},     // { field: message }
        touched: {},    // { field: true }
    });

    function _run(form) {
        const rules = rulesFactory() || {};
        const errors = {};
        for (const [field, validators] of Object.entries(rules)) {
            for (const v of validators) {
                const err = v(form?.[field], form);
                if (err) { errors[field] = err; break; }
            }
        }
        return { rules, errors };
    }

    function validate(form) {
        const { rules, errors } = _run(form);
        state.errors = errors;
        // Mark every rule-bearing field as touched so the template
        // shows red borders even for fields the user never touched.
        const touched = {};
        for (const name of Object.keys(rules)) touched[name] = true;
        state.touched = touched;
        const keys = Object.keys(errors);
        return {
            errors,
            isValid: keys.length === 0,
            firstError: keys.length ? errors[keys[0]] : null,
        };
    }

    function touch(name) {
        state.touched[name] = true;
    }

    function fieldError(name) {
        if (!state.touched[name]) return null;
        return state.errors[name] || null;
    }

    function clearErrors() {
        state.errors = {};
        state.touched = {};
    }

    return { validate, touch, fieldError, clearErrors };
}
