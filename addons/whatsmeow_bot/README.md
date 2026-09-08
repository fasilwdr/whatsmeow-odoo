## Overview

**Whatsmeow Chatbot** (`whatsmeow_bot`) answers incoming WhatsApp messages automatically, walks the contact through a conversation you draw yourself, keeps every answer, and hands the conversation to a real colleague when the script says so.

A bot decides whether to speak using the very same match criteria you already use for inbound filtering and Discuss routing — sender, chat type, keyword, phone, contact. The workflow itself is drawn in a **flow designer**: cards in the order the conversation runs, a line drawn for every branch, and a live preview of the bubble the contact will actually receive. A step is edited in the card itself — a short form showing only what its kind can use — with the full Odoo form one click away for anything rarer. Every conversation the bot holds becomes a record with its transcript, its collected answers, and how it ended, visible to WhatsApp administrators from their own menu.

### 🤖 Answers as a Bot, Not as You
The bot replies on behalf of a dedicated **WhatsApp Bot** user shipped with the module, or as OdooBot — your choice per bot. On WhatsApp the contact simply sees your number; inside Odoo, the notes and briefings the bot leaves are attributed to the bot rather than to a colleague who was not there.

### 🧭 The Conditions You Already Know
When a bot answers is asked in exactly the language you learnt twice already: the same criteria as the session's **Inbound Filtering** rules and its **Discuss Routing** rules. Set what matters, leave the rest blank, and order the bots — first match wins.

### 🗺️ A Map You Can Read
Press **Design the Flow** and the workflow opens as a canvas: colour-coded cards in the order the conversation runs, a WhatsApp bubble preview per step, and a **drawn line for every branch** — option 2 of a menu leaves the card at the number 2 and curves down to the step it reaches. Hover a card to pick its lines out of the rest.

### ✏️ The Card Is the Editor
Click a step and it opens where it already sits — same place, same colour, same numbers — showing only the handful of fields its kind can actually use. A message step asks four questions; a menu asks six. Nothing is hidden in a panel you have to map back onto a card, and the branch lines redraw as you change a dropdown, so a rerouted option is visible before you save it.

### 🚪 A Way Out to the Full Form
The inline editor is deliberately not complete. For the rare field — retry wording, an option's stored value — **All fields** saves what you typed and opens the ordinary Odoo form in a dialog, which already renders all of it properly. The same steps are also a plain list on the bot's **Steps** tab. Three ways in, one set of records.

## Features

### 🧱 Six Kinds of Step
**Send a message**, **Ask for an answer**, **Ask with options**, **Run a server action**, **Hand over to a human**, and **End the conversation**. Deliberately few — between them they cover "say something, ask something, do something, fetch a colleague, stop".

### 🔢 Numbered Options That Actually Work
A menu step sends its options as a numbered list in the message body. The contact can answer with the number, with the option's own words, or with any keyword you add to it — so *2*, *Support* and *help* all pick the same branch. Each option can lead to its own step — drawn as a line on the canvas — and fire its own server actions.

### 🗃️ Answers You Can Reuse and Report On
Every answer is filed under a **store key** you choose. Quote it in a later message as `{{ answer.your_key }}`, alongside `{{ contact.name }}` and friends. The values are records, not a blob — searchable, groupable, and listed under **Collected Answers**.

### ✅ Input It Can Trust
A question can expect text, a number, an email, a phone or a date, and anything that is not one is asked again with your own wording. After the retries run out the bot does what you told it to: keep asking, move on, hand over, or stop.

### ⚡ Server Actions From What Was Said
Any step or option can fire **several** `ir.actions.server`. Each runs on the chat — or on the contact, when it is a `res.partner` action — with everything collected so far in its context, so "create the lead", "raise the ticket" *and* "tag the customer" is a configuration, not a customisation. Each runs in its own savepoint: one that fails is recorded on the chat, the rest still run, and the conversation carries on.

### 🤝 Handover That Reuses Your Routing
A handover opens (or continues) the contact's Discuss conversation and adds whoever the session's own **Discuss routing rules** pick — the operators already configured for that contact — plus anyone the bot or the step names. The operator is briefed with everything the bot collected, and the bot then stays out of that conversation until someone releases it.

### 🙊 It Does Not Talk Over People
By default a bot never starts a workflow on a conversation an operator is already attending in Discuss, and never speaks in a conversation a previous handover put in human hands.

