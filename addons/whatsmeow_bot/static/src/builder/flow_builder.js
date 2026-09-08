/** @odoo-module **/

import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { _lt, _t } from "@web/core/l10n/translation";
import { sprintf } from "@web/core/utils/strings";
import { ConfirmationDialog } from "@web/core/confirmation_dialog/confirmation_dialog";

import {
    Component,
    markup,
    onMounted,
    onPatched,
    onWillStart,
    onWillUnmount,
    useRef,
    useState,
} from "@odoo/owl";

import { renderPreview } from "./markup";

/**
 * The chatbot flow designer.
 *
 * Two ideas, and the whole file follows from them.
 *
 * The first is that a card *is* the editor. Click a step and it opens where it
 * already sits — same place, same colour, same numbers — showing only the
 * handful of fields its kind can actually use. A message step asks four
 * questions; a menu asks six. Nothing is hidden in a panel you have to map back
 * onto a card, and the branch lines redraw as you change a dropdown, so a
 * rerouted option is visible before it is saved.
 *
 * The second is that the inline editor is deliberately not complete. Retry
 * wording, the stored value of an option, everything rare: one click on
 * **All fields** opens the ordinary Odoo form, which already knows how to
 * render all of it properly. The simple path stays simple precisely because it
 * is allowed to stop.
 *
 * Writes go through plain `orm.write` / `orm.create` with x2many commands. No
 * bespoke save endpoint means no second set of rules to keep in step with the
 * model's own.
 */

// One visual identity per step type, used by the card, its badge, the palette
// button that creates it and the colour of the lines leaving it. `accent` names
// a CSS custom-property set; `color` is the same colour for SVG, which cannot
// read one.
export const STEP_STYLE = {
    message: { icon: "fa-comment-o", accent: "teal", color: "#0f9d8f" },
    question: { icon: "fa-question-circle-o", accent: "blue", color: "#2f80ed" },
    choice: { icon: "fa-list-ol", accent: "violet", color: "#7c5cff" },
    action: { icon: "fa-bolt", accent: "amber", color: "#d98c1f" },
    handover: { icon: "fa-user-o", accent: "rose", color: "#e5566d" },
    end: { icon: "fa-flag-checkered", accent: "slate", color: "#7b8794" },
};

const DEFAULT_BODY = {
    message: _lt("Hi {{ contact.name }}! 👋"),
    question: _lt("What is your name?"),
    choice: _lt("How can I help?"),
    action: "",
    handover: _lt("One moment — I am passing you to a colleague."),
    end: _lt("Thanks! We will be in touch shortly."),
};

const WAITING_TYPES = ["question", "choice"];
const TERMINAL_TYPES = ["end", "handover"];

const SVG_NS = "http://www.w3.org/2000/svg";
// How far inside the lane's right padding the curves bend. The padding reserves
// that room; this keeps the bend off the very edge.
const GUTTER_INSET = 26;
// Where a line lands on its target: level with the card's number badge, so an
// arrow points at the step's own identity rather than at the middle of whatever
// text it happens to contain.
const TARGET_INSET = 22;
const BADGE_OFFSET = 16;

export class WhatsmeowFlowBuilder extends Component {
    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.dialog = useService("dialog");
        this.notification = useService("notification");
        this.laneRef = useRef("lane");
        this.linksRef = useRef("links");

        const params = this.props.action.params || {};
        const context = this.props.action.context || {};
        this.botId = params.bot_id || context.active_id;

        // Draft options have no database id until they are saved, and Owl needs
        // a stable key for each row while they are being typed into.
        this.draftSeq = 0;

        this.state = useState({
            loading: true,
            saving: false,
            bot: {},
            steps: [],
            options: { step_types: [], input_types: [], server_actions: [], users: [] },
            readonly: false,
            // The step being edited: an id, the string "new", or null.
            editingId: null,
            draft: null,
            removedAnswers: [],
            dragId: null,
            dropIndex: null,
        });

