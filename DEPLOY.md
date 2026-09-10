# Deploying the whatsmeow gateway

The gateway is a single Go binary that speaks the WhatsApp Web multi-device
protocol on one side and plain HTTP+JSON to Odoo on the other. This document
covers putting it on a server as a systemd service and wiring it to Odoo.

`install.sh` does the whole thing. Read on if you want to know what it does,
or if something did not come up.

---

## 1. What you need

| | |
|---|---|
| OS | Debian 12/13 or Ubuntu 22.04/24.04 (systemd + apt) |
| Access | root, via `sudo` |
| Network | outbound HTTPS (Go toolchain, Go modules, WhatsApp servers) |
| Disk | ~1.5 GB while building; a few hundred MB after |
| Odoo | 19, reachable from the gateway host |

One Odoo can drive several gateways, and one gateway can hold several WhatsApp
numbers. Running the gateway on the same host as Odoo is the simple case and is
what the defaults assume.

## 2. Install

Copy the repository to the server (or clone it), then run the installer from the
repository root:

```bash
cd whatsmeow-odoo
sudo ./install.sh
```

It finds the gateway source by walking up from itself, so it also works when
called by path from elsewhere (`sudo /srv/whatsmeow-odoo/install.sh`).

It is one script for Debian and Ubuntu — the two differ in nothing it touches,
so there is no separate Debian variant to keep in sync.

The script:

1. installs `git`, `build-essential`, `ca-certificates`, `wget`, `openssl`, `curl`
   (`build-essential` is not optional — `go-sqlite3` needs CGO, so a gcc must exist);
2. installs the Go toolchain into `/usr/local/go` if the one present is older than
   the version `gateway/go.mod` asks for;
3. creates the system user `wagw` and the data directory;
4. copies `main.go`, `go.mod`, `go.sum` into `/opt/whatsmeow-gateway` and builds there;
5. generates `/opt/whatsmeow-gateway/gateway.env` with a fresh API key and webhook
   secret — **unless the file already exists**, in which case it is left alone;
6. writes and starts the `whatsmeow-gateway` systemd unit;
7. waits for `/health` to answer, then prints the credentials to paste into Odoo.

**Re-running is safe.** It keeps the env file and the data directory, rebuilds
the binary and restarts the service — that is how you deploy a new version.

### Knobs

Override by prefixing the command:

```bash
sudo LISTEN_ADDR=127.0.0.1:9000 \
     ODOO_WEBHOOK_URL=https://odoo.example.com/whatsmeow/webhook \
     ./install.sh
```

