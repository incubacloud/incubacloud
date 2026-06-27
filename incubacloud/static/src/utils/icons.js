/**
 * Relay icon set — inline stroke SVGs ported 1:1 from the design prototype
 * (`prototipo-consola.html`, the `I` object). Rendered via `env.icon(name,size)`
 * so any template can use them without per-component registration:
 *
 *     <t t-out="env.icon('projects', 17)"/>
 *
 * All icons are 24x24, fill=none, stroke=currentColor (inherit text color),
 * round caps/joins. FontAwesome glyphs that have no equivalent here
 * (spinner, github, search, upload, copy, info-circle, …) keep using `<i class="fa">`.
 */
import { markup } from "@odoo/owl";

// Inner SVG markup per icon name (the design prototype's `I` set).
export const RELAY_ICONS = {
    projects: '<rect x="3" y="4" width="18" height="5" rx="1.5"/><rect x="3" y="11" width="18" height="5" rx="1.5"/><rect x="3" y="18" width="11" height="3" rx="1.5"/>',
    hosts: '<rect x="3" y="4" width="18" height="7" rx="2"/><rect x="3" y="13" width="18" height="7" rx="2"/><path d="M7 7.5h.01M7 16.5h.01"/>',
    backups: '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5"/><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
    settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-2.82 1.17V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 8 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.6 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9 1.65 1.65 0 0 0 4.27 7.18l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09A1.65 1.65 0 0 0 15 4.6a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
    instance: '<path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><path d="m3.3 7 8.7 5 8.7-5M12 22V12"/>',
    refresh: '<path d="M3 12a9 9 0 0 1 15-6.7L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-15 6.7L3 16"/><path d="M3 21v-5h5"/>',
    rocket: '<path d="M4.5 16.5c-1.5 1.3-2 5-2 5s3.7-.5 5-2c.7-.8.7-2 0-2.8a2 2 0 0 0-3 0z"/><path d="M12 15 9 12a14 14 0 0 1 8-9 14 14 0 0 1-2 8 14 14 0 0 1-3 4z"/><path d="M9 12H4s.5-2.8 2-4 5 0 5 0M12 15v5s2.8-.5 4-2 0-5 0-5"/>',
    play: '<path d="M6 4v16l13-8z"/>',
    stop: '<rect x="5" y="5" width="14" height="14" rx="2"/>',
    login: '<path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/><path d="M10 17l5-5-5-5M15 12H3"/>',
    terminal: '<path d="m4 17 6-5-6-5"/><path d="M12 19h8"/>',
    shell: '<polyline points="4 7 9 12 4 17"/><line x1="12" y1="17" x2="20" y2="17"/>',
    db: '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5"/>',
    download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5M12 15V3"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    move: '<polyline points="5 9 2 12 5 15"/><polyline points="9 5 12 2 15 5"/><polyline points="15 19 12 22 9 19"/><polyline points="19 9 22 12 19 15"/><line x1="2" y1="12" x2="22" y2="12"/><line x1="12" y1="2" x2="12" y2="22"/>',
    trash: '<path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>',
    check: '<path d="M20 6 9 17l-5-5"/>',
    back: '<path d="m15 18-6-6 6-6"/>',
    fwd: '<path d="m9 18 6-6-6-6"/>',
    alert: '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    git: '<circle cx="12" cy="12" r="3"/><path d="M12 9V3M12 21v-6"/>',
    x: '<path d="M18 6 6 18M6 6l12 12"/>',
    lock: '<rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
    tenants: '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/>',
    plans: '<path d="M6 3h12l4 6-10 13L2 9z"/><path d="M2 9h20M12 3 8 9l4 13 4-13-4-6"/>',
    pools: '<rect x="3" y="4" width="18" height="7" rx="2"/><rect x="3" y="13" width="18" height="7" rx="2"/><path d="M7 7.5h.01M7 16.5h.01"/>',
    announce: '<path d="M3 11l18-5v12L3 14v-3z"/><path d="M11.6 16.8a3 3 0 1 1-5.8-1.6"/>',
    providers: '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
    user: '<path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
    card: '<rect x="2" y="5" width="20" height="14" rx="2"/><path d="M2 10h20"/>',
    key: '<path d="M21 2l-2 2m-7.6 7.6a5 5 0 1 0-7 7 5 5 0 0 0 7-7zm0 0L15 8l3 3 3-3-3-3"/>',
    shield: '<path d="M12 2 4 5v6c0 5.5 3.8 8.5 8 10 4.2-1.5 8-4.5 8-10V5z"/>',
    unlock: '<rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 9.9-1"/>',
    arrowdown: '<path d="M12 5v14"/><path d="M19 12l-7 7-7-7"/>',
    flask: '<path d="M9 3h6M10 3v6l-5 9a2 2 0 0 0 1.8 3h10.4a2 2 0 0 0 1.8-3l-5-9V3z"/>',
};

// Aliases so callers can use intuitive / FontAwesome-ish names too.
const ALIASES = {
    folder: "projects", "folder-o": "projects",
    server: "hosts", cog: "settings", gear: "settings",
    cube: "instance", cubes: "instance",
    database: "db", times: "x", close: "x",
    "arrow-left": "back", "arrow-right": "fwd", "caret-right": "fwd",
    "arrow-down": "arrowdown", "chevron-left": "back", "chevron-right": "fwd",
    "sign-in": "login", "sign-out": "login",
    "unlock-alt": "unlock", "clock-o": "clock", history: "clock",
    "user-circle-o": "user", "user-circle": "user", users: "tenants",
    diamond: "plans", exchange: "move", bullhorn: "announce",
    "exclamation-triangle": "alert", "exclamation-circle": "alert", warning: "alert",
    "check-circle": "check", "times-circle": "x",
    "plus-circle": "plus", wrench: "settings", code: "shell",
    heartbeat: "refresh", recycle: "refresh", undo: "back",
};

/**
 * Return inline SVG markup for an icon name (Relay set; FontAwesome-ish aliases
 * resolved). Falls back to the `x` icon if unknown. Output is OWL `markup()`
 * so it renders raw via `t-out`.
 * @param {string} name icon key
 * @param {number|string} [size=18] width/height in px
 * @param {number|string} [stroke=2] stroke width
 * @returns {ReturnType<typeof markup>} raw SVG markup
 */
export function relayIcon(name, size = 18, stroke = 2) {
    const key = RELAY_ICONS[name] ? name : (ALIASES[name] || "x");
    const inner = RELAY_ICONS[key] || RELAY_ICONS.x;
    return markup(
        `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" ` +
        `stroke="currentColor" stroke-width="${stroke}" stroke-linecap="round" ` +
        `stroke-linejoin="round" class="ic-svg" aria-hidden="true">${inner}</svg>`
    );
}
