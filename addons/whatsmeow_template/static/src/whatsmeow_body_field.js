import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { TextField, textField } from "@web/views/fields/text/text_field";

import { useState } from "@odoo/owl";

import { applyMark, isInsideMonospace, MARKS } from "./whatsmeow_markup";

/**
 * A textarea that offers WhatsApp's four text styles on the current selection.
 *
 * The styles already work today — the gateway sends `body` verbatim and the
 * recipient's phone parses the markers — so this adds discoverability, not
 * capability (PLAN.md §11.8).
 *
 * The design decision is that there is **no second representation**: this edits
 * the raw markup, it does not model the text as HTML. Everything expensive
 * about a rich-text approach comes from holding two forms of the same string
 * and converting between them, and a `{{ object.name }}` inside a
 * contenteditable splits across nodes the moment someone formats half of it.
 * The markup stays canonical, so `_render_body`, the queue and the gateway
 * never learn this feature exists — and a WYSIWYG later is a widget swap, not
 * a rewrite.
 *
 * For the same reason there is no preview pane: the markers are visible in the
 * textarea, so the edit surface already *is* the wire format.
 */
export class WhatsmeowBodyField extends TextField {
    static template = "whatsmeow_template.WhatsmeowBodyField";

    setup() {
        super.setup();
        this.marks = Object.entries(MARKS).map(([name, mark]) => ({
            name,
            icon: mark.icon,
            title: MARK_TITLES[name],
        }));
        this.toolbar = useState({ visible: false, top: 0, left: 0, inMonospace: false });
    }

    get textarea() {
        return this.textareaRef.el;
    }

    /**
     * Monospace suppresses the other styles inside it, so the inline buttons
     * would be a no-op there.
     */
    isDisabled(name) {
        return this.toolbar.inMonospace && name !== "monospace";
    }

    /** Show the toolbar whenever the caret leaves a real selection behind. */
    onSelect() {
        const textarea = this.textarea;
        if (!textarea || textarea.selectionStart === textarea.selectionEnd) {
            this.toolbar.visible = false;
            return;
        }
        Object.assign(this.toolbar, this.anchorFor(textarea), {
            visible: true,
            inMonospace: isInsideMonospace(textarea.value, textarea.selectionStart),
        });
    }

    onBlur() {
        super.onBlur(...arguments);
        this.toolbar.visible = false;
    }

    /**
     * Where to float the toolbar, in pixels relative to the wrapping div.
     *
     * A textarea exposes no caret geometry, so the position is measured: the
     * line is the newline count before the selection, and the column is the
     * width of that line's prefix in the textarea's own font. It only has to be
     * close — the result is clamped into the field, and being a few pixels off
     * costs nothing because the buttons act on the selection, not on wherever
     * they happen to sit.
     */
    anchorFor(textarea) {
        const style = getComputedStyle(textarea);
        const lineHeight = parseFloat(style.lineHeight) || 16;
        const before = textarea.value.slice(0, textarea.selectionStart);
        const lineIndex = before.split("\n").length - 1;
        const prefix = before.slice(before.lastIndexOf("\n") + 1);

        const context = (WhatsmeowBodyField.measureContext ??= document
            .createElement("canvas")
            .getContext("2d"));
        context.font = style.font || `${style.fontSize} ${style.fontFamily}`;

        const top = lineIndex * lineHeight - textarea.scrollTop - TOOLBAR_HEIGHT;
        const left = parseFloat(style.paddingLeft) + context.measureText(prefix).width;
        return {
            top: Math.max(top, 0),
            left: Math.max(Math.min(left, textarea.clientWidth - TOOLBAR_WIDTH), 0),
        };
    }

    /**
     * The textarea keeps focus (and therefore its selection) only because the
     * button's `mousedown` is prevented in the template — otherwise the
     * textarea blurs and the selection is gone before this ever runs.
     */
    async onClickMark(name) {
        const textarea = this.textarea;
        const { value, selectionStart, selectionEnd } = textarea;
        const result = applyMark(value, selectionStart, selectionEnd, name);
        if (!result) {
            // Nothing in the selection can carry a marker — leave it be.
            return;
        }

        // The record is what must change: setting `textarea.value` alone leaves
        // the record clean and the edit is dropped on save. The DOM is written
        // too, so the selection below lands on the new text whether or not the
        // re-render has flushed yet.
        textarea.value = result.value;
        await this.props.record.update({ [this.props.name]: result.value });

        if (!this.textarea) {
            return;
        }
        // Keep the markers inside the selection so marks can be chained: bold
        // then italic nests, and bold twice toggles back off.
        this.textarea.focus();
        this.textarea.setSelectionRange(result.selectionStart, result.selectionEnd);
        this.onSelect();
    }
}

/** Rough size of the floating toolbar, used only to keep it inside the field. */
const TOOLBAR_WIDTH = 132;
const TOOLBAR_HEIGHT = 32;

const MARK_TITLES = {
    bold: _t("Bold"),
    italic: _t("Italic"),
    strikethrough: _t("Strikethrough"),
    monospace: _t("Monospace"),
};

export const whatsmeowBodyField = {
    ...textField,
    component: WhatsmeowBodyField,
    displayName: _t("WhatsApp Message"),
    supportedTypes: ["text"],
};

registry.category("fields").add("whatsmeow_body", whatsmeowBodyField);
