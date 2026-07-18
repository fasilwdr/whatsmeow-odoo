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

Copy the repository to the server (or clone it), then:

```bash
cd whatsmeow-odoo/deploy
sudo ./install.sh
```

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
| `GATEWAY_SRC` | `../gateway` | source directory, if not run from the repo |
| `GO_VERSION` | from `gateway/go.mod` | Go toolchain to install |
| `UPGRADE_WHATSMEOW` | `0` | see [§7](#7-upgrading-whatsmeow) |

These only take effect on a **first** install. On a re-run the existing
`gateway.env` wins; edit that file and `systemctl restart whatsmeow-gateway`.

## 3. Wiring it to Odoo

The script ends by printing a gateway URL, an API key and a webhook secret.

1. Put the three add-ons on Odoo's `addons_path`: `whatsmeow` (required), plus
   `whatsmeow_discuss` and `whatsmeow_template` if you want them. Restart Odoo
   and install `whatsmeow` from Apps.
2. **WhatsApp → Configuration → Gateways → New.** Paste the URL, API key and
   webhook secret, save, and press **Test Connection**. It must go green before
   anything else will work.
3. **WhatsApp → Configuration → Sessions → New.** Pick the gateway and give the
   session a code — lowercase letters, digits, `_` and `-`, up to 40 characters.
   The code names the session's store on disk, so treat it as permanent.
4. Press **Start / Pair**, scan the QR with WhatsApp on the phone
   (*Settings → Linked devices → Link a device*). The QR expires in seconds;
   press **Refresh Status** for a new one. The status goes **Connected** once
   paired.

Send a test message from Odoo, and reply from the phone to confirm the webhook
comes back. If outbound works but inbound never arrives, the problem is
`WMG_ODOO_WEBHOOK_URL` (see [§8](#8-troubleshooting)).

Odoo drives the rest on four crons (queue, inbound media, recipient validation,
session status), so the Odoo cron worker must actually be running — with
`--max-cron-threads=0` messages queue and never leave.

### Reaching Odoo across hosts

If the gateway and Odoo are on different machines, `ODOO_WEBHOOK_URL` must be an
address the gateway can resolve, and the gateway's own `LISTEN_ADDR` must be one
Odoo can reach — `127.0.0.1` will not do for either.

Both directions carry a shared secret in a header and are otherwise unprotected,
so anything crossing a network you do not control belongs behind TLS: terminate
it at a reverse proxy in front of each side and keep the services themselves on
the loopback. Point Odoo's Gateway URL at the proxy, not at the binary.

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
restored into a corrupt state. `gateway.env` is worth keeping too — the API key
and webhook secret in it are what the Odoo connection record expects; restoring
data without it means editing the credentials in Odoo.

The `media/` subdirectory inside it is a staging area, not state — inbound files
wait there for Odoo to fetch, and anything uncollected is deleted after
`WMG_MEDIA_TTL_HOURS` (24). It does not need backing up, but it does need room:
WhatsApp allows files up to ~100 MB.

## 6. Deploying a new version

```bash
git pull
cd deploy && sudo ./install.sh
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

**The service restarts in a loop.** `journalctl -u whatsmeow-gateway -n 50`.
A port already in use and an unwritable `WMG_DATA_DIR` are the common two;
the latter means `ReadWritePaths` in the unit no longer matches the data
directory in `gateway.env` (they are set together, so this only happens after
editing one by hand).

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
