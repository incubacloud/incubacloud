/** @odoo-module **/

/**
 * The editors' share of the server's rule on compatible pip lines.
 *
 * The server decides with ``packaging`` whether two lines of one
 * package can both hold (``pip_spec_relation`` in
 * ``models/_repo_requirements.py``). The project and instance editors
 * merge a repository's ``requirements.txt`` in the browser when it is
 * added; here they only spare the case that is certain without a
 * version parser: a line that names the package and nothing else
 * (``requests``) is met by any other line of it (``requests==2.34.2``).
 * Every other difference still opens the conflict dialog.
 */

const BARE_NAME_RE = /^[A-Za-z0-9_.-]+$/;

/**
 * Which of two lines of one package meets both, when one is a bare name.
 *
 * @param {string} existing the line already in the list
 * @param {string} incoming the line a repository brings
 * @returns {"existing"|"incoming"|null} the line to keep, or null when
 *   neither is a bare name and the person decides
 */
export function bareNameRelation(existing, incoming) {
    if (BARE_NAME_RE.test(incoming.trim())) return "existing";
    if (BARE_NAME_RE.test(existing.trim())) return "incoming";
    return null;
}
