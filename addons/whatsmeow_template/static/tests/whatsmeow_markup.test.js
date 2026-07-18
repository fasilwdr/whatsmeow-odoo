import { describe, expect, test } from "@odoo/hoot";

import { applyMark, isInsideMonospace } from "@whatsmeow_template/whatsmeow_markup";

/**
 * The whole of §11.8 lives in `applyMark`, so the whole of it is testable
 * without mounting the widget — which is the reason the wrap function is a
 * standalone module in the first place.
 *
 * Each case here is a rule WhatsApp's parser imposes, not a string-manipulation
 * preference: every expectation is a sequence that renders on a phone, and the
 * naive "wrap the selection" would get several of them wrong.
 */
describe.current.tags("headless");

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

describe("whitespace inside markers", () => {
    test("wraps a clean selection", () => {
        expect(mark("hello world", 0, 5, "bold")).toBe("*hello* world");
    });

    test("shrinks past a trailing space", () => {
        // `*hello *` arrives as literal asterisks. Double-clicking a word
        // usually takes the trailing space, so this is the common case.
        expect(mark("hello world", 0, 6, "bold")).toBe("*hello* world");
    });

    test("shrinks past a leading space", () => {
        expect(mark("a hello b", 1, 8, "bold")).toBe("a *hello* b");
    });

    test("refuses a selection of only whitespace", () => {
        expect(mark("a   b", 1, 4, "bold")).toBe(null);
    });

    test("refuses a collapsed selection", () => {
        expect(mark("hello", 2, 2, "bold")).toBe(null);
    });
});

describe("word boundaries", () => {
    test("expands a sub-word selection out to the whole word", () => {
        // Mid-word markers are unreliable, so the contract is "the smallest
        // renderable range containing the selection", not "what was selected".
        expect(mark("hello", 1, 3, "bold")).toBe("*hello*");
    });

    test("does not swallow an adjacent mark's marker", () => {
        expect(mark("*a* b", 4, 5, "italic")).toBe("*a* _b_");
    });
});

describe("toggling", () => {
    test("unwraps when the markers sit just outside the selection", () => {
        expect(mark("*hello* world", 1, 6, "bold")).toBe("hello world");
    });

    test("unwraps when the selection contains its own markers", () => {
        // This is the shape the widget hands back after wrapping, so without it
        // a second click on bold would emit `**hello**` — literal asterisks.
        expect(mark("*hello* world", 0, 7, "bold")).toBe("hello world");
    });

    test("round-trips across repeated clicks", () => {
        let [value, start, end] = ["hello world", 0, 5];
        const seen = [];
        for (let n = 0; n < 4; n++) {
            ({ value, selectionStart: start, selectionEnd: end } = applyMark(
                value, start, end, "bold"
            ));
            seen.push(value);
        }
        expect(seen).toEqual([
            "*hello* world", "hello world", "*hello* world", "hello world",
        ]);
    });

    test("nests a different mark rather than replacing it", () => {
        expect(mark("*hello* world", 0, 7, "italic")).toBe("_*hello*_ world");
    });
});

describe("line breaks", () => {
    test("applies an inline mark once per line", () => {
        // Markers do not reliably span a newline.
        expect(mark("one\ntwo", 0, 7, "bold")).toBe("*one*\n*two*");
    });

    test("skips blank lines", () => {
        expect(mark("one\n\ntwo", 0, 8, "bold")).toBe("*one*\n\n*two*");
    });
});

describe("placeholders", () => {
    test("snaps an edge landing inside {{ }} out to the brace", () => {
        // `{{ object.*na*me }}` does not break WhatsApp, it breaks
        // inline_template at render time.
        expect(mark("{{ object.name }}", 3, 9, "bold")).toBe("*{{ object.name }}*");
    });

    test("expands a selection that ends inside a placeholder", () => {
        const text = "Hi {{ object.name }} there";
        expect(mark(text, 0, 15, "bold")).toBe("*Hi {{ object.name }}* there");
    });
});

describe("monospace", () => {
    test("spans newlines instead of splitting per line", () => {
        expect(mark("one\ntwo", 0, 7, "monospace")).toBe("```one\ntwo```");
    });

    test("does not snap to word boundaries", () => {
        expect(mark("hello", 1, 3, "monospace")).toBe("h```el```lo");
    });

    test("toggles off", () => {
        expect(mark("```one\ntwo```", 3, 10, "monospace")).toBe("one\ntwo");
    });

    test("detects a caret inside a fence", () => {
        expect(isInsideMonospace("```x``` y", 4)).toBe(true);
        expect(isInsideMonospace("```x``` y", 8)).toBe(false);
    });
});

describe("strikethrough", () => {
    test("wraps with a tilde", () => {
        expect(mark("a bad b", 2, 5, "strikethrough")).toBe("a ~bad~ b");
    });
});

describe("restored selection", () => {
    test("spans the markers so marks can be chained", () => {
        expect(selectionAfter("hello world", 0, 5, "bold")).toBe("*hello*");
    });

    test("is the bare text after unwrapping", () => {
        expect(selectionAfter("*hello* world", 1, 6, "bold")).toBe("hello");
    });
});
