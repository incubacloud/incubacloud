/**
 * Composable form-field validators.
 *
 * Contract: each validator is a zero-argument factory that returns a
 * function ``(value, form?) => errorMessage | null``. Returning
 * ``null`` means the value passes. The factory shape lets consumers
 * parameterize messages at definition time:
 *
 *     required()
 *     required(_t("Host name is required"))
 *     portRange()  // → default 1-65535 message
 *
 * All validators are null/empty-tolerant by default EXCEPT
 * ``required()``. This lets callers chain ``[required(), email()]``
 * without duplicate empty-string checks: ``email`` passes on empty
 * and ``required`` catches it first.
 *
 * Keep these framework-agnostic: no OWL imports, no ``_t`` wiring.
 * Translation happens at the consumer call site.
 */

export const required = (msg = "This field is required") =>
    (v) => (v == null || String(v).trim() === "") ? msg : null;

export const email = (msg = "Invalid email address") =>
    (v) => {
        if (v == null || String(v).trim() === "") return null;
        // Deliberately permissive: server-side checks RFC edge cases;
        // the client just catches obvious typos so the round-trip
        // doesn't waste time on "asdf" or "foo@".
        return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(String(v).trim())
            ? null : msg;
    };

export const portRange = (msg = "Port must be between 1 and 65535") =>
    (v) => {
        if (v == null || String(v).trim() === "") return null;
        const n = Number(v);
        return (Number.isInteger(n) && n >= 1 && n <= 65535) ? null : msg;
    };

/**
 * Accept either an IPv4 dotted-quad or a DNS-style hostname. Not a
 * strict RFC check — IPs like ``999.0.0.0`` slip through the pattern
 * but get rejected server-side when asyncssh tries to connect; we
 * just block clearly-bogus input like ``1 2 3`` or empty segments.
 */
export const hostname = (msg = "Invalid hostname or IP address") =>
    (v) => {
        if (v == null || String(v).trim() === "") return null;
        const s = String(v).trim();
        const ipv4 = /^(\d{1,3}\.){3}\d{1,3}$/;
        const dns = /^(?=.{1,253}$)[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*$/;
        return (ipv4.test(s) || dns.test(s)) ? null : msg;
    };

/**
 * Lowercase slug: letters, digits, and hyphens only. Used for
 * URL-safe identifiers (instance names, tenant slugs, project
 * codes).
 */
export const slug = (
    msg = "Only lowercase letters, digits, and hyphens allowed",
) => (v) => {
    if (v == null || String(v).trim() === "") return null;
    return /^[a-z0-9-]+$/.test(String(v).trim()) ? null : msg;
};

/**
 * Instance-style identifier: starts with a letter or digit, then
 * lowercase letters, digits, underscores and hyphens up to 63 chars.
 * Mirrors the server-side ``_INSTANCE_NAME_RE`` constraint.
 */
export const instanceName = (
    msg = "Must start with a letter or digit and contain only lowercase letters, digits, hyphens and underscores",
) => (v) => {
    if (v == null || String(v).trim() === "") return null;
    return /^[a-z0-9][a-z0-9_-]{0,62}$/.test(String(v).trim()) ? null : msg;
};

export const url = (msg = "Invalid URL") =>
    (v) => {
        if (v == null || String(v).trim() === "") return null;
        try { new URL(String(v).trim()); return null; }
        catch (_e) { return msg; }
    };

/**
 * Retention string in duplicity format: ``<count><unit>`` where unit
 * is ``D`` (days), ``M`` (months), or ``Y`` (years).
 */
export const retention = (
    msg = "Use N + D/M/Y (e.g. 30D, 3M, 1Y)",
) => (v) => {
    if (v == null || String(v).trim() === "") return null;
    return /^\d+[DMY]$/.test(String(v).trim()) ? null : msg;
};

export const minLength = (n, msg = null) =>
    (v) => {
        if (v == null || String(v).trim() === "") return null;
        const m = msg || `Must be at least ${n} characters`;
        return String(v).length >= n ? null : m;
    };

export const maxLength = (n, msg = null) =>
    (v) => {
        if (v == null) return null;
        const m = msg || `Must be at most ${n} characters`;
        return String(v).length <= n ? null : m;
    };

/**
 * Guard that runs the provided validators only when ``predicate(form)``
 * is truthy. Used for conditional fields (e.g. password required only
 * when ``login_type === 'password'``).
 */
export const when = (predicate, ...validators) =>
    (v, form) => {
        if (!predicate(form)) return null;
        for (const vf of validators) {
            const err = vf(v, form);
            if (err) return err;
        }
        return null;
    };