| Variable | Default | |
|---|---|---|
| `LISTEN_ADDR` | `127.0.0.1:8080` | where the gateway listens |
| `ODOO_WEBHOOK_URL` | `http://127.0.0.1:8069/whatsmeow/webhook` | where inbound events are posted |
| `INSTALL_DIR` | `/opt/whatsmeow-gateway` | binary, env file |
| `DATA_DIR` | `/var/lib/whatsmeow-gateway` | session stores, staged media |
| `SERVICE_USER` | `wagw` | system user the service runs as |
| `GATEWAY_SRC` | found by walking up | gateway source, if it is not in the checkout |
| `GO_VERSION` | from `gateway/go.mod` | Go toolchain to install |
| `UPGRADE_WHATSMEOW` | `0` | see [§7](#7-upgrading-whatsmeow) |

On a re-run the existing `gateway.env` wins over the *defaults* — secrets and
settings are kept — but `LISTEN_ADDR` and `ODOO_WEBHOOK_URL` passed explicitly
are written into that file, so a first install that picked a bad port can be
corrected without losing the generated credentials:

```bash
sudo LISTEN_ADDR=127.0.0.1:8081 ./install.sh
```

`DATA_DIR` is deliberately *not* re-applied once an env file exists: the paired
sessions live in the old directory and moving it would orphan them. Anything
else is a hand-edit of `gateway.env` plus `systemctl restart whatsmeow-gateway`.

### Choosing a port

The default is `127.0.0.1:8080`. The script checks the port is free before
installing anything and stops with a one-line message if it is not — a busy
port otherwise shows up as a systemd restart loop.

Note that **8080 is a popular port** and, under WSL2 mirrored networking, a
listener on the *Windows* side occupies it inside Linux too without appearing in
`ss` or `netstat`. If 8080 is taken, 8081 is the usual next choice.

## 3. Wiring it to Odoo

The script ends by printing a gateway URL, an API key and a webhook secret.

1. Put the three add-ons on Odoo's `addons_path`: `whatsmeow` (required), plus
   `whatsmeow_discuss` and `whatsmeow_template` if you want them. Restart Odoo
   and install `whatsmeow` from Apps.
2. **WhatsApp → Configuration → Gateways → New.** Paste the URL and API key,
   set a webhook secret of your own (`openssl rand -hex 32` — one per database,
   not the gateway's), and fill in **This Odoo's URL** if Odoo sits behind a
   proxy or is not reachable at its Web Base URL. Save, then press **Test
   Connection**: it must go green, and it lists the sessions this key already
   owns — a good way to catch a key pasted into the wrong database.
3. **WhatsApp → Configuration → Sessions → New.** Pick the gateway and give the
   session a code — lowercase letters, digits, `_` and `-`, up to 40 characters.
   The code is how this Odoo addresses the session forever after, so treat it as
   permanent. It only has to be unique *within this database*: two clients on one
   gateway may both call their number `main`.
4. Press **Start / Pair**, scan the QR with WhatsApp on the phone
   (*Settings → Linked devices → Link a device*). The QR expires in seconds;
   press **Refresh Status** for a new one. The status goes **Connected** once
   paired.

**Start / Pair** is also what tells the gateway where to post this session's
events — the Webhook URL and secret from step 2, stored per session in
`registry.json`. So if you change **This Odoo's URL** later, press **Start /
Pair** again (it does not re-pair an already-paired number); the session-status
cron repairs a mismatch on its own within the hour.

Send a test message from Odoo, and reply from the phone to confirm the webhook
comes back. If outbound works but inbound never arrives, the session form shows
the gateway's own error under *The gateway cannot reach this Odoo*
(see [§8](#8-troubleshooting)).

Odoo drives the rest on four crons (queue, inbound media, recipient validation,
session status), so the Odoo cron worker must actually be running — with
`--max-cron-threads=0` messages queue and never leave.

### Reaching Odoo across hosts

If the gateway and Odoo are on different machines, **This Odoo's URL** on the
Gateway record must be an address the gateway can resolve, and the gateway's own
`LISTEN_ADDR` must be one Odoo can reach — `127.0.0.1` will not do for either.

Both directions carry a shared secret in a header and are otherwise unprotected,
so anything crossing a network you do not control belongs behind TLS: terminate
it at a reverse proxy in front of each side and keep the services themselves on
the loopback. Point Odoo's Gateway URL at the proxy, not at the binary.

A gateway that is not on loopback refuses to register a plain-`http` webhook URL
for a public host, and refuses a private/LAN one altogether, on the grounds that
a shared gateway should not be POSTing into its own network because a client
asked it to. `WMG_WEBHOOK_ALLOW_INSECURE=1` and `WMG_WEBHOOK_ALLOW_PRIVATE=1`
override each of those if your network genuinely is the trust boundary.

### One gateway, several Odoos

A gateway serves as many Odoo databases as you like. Each gets its own API key:

```bash
sudo ./install.sh --add-client acme
```

That prints a key to paste into that Odoo's Gateway record, and restarts the
service. From then on the two installs are separate in every way that matters:
a client sees only the sessions started with its own key — `GET /sessions`,
sending, media, logout, all of it — and its events go only to the URL its own
sessions registered. Session codes are namespaced per client, so `main` in one
Odoo and `main` in another are different numbers with different stores.

The key from the first install stays valid as the client `default`, and the
sessions that existed before you added anyone are filed under it, so an existing
single-Odoo gateway needs no changes at all.

A shared gateway is a shared blast radius: it is one process, one machine and
one WhatsApp connection pool. Keep clients whose uptime you have promised
separately on separate gateways, and expose the shared one over TLS only.

## 4. Day-to-day

```bash
systemctl status whatsmeow-gateway
systemctl restart whatsmeow-gateway
journalctl -u whatsmeow-gateway -f
curl http://127.0.0.1:8080/health          # unauthenticated, on purpose
```

## 5. Back up the data directory

`/var/lib/whatsmeow-gateway` holds one SQLite store per session, and those files
**are the pairing** — the credentials WhatsApp issued when the QR was scanned.
Lose them and every session must be re-paired by scanning again on each phone.

```bash
systemctl stop whatsmeow-gateway
tar czf whatsmeow-data-$(date +%F).tar.gz -C /var/lib whatsmeow-gateway
systemctl start whatsmeow-gateway
```

Stop the service first: SQLite files copied from under a running writer can be
restored into a corrupt state. `gateway.env` is worth keeping too — the API keys
in it are what the Odoo connection records expect; restoring data without it
means editing the credentials in Odoo.

`registry.json` sits in the same directory and is small, plain JSON, and just as
important: it records who owns each session and where its events go. Restore the
stores without it and the gateway comes up owning nothing — the next **Start /
Pair** would claim a *fresh* store and ask for a new QR on a number that is
already paired. It is also the file to edit by hand when a client changes
domain and you would rather not wait for Odoo to re-register.

The `media/` subdirectory inside it is a staging area, not state — inbound files
wait there for Odoo to fetch, and anything uncollected is deleted after
`WMG_MEDIA_TTL_HOURS` (24). It does not need backing up, but it does need room:
WhatsApp allows files up to ~100 MB.

## 6. Deploying a new version

```bash
git pull
sudo ./install.sh
```

Rebuild and restart, secrets and sessions untouched. Do this whenever the Go
side changes — new endpoints (`/react`, `/check`) exist only in a rebuilt binary,
and the matching Odoo feature fails against an old one. Upgrade the Odoo module
in the same pass:

```bash
odoo-bin -c odoo.conf -d <db> -u whatsmeow --stop-after-init
```

## 7. Upgrading whatsmeow

The build uses the whatsmeow revision pinned in `gateway/go.mod`. The pin is
deliberate: whatsmeow publishes no stable releases and its API drifts, so an
unattended upgrade is exactly how a working gateway stops compiling one morning.

To move it on purpose:

```bash
sudo UPGRADE_WHATSMEOW=1 ./install.sh
```

The refreshed `go.mod`/`go.sum` are copied back into the repo's `gateway/` —
commit them, so every other host builds the revision you just tested.

## 8. Troubleshooting

**`Test Connection` says unreachable / timed out.** The service is not running,
or Odoo is dialling the wrong address. Check `systemctl status
whatsmeow-gateway`, then that the Gateway URL in Odoo matches `WMG_LISTEN`.

**`Test Connection` says `Gateway error (401)`.** The API key in Odoo does not
match `WMG_API_KEY` in `gateway.env`. Re-read it there — the installer only
prints it on the run that generated it.

**Outbound works, nothing inbound.** The gateway cannot reach
`WMG_ODOO_WEBHOOK_URL`, or the secret is wrong. `journalctl -u
whatsmeow-gateway` shows the failed POSTs. Confirm by hand from the gateway host:

```bash
curl -i -X POST -H 'Content-Type: application/json' \
     -H "X-Webhook-Secret: $(grep ^WMG_WEBHOOK_SECRET= /opt/whatsmeow-gateway/gateway.env | cut -d= -f2-)" \
     -d '{"event":"ping"}' \
     http://127.0.0.1:8069/whatsmeow/webhook
```

`404 {"error":"unknown session"}` is the **good** answer here — it means the URL
is right and the secret matched a connection record; only the made-up session
was rejected. `401` means the secret matches no connection. A connection error
or a timeout means the URL is wrong or Odoo is not reachable from this host.

**The build fails with `missing go.sum entry`.** `go mod tidy` did not run or
had no network. The script runs it; a proxy that blocks `proxy.golang.org` is
the usual cause.

**`bind: address already in use` in the journal.** Something else holds the
port. Re-run with a free one — the explicit value replaces what is in
`gateway.env`, keeping the secrets:

```bash
sudo LISTEN_ADDR=127.0.0.1:8081 ./install.sh
```

**The service restarts in a loop.** `journalctl -u whatsmeow-gateway -n 50`, and
read the *first* error, not the last — the loop repeats it every five seconds.
A busy port and an unwritable `WMG_DATA_DIR` are the common two; the latter
means `ReadWritePaths` in the unit no longer matches the data directory in
`gateway.env` (they are set together, so this only happens after editing one by
hand). A failed install leaves the service stopped rather than cycling.

**A session shows Disconnected after the phone was offline.** Normal — the gateway
reconnects on its own. A session that stays disconnected has been unlinked from
the phone (*Linked devices*), and must be paired again.

## 9. Uninstalling

```bash
systemctl disable --now whatsmeow-gateway
rm /etc/systemd/system/whatsmeow-gateway.service
systemctl daemon-reload
rm -rf /opt/whatsmeow-gateway
# The pairing keys live here. Back it up first if you may want the sessions back.
rm -rf /var/lib/whatsmeow-gateway
userdel wagw
```

Log out of each session from Odoo (or from the phone's *Linked devices*) before
removing the data — otherwise the phone keeps showing a linked device that no
longer exists.
