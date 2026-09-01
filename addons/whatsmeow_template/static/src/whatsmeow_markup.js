/** @odoo-module **/

/**
 * WhatsApp text markers, as a pure string operation.
 *
 * WhatsApp's styles are a client-side *text* convention: the gateway sends the
 * body verbatim and the recipient's phone parses `*bold*`, `_italic_`,
 * `~strike~` and ``` monospace ```. So nothing here talks to the server, and
 * nothing here models the text as HTML — there is exactly one representation,
 * the raw markup the operator can see in the textarea (PLAN.md §11.8).
 *
 * Everything the feature knows lives in `applyMark`, which is why it is a
 * standalone module: it is unit-testable without mounting the widget.
 *
 * The rules below are WhatsApp's, not a tidy string operation. Wrapping the
 * selection verbatim is the naive version and it is wrong wherever WhatsApp's
 * own parser disagrees — the toolbar must only ever emit sequences that render.
 */

/**
 * The marks WhatsApp actually has. This is a whitelist, deliberately: Odoo's
 * own format group ships underline next to bold/italic, and WhatsApp has no
 * underline — offering it would let an author apply a style that silently
 * vanishes on the recipient's phone. Same for links: WhatsApp auto-links bare
 * URLs but has no anchor text.
 *
 * Monospace is the odd one out. It is block-ish rather than inline: it spans
 * newlines and suppresses the other styles inside it, so it neither snaps to
 * word boundaries nor splits per line, and the inline buttons disable while the
 * caret sits inside one.
 */
export const MARKS = {
    bold: { marker: "*", icon: "fa-bold" },
    italic: { marker: "_", icon: "fa-italic" },
    strikethrough: { marker: "~", icon: "fa-strikethrough" },
    monospace: { marker: "```", icon: "fa-code", block: true },
};

/**
 * Characters that begin a mark. A marker is never part of a word, so word
 * snapping stops on one — otherwise applying bold next to an existing italic
 * would swallow the italic's marker into the range.
 */
const MARKER_CHARS = new Set(["*", "_", "~", "`"]);

/**
 * `{{ object.name }}` — matched non-greedily and across newlines, mirroring
 * `INLINE_TEMPLATE_REGEX` in odoo/tools/rendering_tools.py. A marker landing
 * inside one of these produces `{{ object.*na*me }}`, which does not break
 * WhatsApp — it breaks `inline_template` at render time, which is worse.
 */
const PLACEHOLDER_RE = /\{\{[\s\S]*?\}\}/g;

function isWordChar(ch) {
    return ch !== undefined && !/\s/.test(ch) && !MARKER_CHARS.has(ch);
}

/** Every `{{ … }}` span in `text`, as [start, end) pairs. */
function placeholderRanges(text) {
    const ranges = [];
    for (const match of text.matchAll(PLACEHOLDER_RE)) {
        ranges.push([match.index, match.index + match[0].length]);
    }
    return ranges;
}

/**
 * Shrink inward past leading and trailing whitespace.
 *
 * A marker with whitespace immediately inside it does not render: `*text *`
 * arrives as literal asterisks. This is the rule that actually bites, because
 * double-clicking a word usually takes the trailing space with it.
 */
function trimRange(text, start, end) {
    while (start < end && /\s/.test(text[start])) {
        start++;
    }
    while (end > start && /\s/.test(text[end - 1])) {
        end--;
    }
    return [start, end];
}

/**
 * Expand to whole words.
 *
 * Mid-word markers are unreliable — a marker placed inside a word can arrive as
 * a literal character instead of styling anything. So an arbitrary sub-word
 * selection cannot be honoured by wrapping it, and the contract here is "wrap
 * the smallest renderable range containing the selection" rather than "wrap
 * what the user selected".
 */
function snapToWords(text, start, end) {
    while (isWordChar(text[start - 1])) {
        start--;
    }
    while (isWordChar(text[end])) {
        end++;
    }
    return [start, end];
}

