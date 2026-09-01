/** @odoo-module **/

import { applyMark, isInsideMonospace, renderPreview } from "@whatsmeow_template/whatsmeow_markup";

/**
 * The whole of §11.8 lives in `applyMark`, so the whole of it is testable
 * without mounting the widget — which is the reason the wrap function is a
 * standalone module in the first place.
 *
 * Each case here is a rule WhatsApp's parser imposes, not a string-manipulation
 * preference: every expectation is a sequence that renders on a phone, and the
 * naive "wrap the selection" would get several of them wrong.
 */

/** Apply a mark and return just the resulting body. */
function mark(text, start, end, name) {
    const result = applyMark(text, start, end, name);
    return result === null ? null : result.value;
}

/** Apply a mark and return the text the widget would leave selected. */
function selectionAfter(text, start, end, name) {
    const result = applyMark(text, start, end, name);
    return result === null ? null : result.value.slice(result.selectionStart, result.selectionEnd);
}

QUnit.module("whatsmeow_template", {}, function () {
QUnit.module("whatsmeow_markup");

// -- whitespace inside markers --
QUnit.test("wraps a clean selection", function (assert) {
    assert.deepEqual(mark("hello world", 0, 5, "bold"), "*hello* world");
});

QUnit.test("shrinks past a trailing space", function (assert) {
    // `*hello *` arrives as literal asterisks. Double-clicking a word
    // usually takes the trailing space, so this is the common case.
    assert.deepEqual(mark("hello world", 0, 6, "bold"), "*hello* world");
});

QUnit.test("shrinks past a leading space", function (assert) {
    assert.deepEqual(mark("a hello b", 1, 8, "bold"), "a *hello* b");
});

QUnit.test("refuses a selection of only whitespace", function (assert) {
    assert.deepEqual(mark("a   b", 1, 4, "bold"), null);
});

QUnit.test("refuses a collapsed selection", function (assert) {
    assert.deepEqual(mark("hello", 2, 2, "bold"), null);
});

// -- word boundaries --
QUnit.test("expands a sub-word selection out to the whole word", function (assert) {
    // Mid-word markers are unreliable, so the contract is "the smallest
    // renderable range containing the selection", not "what was selected".
    assert.deepEqual(mark("hello", 1, 3, "bold"), "*hello*");
});

QUnit.test("does not swallow an adjacent mark's marker", function (assert) {
    assert.deepEqual(mark("*a* b", 4, 5, "italic"), "*a* _b_");
});

// -- toggling --
QUnit.test("unwraps when the markers sit just outside the selection", function (assert) {
    assert.deepEqual(mark("*hello* world", 1, 6, "bold"), "hello world");
});

QUnit.test("unwraps when the selection contains its own markers", function (assert) {
    // This is the shape the widget hands back after wrapping, so without it
    // a second click on bold would emit `**hello**` — literal asterisks.
    assert.deepEqual(mark("*hello* world", 0, 7, "bold"), "hello world");
});

QUnit.test("round-trips across repeated clicks", function (assert) {
    let [value, start, end] = ["hello world", 0, 5];
    const seen = [];
    for (let n = 0; n < 4; n++) {
        ({ value, selectionStart: start, selectionEnd: end } = applyMark(
            value, start, end, "bold"
        ));
        seen.push(value);
    }
    assert.deepEqual(seen, [
        "*hello* world", "hello world", "*hello* world", "hello world",
    ]);
});

QUnit.test("nests a different mark rather than replacing it", function (assert) {
    assert.deepEqual(mark("*hello* world", 0, 7, "italic"), "_*hello*_ world");
});

// -- line breaks --
QUnit.test("applies an inline mark once per line", function (assert) {
    // Markers do not reliably span a newline.
    assert.deepEqual(mark("one\ntwo", 0, 7, "bold"), "*one*\n*two*");
});

QUnit.test("skips blank lines", function (assert) {
    assert.deepEqual(mark("one\n\ntwo", 0, 8, "bold"), "*one*\n\n*two*");
});

// -- placeholders --
QUnit.test("snaps an edge landing inside {{ }} out to the brace", function (assert) {
    // `{{ object.*na*me }}` does not break WhatsApp, it breaks
    // inline_template at render time.
    assert.deepEqual(mark("{{ object.name }}", 3, 9, "bold"), "*{{ object.name }}*");
});

QUnit.test("expands a selection that ends inside a placeholder", function (assert) {
    const text = "Hi {{ object.name }} there";
    assert.deepEqual(mark(text, 0, 15, "bold"), "*Hi {{ object.name }}* there");
});

// -- monospace --
QUnit.test("spans newlines instead of splitting per line", function (assert) {
    assert.deepEqual(mark("one\ntwo", 0, 7, "monospace"), "```one\ntwo```");
});

QUnit.test("does not snap to word boundaries", function (assert) {
    assert.deepEqual(mark("hello", 1, 3, "monospace"), "h```el```lo");
});

QUnit.test("toggles off", function (assert) {
    assert.deepEqual(mark("```one\ntwo```", 3, 10, "monospace"), "one\ntwo");
});

QUnit.test("detects a caret inside a fence", function (assert) {
    assert.deepEqual(isInsideMonospace("```x``` y", 4), true);
    assert.deepEqual(isInsideMonospace("```x``` y", 8), false);
});

// -- strikethrough --
QUnit.test("wraps with a tilde", function (assert) {
    assert.deepEqual(mark("a bad b", 2, 5, "strikethrough"), "a ~bad~ b");
});

// -- restored selection --
QUnit.test("spans the markers so marks can be chained", function (assert) {
    assert.deepEqual(selectionAfter("hello world", 0, 5, "bold"), "*hello*");
});

QUnit.test("is the bare text after unwrapping", function (assert) {
    assert.deepEqual(selectionAfter("*hello* world", 1, 6, "bold"), "hello");
});

/**
 * The preview is display-only, so what is tested here is that it reads back the
 * grammar `applyMark` writes — and that a body can never become markup.
 */
// -- preview --
QUnit.test("renders the four marks", function (assert) {
    assert.deepEqual(renderPreview("*a*"), "<strong>a</strong>");
    assert.deepEqual(renderPreview("_a_"), "<em>a</em>");
    assert.deepEqual(renderPreview("~a~"), "<s>a</s>");
    assert.deepEqual(renderPreview("```a```"), "<code>a</code>");
});

QUnit.test("nests, the way chaining two marks produces", function (assert) {
    assert.deepEqual(renderPreview("*bold _and italic_*",
        "<strong>bold <em>and italic</em></strong>"
    );
});

QUnit.test("leaves markers that would not render on a phone", function (assert) {
    // Whitespace inside the marker: the phone shows literal asterisks, and
    // `applyMark` refuses to emit this in the first place.
    assert.deepEqual(renderPreview("*a *"), "*a *");
    // A marker does not span a line break.
    assert.deepEqual(renderPreview("*a\nb*"), "*a<br/>b*");
});

QUnit.test("monospace spans lines and suppresses the other marks", function (assert) {
    assert.deepEqual(renderPreview("```*a*\nb```"), "<code>*a*<br/>b</code>");
});

QUnit.test("leaves an unterminated fence as text", function (assert) {
    assert.deepEqual(renderPreview("```a"), "```a");
});

QUnit.test("escapes the body, so it can never be markup", function (assert) {
    assert.deepEqual(renderPreview("<b>&</b>"), "&lt;b&gt;&amp;&lt;/b&gt;");
});

QUnit.test("shows a placeholder as itself, not as a value", function (assert) {
    assert.deepEqual(renderPreview("Hi {{ object.name }}",
        'Hi <span class="o_whatsmeow_placeholder">{{ object.name }}</span>'
    );
});

QUnit.test("does not let two placeholders italicise the text between them", function (assert) {
    // Both hold an underscore; naive parsing would mark everything between.
    assert.deepEqual(renderPreview("{{ object.partner_id.name }} {{ object.user_id.name }}",
        '<span class="o_whatsmeow_placeholder">{{ object.partner_id.name }}</span> ' +
            '<span class="o_whatsmeow_placeholder">{{ object.user_id.name }}</span>'
    );
});

QUnit.test("marks a placeholder without breaking it", function (assert) {
    assert.deepEqual(renderPreview("*{{ object.name }}*",
        '<strong><span class="o_whatsmeow_placeholder">{{ object.name }}</span></strong>'
    );
});

QUnit.test("is empty for an empty body", function (assert) {
    assert.deepEqual(renderPreview(""), "");
});

});
