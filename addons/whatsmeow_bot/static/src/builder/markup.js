/** @odoo-module **/

/**
 * Draw a step's body the way a phone would.
 *
 * A deliberate copy of the grammar `whatsmeow_template`'s preview uses, rather
 * than an import of it: that module is optional, and a chatbot designer that
 * only renders its bubbles when an unrelated add-on happens to be installed is
 * a worse trade than thirty lines duplicated. The rules are WhatsApp's and do
 * not move.
 *
 * Display-only and one-way — nothing is ever read back out of the preview — so
 * where this disagrees with WhatsApp's own parser the phone wins and the
 * preview is slightly wrong, which costs nothing.
 *
 * The output is built from escaped text and a closed set of tags, so a body
 * cannot smuggle HTML onto the designer's screen.
 */

const PLACEHOLDER_RE = /\{\{[\s\S]*?\}\}/g;
const INLINE_RE = /([*_~])(?=\S)((?:(?!\1)[^\n])*?\S)\1/;
const INLINE_TAGS = { "*": "strong", _: "em", "~": "s" };

// Placeholders are lifted out before the inline pass and put back after it, so
// the marker inside `{{ answer.a_b }}` cannot start an italic run that swallows
// the rest of the line. The sentinel is bracketed and escaped-safe: it survives
// `escapeHtml` unchanged and nothing a human types looks like it.
const SENTINEL_OPEN = "@@WAPH";
const SENTINEL_CLOSE = "@@";
const SENTINEL_RE = /@@WAPH(\d+)@@/g;

function escapeHtml(text) {
    return text
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
}

function lineBreaks(text) {
    return text.replace(/\n/g, "<br/>");
}

/** Wrap every inline mark, recursing so `*bold _and italic_*` nests. */
function renderInline(text) {
    const match = INLINE_RE.exec(text);
    if (!match) {
        return text;
    }
    const tag = INLINE_TAGS[match[1]];
    const before = text.slice(0, match.index);
    const after = text.slice(match.index + match[0].length);
    return `${before}<${tag}>${renderInline(match[2])}</${tag}>${renderInline(after)}`;
}

export function renderPreview(text) {
    if (!text) {
        return "";
    }
    const placeholders = [];
    let masked = text.replace(PLACEHOLDER_RE, (match) => {
        placeholders.push(match);
        return `${SENTINEL_OPEN}${placeholders.length - 1}${SENTINEL_CLOSE}`;
    });
    masked = escapeHtml(masked);

    // Monospace is block-ish: it spans newlines and suppresses everything
    // inside, so the fences are split off before any inline mark is looked for.
    const chunks = masked.split("```");
    let html = "";
    for (const [index, chunk] of chunks.entries()) {
        const isCode = index % 2 === 1 && index < chunks.length - 1;
        if (isCode) {
            html += `<code>${lineBreaks(chunk)}</code>`;
        } else {
            // An odd last chunk had an opening fence with nothing closing it.
            // `split` ate that fence; it goes back as the literal text a phone
            // would show.
            const opener = index % 2 === 1 ? "```" : "";
            html += opener + lineBreaks(renderInline(chunk));
        }
    }
    return html.replace(
        SENTINEL_RE,
        (match, index) =>
            placeholders[index] === undefined
                ? match
                : `<span class="o_wa_ph">${escapeHtml(placeholders[index])}</span>`
    );
}