/**
 * Drop the mark's own markers when the selection already contains them.
 *
 * The selection this function returns spans the markers, so that bold→italic
 * nests; the cost is that bold→bold hands us back `*hello*` rather than
 * `hello`, and the toggle check below only looks *outside* the range. Without
 * shedding them here, clicking bold twice would emit `**hello**` — which is not
 * "extra bold", it arrives as literal asterisks.
 */
function shedOwnMarkers(text, start, end, marker) {
    const len = marker.length;
    while (
        end - start >= 2 * len &&
        text.slice(start, start + len) === marker &&
        text.slice(end - len, end) === marker
    ) {
        start += len;
        end -= len;
    }
    return [start, end];
}

/** Push either edge that landed strictly inside a `{{ … }}` out to its brace. */
function snapOutOfPlaceholders(text, start, end) {
    for (const [from, to] of placeholderRanges(text)) {
        if (start > from && start < to) {
            start = from;
        }
        if (end > from && end < to) {
            end = to;
        }
    }
    return [start, end];
}

/**
 * The smallest range containing [start, end) that WhatsApp will actually
 * render. The three snaps feed each other — expanding to a word boundary can
 * land inside a placeholder, and leaving a placeholder can land mid-word — so
 * they run to a fixed point. It terminates because every snap but the trim only
 * expands, and the trim cannot undo an expansion over non-whitespace.
 */
function renderableRange(text, start, end, marker, block) {
    let [from, to] = [start, end];
    for (;;) {
        const [wasFrom, wasTo] = [from, to];
        [from, to] = trimRange(text, from, to);
        [from, to] = shedOwnMarkers(text, from, to, marker);
        if (from >= to) {
            // The selection held nothing but whitespace and markers.
            return null;
        }
        [from, to] = snapOutOfPlaceholders(text, from, to);
        if (!block) {
            [from, to] = snapToWords(text, from, to);
        }
        if (from === wasFrom && to === wasTo) {
            return [from, to];
        }
    }
}

/**
 * Split [start, end) on newlines.
 *
 * Markers do not reliably span a line break, so an inline mark is applied once
 * per line rather than once across the selection. Blank lines drop out.
 */
function lineSegments(text, start, end) {
    const segments = [];
    let from = start;
    while (from < end) {
        const br = text.indexOf("\n", from);
        const to = br === -1 || br >= end ? end : br;
        if (to > from) {
            segments.push([from, to]);
        }
        from = to + 1;
    }
    return segments;
}

/** True when `marker` already sits immediately outside [start, end). */
function isWrapped(text, start, end, marker) {
    return (
        start >= marker.length &&
        text.slice(start - marker.length, start) === marker &&
        text.slice(end, end + marker.length) === marker
    );
}

/**
 * True when `index` falls inside a ``` … ``` run.
 *
 * Monospace suppresses the other styles, so the inline buttons are meaningless
 * there and disable. Counting fences is enough: an odd number before the caret
 * means the caret is inside an open one.
 */
export function isInsideMonospace(text, index) {
    let fences = 0;
    let at = text.indexOf("```");
    while (at !== -1 && at < index) {
        fences++;
        at = text.indexOf("```", at + 3);
    }
    return fences % 2 === 1;
}

/**
 * Render `text` the way a phone would, as an HTML string.
 *
 * This is display-only and deliberately one-way: the markup stays the single
 * canonical representation (see the widget's header comment), nothing round
 * trips back out of the preview. So the renderer only has to be
 * approximately right — where it disagrees with WhatsApp's own parser the
 * phone wins and the preview is a little wrong, which costs nothing, whereas
 * a second *editable* representation would cost the whole design.
 *
 * The output is built from escaped text and a fixed set of tags, so an
 * operator cannot smuggle HTML in through the body.
 */