### 🕓 Chats That Close Themselves
A conversation nobody comes back to expires after a timeout you set, so the contact starts fresh next time instead of answering a question they have long forgotten. **Stop words** and **restart words** work from anywhere in the flow.

### 🗂️ Every Chat On File
**WhatsApp → Chatbot Chats** lists every conversation a bot has held: its transcript (what the bot said, what the contact answered, which server actions ran), its collected values, the step it is waiting at, and how it ended.

## Installation

This module extends the core connector and the Discuss bridge, so **Whatsmeow WhatsApp Connector** (`whatsmeow`) and **Whatsmeow Discuss Routing** (`whatsmeow_discuss`) must be installed first — with the gateway deployed and at least one session paired. Once they are in place, install this one from the Apps list like any other add-on.

Installing it changes nothing on its own: no number answers with a bot until you opt it in.

## Configuration

- **Opt a number in**: **WhatsApp → Configuration → Sessions**, open the number, and on the **Chatbot** tab tick **Answer with a chatbot**.
- **Create a bot**: **WhatsApp → Configuration → Chatbots → New**. Give it a name and pick who it **Answers as** — the shipped *WhatsApp Bot* user or OdooBot.
- **Say when it answers**: on the **When it answers** tab, set the criteria. Leave **Chat type** as *Private* unless you really want the bot speaking inside WhatsApp groups.
- **Tune its manners**: on **Behaviour**, set how many times a question is re-asked, what happens when the retries run out, the timeout, and the stop / restart words.
- **Set up the handover**: on **Handover**, add the operators the bot should fetch and the line it sends while doing so.
- **Draw the flow**: press **Design the Flow**.

## Usage

### 🚀 Draw a Workflow
**WORKFLOW · 01**

1. Open the bot and press **Design the Flow**.
2. Pick a kind of step from the palette at the bottom of the canvas. A card appears, already open for editing — write what the bot should say and press **Save**.
3. Click any card to edit it again, in place.
4. Drag cards to reorder them, or use the arrows that appear on hover. The order of the cards is the order the conversation runs in, and it saves as you drag.
5. Need a field the card does not offer? **All fields** saves your changes and opens the full Odoo form. The expand icon on a card goes straight there without editing first.

### 🔀 Branch on an Answer
**WORKFLOW · 02**

1. Add an **Ask with options** step and give it a store key, e.g. `topic`.
2. Add one option per branch and write what the contact will see. Add a few **Also matches** keywords so a typed word works as well as the number.
3. Give each option a **Go to** step. A line is drawn on the canvas from that option's number to the step it reaches — and it follows the dropdown as you change it, before you save. Options with nothing set simply carry on to the next card.
4. Quote the choice later with `{{ answer.topic }}`.

### 🧾 Collect and Act
**WORKFLOW · 03**

1. Chain **Ask for an answer** steps, one per thing you need, each with its own **Expecting** type and store key.
2. On the last one, add whatever should happen to **Run actions** — create the lead, raise the ticket, write the contact. Several is fine; they run in the order shown, and when the order between two of them really matters, one *Multiple actions* server action states it explicitly.
3. In the action's Python code, read the answers from `env.context['whatsmeow_bot_values']` (a `{key: value}` dict) or from `record.value_ids` when the action runs on the chat.
4. Finish with an **End the conversation** step that thanks the contact by name.

### 🤝 Hand Over
**WORKFLOW · 04**

1. Add a **Hand over to a human** step — typically as an option on your main menu ("Talk to someone").
2. Leave its operator list empty to let the session's Discuss routing rules decide who attends, or tick specific people to add them on top.
3. When the bot reaches it, the contact's Discuss conversation opens with a note briefing the operator on everything collected.
4. When the conversation is finished, open the chat under **WhatsApp → Chatbot Chats** and press **Release to the Bot** if you want the bot to answer there again.

## Notes

- The bot's replies are sent **immediately**, not through the paced queue — they are answers to someone who has just written to you, which is the safest traffic there is, and a delayed answer is a useless one. They are therefore not counted against a number's warm-up allowance.
- A **stop word** closes the current conversation. It is *not* an opt-out. To stop messaging someone for good, add an **Opt the sender out** rule on the session's inbound filter, which blocks every send path there is.
- A bot never sees a message the session's inbound filter rejected, and never sees WhatsApp's empty first copy of a double-delivered message.
- If a workflow raises — a broken server action, a step deleted underneath a running chat — the bot's work is rolled back and the message is delivered the ordinary way, as though no bot existed. A contact's message is never lost to a bot's mistake.
