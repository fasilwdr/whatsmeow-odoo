## Overview

**Whatsmeow Point of Sale** (`whatsmeow_pos`) sends the customer their POS receipt over WhatsApp from the same **Send Receipt** dialog that emails it. When the order was invoiced, the invoice PDF goes out with it. The cashier can type or change the customer's number in the dialog before sending.

Each point of sale sends from a whatsmeow session. If none is set, it uses a connected one automatically. If no number is connected but a gateway is set up, a manager can pair one from the register by scanning a QR code.

## Features

### 🧾 Receipt and Invoice from the Register
The phone field in the Send Receipt dialog gets a WhatsApp button. It comes pre-filled with the customer's number, and the cashier can edit it. The receipt goes as an image, the same one the email attaches. An invoiced order also gets its invoice as a PDF. A number typed in local format (`050 123 4567`) is completed with the customer's country, or with the shop's.

### 🔌 Picks a Number on Its Own
A point of sale with no **Send From** number is given the first connected session. If its number is logged out or in error, a connected one replaces it. A number that is only *disconnected* (a phone with no signal) keeps its place: another number covers the sends until it comes back. This way the shop's customers don't start hearing from a different sender.

### 📷 Pair a Number from the Register
With no connected number, the dialog offers **Scan a QR code to connect one**. Pressing the WhatsApp button does the same. The QR code shows in the register and refreshes as WhatsApp rotates it. Once the phone scans it, the number belongs to this point of sale and the waiting receipt goes out. Pairing needs an active gateway and a **Point of Sale manager** or **WhatsApp administrator**. Cashiers are asked to fetch one.

### 💬 Your Own Wording
The receipt and invoice text come from whatsmeow templates (`pos.order` and `account.move`). If you don't pick any, the *POS Receipt* and *POS Invoice* templates shipped with the module are used. An invoice template with a report of its own attaches that report. Otherwise, the standard invoice PDF is attached.

### ✅ Same Rules as Every Other Send
Receipts are sent straight away rather than queued, because the customer is standing at the counter. Otherwise they are ordinary outgoing messages: opt-outs block them, the send is logged on the order's (and invoice's) chatter, and delivery states come back as usual. If the gateway refuses a send, the cashier sees why, and the message stays in **WhatsApp > Messages** so it can be resent.

## Configuration

**Point of Sale > Configuration > Settings > Bills & Receipts > WhatsApp Receipts**: turn it on (it is on by default), and optionally choose the sending number and the two templates.
