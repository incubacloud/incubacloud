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
 * (``requests``) is met by any other line of it (``requests==2.34.2``),
 * unless that line carries an environment marker (``;``) or a URL
 * (``@``), as the server decides. Every other difference still opens
 * the conflict dialog.
 */

const BARE_NAME_RE = /^[A-Za-z0-9_.-]+$/;
// A marker or a URL: a bare name neither meets it nor is met by it.
const MARKER_OR_URL_RE = /[;@]/;

/**
 * Which of two lines of one package meets both, when one is a bare name.
 *
 * @param {string} existing the line already in the list
 * @param {string} incoming the line a repository brings
 * @returns {"existing"|"incoming"|null} the line to keep, or null when
 *   neither is a bare name, or the other one has a marker or a URL, and
 *   the person decides
 */
export function bareNameRelation(existing, incoming) {
    const stored = existing.trim();
    const brought = incoming.trim();
    if (BARE_NAME_RE.test(brought)) {
        return MARKER_OR_URL_RE.test(stored) ? null : "existing";
    }
    if (BARE_NAME_RE.test(stored)) {
        return MARKER_OR_URL_RE.test(brought) ? null : "incoming";
    }
    return null;
}

/**
 * Replace the first line of *text* that *matches*.
 *
 * The editors found the stored line with ``^line$``, so a line with
 * blanks around it or a trailing comment (``requests  # needed by X``)
 * was not found and the text stayed as it was. The caller's *matches*
 * reads the line as its parser does, comment and blanks aside.
 *
 * @param {string} text the requirements text
 * @param {(line: string) => boolean} matches whether a line is the one
 * @param {string} replacement what the line becomes, written as it is
 * @param {boolean} [keepComment=false] keep the line's trailing comment
 *   after *replacement* (not for a conflict block, which ends in a label)
 * @returns {{text: string, replaced: boolean}} the new text, and whether
 *   a line was found
 */
export function replaceReqLine(text, matches, replacement, keepComment = false) {
    const lines = (text || "").split("\n");
    const index = lines.findIndex((line) => matches(line));
    if (index < 0) {
        return { text: text || "", replaced: false };
    }
    const hash = lines[index].indexOf("#");
    const comment = keepComment && hash >= 0 ? `  ${lines[index].slice(hash).trim()}` : "";
    lines[index] = replacement + comment;
    return { text: lines.join("\n"), replaced: true };
}