        onWillStart(() => this.load());
        // The lines are measured from the DOM, so they can only be drawn once it
        // exists — and redrawn after every patch, because an editor opening
        // makes its card twice as tall and moves every arrow below it.
        onMounted(() => this.drawLinks());
        onPatched(() => this.drawLinks());
        onWillUnmount(() => this.observer && this.observer.disconnect());
    }

    // -- data -----------------------------------------------------------------
    async load() {
        this.apply(await this.orm.call("whatsmeow.bot", "builder_data", [this.botId]));
    }

    apply(data) {
        Object.assign(this.state, {
            bot: data.bot,
            steps: data.steps,
            options: data.options,
            readonly: data.readonly,
            loading: false,
            dragId: null,
            dropIndex: null,
        });
    }

    backToBot() {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "whatsmeow.bot",
            res_id: this.botId,
            views: [[false, "form"]],
            target: "current",
        });
    }

    // -- the inline editor ----------------------------------------------------
    isEditing(stepId) {
        return this.state.editingId === stepId;
    }

    get editingNew() {
        return this.state.editingId === "new";
    }

    startEdit(step) {
        if (this.state.readonly || this.isEditing(step.id)) {
            return;
        }
        this.state.draft = {
            id: step.id,
            name: step.name,
            step_type: step.step_type,
            body: step.body,
            input_type: step.input_type,
            store_key: step.store_key,
            next_step_id: step.next_step_id,
            server_action_ids: [...step.server_action_ids],
            operator_ids: [...step.operator_ids],
            answers: step.answers.map((answer) => ({
                key: `a${answer.id}`,
                id: answer.id,
                name: answer.name,
                keywords: answer.keywords,
                value: answer.value,
                next_step_id: answer.next_step_id,
                server_action_ids: [...answer.server_action_ids],
            })),
        };
        this.state.removedAnswers = [];
        this.state.editingId = step.id;
    }

    startNew(type) {
        this.state.draft = {
            id: false,
            name: "",
            step_type: type,
            body: (DEFAULT_BODY[type] || "").toString(),
            input_type: "text",
            store_key: "",
            next_step_id: false,
            server_action_ids: [],
            operator_ids: [],
            answers: type === "choice" ? [this.newAnswer(), this.newAnswer()] : [],
        };
        this.state.removedAnswers = [];
        this.state.editingId = "new";
    }

    cancelEdit() {
        this.state.editingId = null;
        this.state.draft = null;
        this.state.removedAnswers = [];
    }

    /**
     * Only the mistakes the database would reject anyway, caught here so the
     * answer arrives next to the field rather than in a traceback dialog.
     * Everything else — a bad store key, a link across bots — is the model's
     * job, and it already does it.
     */
    validateDraft() {
        const draft = this.state.draft;
        if (draft.step_type === "choice") {
            if (!draft.answers.length) {
                return _t("A step with options needs at least one option.");
            }
            if (draft.answers.some((answer) => !(answer.name || "").trim())) {
                return _t("Every option needs the text the contact will see.");
            }
        }
        return null;
    }

    answerCommands() {
        const commands = this.state.removedAnswers.map((id) => [2, id]);
        for (const [index, answer] of this.state.draft.answers.entries()) {
            // Position is the number the contact types, so it is written every
            // time rather than left to whatever order the rows happen to be in.
            const values = {
                sequence: (index + 1) * 10,
                name: answer.name || "",
                keywords: answer.keywords || false,
                value: answer.value || false,
                next_step_id: answer.next_step_id || false,
                server_action_ids: [[6, 0, answer.server_action_ids]],
            };
            commands.push(answer.id ? [1, answer.id, values] : [0, 0, values]);
        }
        return commands;
    }

    /** Write the draft and hand back the step's id. */
    async commitDraft() {
        const draft = this.state.draft;
        const values = {
            name: draft.name || false,
            step_type: draft.step_type,
            body: draft.body || false,
            input_type: draft.input_type,
            store_key: draft.store_key || false,
            next_step_id: draft.next_step_id || false,
            server_action_ids: [[6, 0, draft.server_action_ids]],
            operator_ids: [[6, 0, draft.operator_ids]],
            answer_ids: this.answerCommands(),
        };
        if (draft.id) {
            await this.orm.write("whatsmeow.bot.step", [draft.id], values);
            return draft.id;
        }
        const sequence =
            Math.max(0, ...this.state.steps.map((step) => step.sequence || 0)) + 10;
        const ids = await this.orm.create("whatsmeow.bot.step", [
            { ...values, bot_id: this.botId, sequence },
        ]);
        return ids[0];
    }

    async saveStep() {
        const problem = this.validateDraft();
        if (problem) {
            this.notification.add(problem, { type: "danger" });
            return;
        }
        this.state.saving = true;
        try {
            await this.commitDraft();
            this.cancelEdit();
            await this.load();
        } finally {
            this.state.saving = false;
        }
    }

    /**
     * The escape hatch: save what has been typed, then open the full form.
     *
     * Saving first rather than discarding, because someone who has filled in
     * three fields and then wants the fourth did not ask to lose the three.
     */
    async openFullForm() {
        const problem = this.validateDraft();
        if (problem) {
            this.notification.add(problem, { type: "danger" });
            return;
        }
        this.state.saving = true;
        let stepId;
        try {
            stepId = await this.commitDraft();
        } finally {
            this.state.saving = false;
        }
        this.cancelEdit();
        this.openStepForm(stepId);
    }

    openStepForm(stepId) {
        this.action.doAction(
            {
                type: "ir.actions.act_window",
                name: _t("Step"),
                res_model: "whatsmeow.bot.step",
                res_id: stepId,
                views: [[false, "form"]],
                target: "new",
            },
            { onClose: () => this.load() }
        );
    }

    // -- draft fields ---------------------------------------------------------
    onChangeKind(event) {
        const draft = this.state.draft;
        draft.step_type = event.target.value;
        // Settings the new kind cannot use are dropped rather than hidden: a
        // store key left on a plain message is the sort of thing that reads
        // like a bug for months before anyone works out it never did anything.
        if (!WAITING_TYPES.includes(draft.step_type)) {
            draft.store_key = "";
        }
        if (draft.step_type === "choice" && !draft.answers.length) {
            draft.answers = [this.newAnswer(), this.newAnswer()];
        }
        if (draft.step_type !== "choice") {
            this.state.removedAnswers.push(
                ...draft.answers.filter((answer) => answer.id).map((answer) => answer.id)
            );
            draft.answers = [];
        }
        if (draft.step_type !== "handover") {
            draft.operator_ids = [];
        }
        if (TERMINAL_TYPES.includes(draft.step_type)) {
            draft.next_step_id = false;
        }
    }

    /** A `<select>` of step ids writes an integer, or false for "no link". */
    onChangeLink(target, event) {
        target.next_step_id = event.target.value ? Number(event.target.value) : false;
    }

    newAnswer() {
        return {
            key: `n${++this.draftSeq}`,
            id: false,
            name: "",
            keywords: "",
            value: "",
            next_step_id: false,
            server_action_ids: [],
        };
    }

    addAnswer() {
        this.state.draft.answers.push(this.newAnswer());
    }

    removeAnswer(answer) {
        if (answer.id) {
            this.state.removedAnswers.push(answer.id);
        }
        this.state.draft.answers = this.state.draft.answers.filter(
            (entry) => entry.key !== answer.key
        );
    }

    moveAnswer(answer, delta) {
        const answers = this.state.draft.answers;
        const at = answers.findIndex((entry) => entry.key === answer.key);
        const to = at + delta;
        if (at === -1 || to < 0 || to >= answers.length) {
            return;
        }
        const [moved] = answers.splice(at, 1);
        answers.splice(to, 0, moved);
    }

    // A tag list is a chip row plus a "add one" dropdown: it is the smallest
    // thing that behaves like the many2many_tags people already know, without
    // pulling a form-view field into a canvas.
    addTag(owner, field, event) {
        const id = Number(event.target.value);
        // Put the dropdown back to its prompt, so it reads as an action rather
        // than as a control showing the last thing you picked.
        event.target.value = "";
        if (id && !owner[field].includes(id)) {
            owner[field] = [...owner[field], id];
        }
    }

    removeTag(owner, field, id) {
        owner[field] = owner[field].filter((entry) => entry !== id);
    }

    // -- steps ----------------------------------------------------------------
    indexOf(stepId) {
        return this.state.steps.findIndex((step) => step.id === stepId);
    }

    stepById(stepId) {
        return this.state.steps.find((step) => step.id === stepId) || null;
    }

    deleteStep(stepId) {
        const step = this.stepById(stepId);
        this.dialog.add(ConfirmationDialog, {
            title: _t("Delete this step?"),
            body: sprintf(
                _t(
                    "\"%s\" will be removed. Anything that pointed at it simply carries on to the next step."
                ),
                step ? step.title : ""
            ),
            confirm: async () => {
                await this.orm.unlink("whatsmeow.bot.step", [stepId]);
                if (this.isEditing(stepId)) {
                    this.cancelEdit();
                }
                await this.load();
            },
            cancel: () => {},
        });
    }

    async duplicateStep(stepId) {
        await this.orm.call("whatsmeow.bot.step", "copy", [[stepId]]);
        await this.load();
    }

    // -- order ----------------------------------------------------------------
    /**
     * Move the cards locally first, then tell the server.
     *
     * Position is the flow, so a drag is a real edit — but waiting on a round
     * trip before the card moves makes a canvas feel broken. The reload that
     * follows settles any disagreement.
     */
    async reorder(ids) {
        this.apply(
            await this.orm.call("whatsmeow.bot", "builder_reorder", [this.botId, ids])
        );
    }

    move(stepId, delta) {
        const at = this.indexOf(stepId);
        const to = at + delta;
        if (at === -1 || to < 0 || to >= this.state.steps.length) {
            return;
        }
        const [step] = this.state.steps.splice(at, 1);
        this.state.steps.splice(to, 0, step);
        this.reorder(this.state.steps.map((entry) => entry.id));
    }

    onDragStart(stepId, event) {
        this.state.dragId = stepId;
        event.dataTransfer.effectAllowed = "move";
        // Firefox refuses to start a drag with no payload.
        event.dataTransfer.setData("text/plain", String(stepId));
    }

    onDragOver(index, event) {
        if (this.state.dragId === null) {
            return;
        }
        event.preventDefault();
        this.state.dropIndex = index;
    }

    onDrop(index, event) {
        event.preventDefault();
        const stepId = this.state.dragId;
        this.state.dragId = null;
        this.state.dropIndex = null;
        const at = this.indexOf(stepId);
        if (stepId === null || at === -1 || at === index) {
            return;
        }
        const [step] = this.state.steps.splice(at, 1);
        // Removing the dragged card first shifts every later slot up by one.
        this.state.steps.splice(at < index ? index - 1 : index, 0, step);
        this.reorder(this.state.steps.map((entry) => entry.id));
    }

    onDragEnd() {
        this.state.dragId = null;
        this.state.dropIndex = null;
    }

    // -- the lines ------------------------------------------------------------
    /**
     * Draw one curve per branch, from the thing that branches to the step it
     * reaches.
     *
     * Written straight into the SVG rather than rendered by Owl, because every
     * coordinate here comes from measuring the DOM Owl has just produced —
     * feeding those measurements back into reactive state would re-render,
     * which would re-measure, which is a loop rather than a design. Nothing
     * else reads these nodes, so nothing else can be surprised by them.
     *
     * The editor's own dropdowns carry the same data attributes as the chips
     * they replace, so re-pointing an option redraws its line immediately —
     * before anything is saved, which is exactly when you want to see it.
     */
    drawLinks() {
        const lane = this.laneRef.el;
        const svg = this.linksRef.el;
        if (!lane || !svg) {
            return;
        }
        this.watchLane(lane);

        const width = lane.offsetWidth;
        const height = lane.offsetHeight;
        svg.setAttribute("width", width);
        svg.setAttribute("height", height);
        svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
        while (svg.firstChild) {
            svg.removeChild(svg.firstChild);
        }

        const laneBox = lane.getBoundingClientRect();
        const gutter = width - GUTTER_INSET;

        for (const source of lane.querySelectorAll("[data-link-target]")) {
            // Step ids are integers; coercing rather than escaping keeps the
            // selector honest and rejects anything that is not one.
            const targetId = Number(source.dataset.linkTarget);
            const target = targetId ? this.cardFor(lane, targetId) : null;
            if (!target) {
                continue; // a link to a step that is no longer drawn
            }
            const from = source.getBoundingClientRect();
            const to = target.getBoundingClientRect();
            const x0 = from.right - laneBox.left;
            const y0 = from.top - laneBox.top + from.height / 2;
            const x1 = to.right - laneBox.left;
            const y1 = to.top - laneBox.top + TARGET_INSET;
            const color = source.dataset.linkColor || "#7b8794";
            const owner = source.dataset.linkOwner || "";

            svg.appendChild(
                this.svgNode("path", {
                    d: `M ${x0},${y0} C ${gutter},${y0} ${gutter},${y1} ${x1},${y1}`,
                    fill: "none",
                    stroke: color,
                    "stroke-width": 2,
                    "stroke-linecap": "round",
                    "data-from": owner,
                })
            );
            // The arrowhead is drawn rather than markered: a marker inherits one
            // colour per definition, and these lines are coloured per step.
            svg.appendChild(
                this.svgNode("path", {
                    d: `M ${x1},${y1} l 8,-4.5 l 0,9 z`,
                    fill: color,
                    "data-from": owner,
                })
            );

            const label = source.dataset.linkLabel;
            if (label) {
                svg.appendChild(
                    this.svgNode("circle", {
                        cx: x0 + BADGE_OFFSET,
                        cy: y0,
                        r: 9,
                        fill: color,
                        "data-from": owner,
                    })
                );
                const text = this.svgNode("text", {
                    x: x0 + BADGE_OFFSET,
                    y: y0,
                    fill: "#fff",
                    "font-size": 10,
                    "font-weight": 700,
                    "text-anchor": "middle",
                    "dominant-baseline": "central",
                    "data-from": owner,
                });
                text.textContent = label;
                svg.appendChild(text);
            }
        }
    }

    svgNode(tag, attributes) {
        const node = document.createElementNS(SVG_NS, tag);
        for (const [name, value] of Object.entries(attributes)) {
            node.setAttribute(name, value);
        }
        return node;
    }

    watchLane(lane) {
        if (this.observer || !window.ResizeObserver) {
            return;
        }
        // A card that reflows — the window narrows, an editor opens, a font
        // loads — moves every line below it. The SVG is out of flow, so
        // redrawing cannot resize the lane and this cannot feed itself.
        this.observer = new ResizeObserver(() => this.drawLinks());
        this.observer.observe(lane);
    }

    /**
     * Fade every line that does not leave this card.
     *
     * A class toggled straight on the SVG nodes, not a state change: hovering a
     * card must not re-render the canvas, and the nodes were built by hand
     * anyway.
     */
    focusLinks(stepId) {
        const svg = this.linksRef.el;
        if (!svg) {
            return;
        }
        const wanted = stepId === null ? null : String(stepId);
        for (const node of svg.children) {
            node.classList.toggle(
                "o_wa_edge_dim",
                wanted !== null && node.dataset.from !== wanted
            );
        }
    }

    cardFor(lane, stepId) {
        return lane.querySelector(`.o_wa_step[data-step-id="${Number(stepId)}"]`);
    }

    /** Scroll a branch's target into view and flash it. */
    jumpTo(stepId) {
        const lane = this.laneRef.el;
        const el = lane && stepId ? this.cardFor(lane, stepId) : null;
        if (!el) {
            return;
        }
        el.scrollIntoView({ behavior: "smooth", block: "center" });
        el.classList.add("o_wa_flash");
        setTimeout(() => el.classList.remove("o_wa_flash"), 900);
    }

    // -- rendering helpers ----------------------------------------------------
    styleOf(type) {
        return STEP_STYLE[type] || STEP_STYLE.message;
    }

    typeLabel(type) {
        const found = this.state.options.step_types.find((entry) => entry.value === type);
        return found ? found.label : type;
    }

    actionName(id) {
        const found = this.state.options.server_actions.find((entry) => entry.id === id);
        return found ? found.name : "";
    }

    userName(id) {
        const found = this.state.options.users.find((entry) => entry.id === id);
        return found ? found.name : "";
    }

    /** Actions not already on the draft — a dropdown should not offer a repeat. */
    get spareActions() {
        const chosen = this.state.draft.server_action_ids;
        return this.state.options.server_actions.filter(
            (entry) => !chosen.includes(entry.id)
        );
    }

    spareActionsFor(answer) {
        return this.state.options.server_actions.filter(
            (entry) => !answer.server_action_ids.includes(entry.id)
        );
    }

    get spareUsers() {
        const chosen = this.state.draft.operator_ids;
        return this.state.options.users.filter((entry) => !chosen.includes(entry.id));
    }

    /** Every step the draft may link to — itself included, a self-loop re-asks. */
    get linkTargets() {
        return this.state.steps;
    }

    get draftIsWaiting() {
        return WAITING_TYPES.includes(this.state.draft.step_type);
    }

    get draftIsTerminal() {
        return TERMINAL_TYPES.includes(this.state.draft.step_type);
    }

    /**
     * A card's classes, built here rather than in the template: the accent is a
     * computed name, and an object literal with a computed key is exactly the
     * sort of expression a template compiler is entitled to misread.
     */
    stepClass(step, index) {
        const classes = ["o_wa_step", `o_wa_accent_${this.styleOf(step.step_type).accent}`];
        if (this.isEditing(step.id)) {
            classes.push("o_wa_editing");
        }
        if (step.id === this.state.dragId) {
            classes.push("o_wa_dragging");
        }
        if (this.state.dragId !== null && this.state.dropIndex === index) {
            classes.push("o_wa_dropbefore");
        }
        return classes.join(" ");
    }

    preview(step) {
        return markup(renderPreview(step.body || ""));
    }

    get isEmpty() {
        return !this.state.loading && this.state.steps.length === 0;
    }
}

WhatsmeowFlowBuilder.template = "whatsmeow_bot.FlowBuilder";

registry.category("actions").add("whatsmeow_bot.flow_builder", WhatsmeowFlowBuilder);