export function renderPreview(text) {
    if (!text) {
        return "";
    }

    // Placeholders come out first, as sentinels. `{{ object.partner_id.name }}`
    // twice in a body puts two underscores on the same line, and WhatsApp would
    // happily italicise everything between them — true to the wire, but it
    // would render a *template* as garbage on screen for a reason the author
    // cannot act on. Same instinct as the toolbar refusing to mark inside one.
    const placeholders = [];
    let masked = text.replace(PLACEHOLDER_RE, (match) => {
        placeholders.push(match);
        return `\u0000${placeholders.length - 1}\u0000`;
    });
    masked = escapeHtml(masked);

    // Monospace is a block: it spans newlines and suppresses everything inside,
    // so the fences are split off before any inline mark is looked for. An
    // unterminated trailing fence is left as literal text, which is what the
    // phone does too.
    const chunks = masked.split("```");
    let html = "";
    for (const [index, chunk] of chunks.entries()) {
        const isCode = index % 2 === 1 && index < chunks.length - 1;
        if (isCode) {
            html += `<code>${lineBreaks(chunk)}</code>`;
        } else {
            // An odd chunk that is also the last one had an opening fence with
            // nothing to close it. `split` ate that fence, so it goes back as
            // the literal text the phone would show.
            const opener = index % 2 === 1 ? "```" : "";
            html += opener + lineBreaks(renderInline(chunk));
        }
    }

    return html.replace(/\u0000(\d+)\u0000/g, (_match, index) =>
        `<span class="o_whatsmeow_placeholder">${escapeHtml(placeholders[index])}</span>`
    );
}

/** The inline marks, innermost last: each match recurses into its own content. */
const INLINE_RE = /([*_~])(?=\S)((?:(?!\1)[^\n])*?\S)\1/;

const INLINE_TAGS = { "*": "strong", _: "em", "~": "s" };

/**
 * Wrap every inline mark in `text`, recursing so `*bold _and italic_*` nests.
 *
 * A run is bounded to one line and cannot contain its own marker, mirroring
 * `applyMark`'s rules: those are the sequences the toolbar emits, so this is
 * the same grammar read back rather than a second, looser one.
 */
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

/**
 * Apply (or remove) a mark around a selection.
 *
 * @param {string} text the whole body
 * @param {number} start selection start offset
 * @param {number} end selection end offset
 * @param {string} markName a key of {@link MARKS}
 * @returns {{value: string, selectionStart: number, selectionEnd: number}|null}
 *  the new body and the selection to restore, or null when the selection holds
 *  nothing that can be marked. The returned selection spans the markers too, so
 *  clicking bold then italic nests instead of losing the range.
 */
export function applyMark(text, start, end, markName) {
    const mark = MARKS[markName];
    if (!mark) {
        throw new Error(`Unknown WhatsApp mark: ${markName}`);
    }
    const { marker, block } = mark;
    if (start > end) {
        [start, end] = [end, start];
    }
    if (start === end) {
        // Nothing selected. The toolbar only appears on a selection, so this is
        // reachable only if the range collapsed between show and click.
        return null;
    }

    // A block mark spans newlines by design; an inline one is applied per line.
    const raw = block ? [[start, end]] : lineSegments(text, start, end);
    const segments = [];
    for (const [from, to] of raw) {
        const range = renderableRange(text, from, to, marker, block);
        if (range) {
            segments.push(range);
        }
    }
    if (!segments.length) {
        return null;
    }

    // Toggle, don't stack: doubled markers are not "extra bold", they arrive as
    // literal characters. Different marks still nest — only this one is undone.
    const unwrap = segments.every(([from, to]) => isWrapped(text, from, to, marker));

    let value = "";
    let cursor = 0;
    let selectionStart = null;
    let selectionEnd = null;
    for (const [from, to] of segments) {
        if (unwrap) {
            value += text.slice(cursor, from - marker.length);
            selectionStart ??= value.length;
            value += text.slice(from, to);
            cursor = to + marker.length;
        } else {
            value += text.slice(cursor, from);
            selectionStart ??= value.length;
            value += marker + text.slice(from, to) + marker;
            cursor = to;
        }
        selectionEnd = value.length;
    }
    value += text.slice(cursor);

    return { value, selectionStart, selectionEnd };
}
