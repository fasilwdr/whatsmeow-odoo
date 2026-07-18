#!/usr/bin/env bash
#
# install.sh — install the whatsmeow gateway as a systemd service.
#
# One script for both distributions: Debian 12/13 and Ubuntu 22.04/24.04 differ
# in nothing this script touches (apt, useradd, /usr/sbin/nologin, systemd), so
# a separate Debian variant would only be a copy that drifts.
#
# Run as root:  sudo ./install.sh
#
# Re-running is safe: it keeps existing secrets, rebuilds and restarts.
#
# By default this builds the whatsmeow version pinned in gateway/go.mod. That pin is
# deliberate: whatsmeow has no stable releases and its API drifts, so an unattended
# upgrade is how a working gateway silently stops compiling. To move the pin on
# purpose:
#
#     sudo UPGRADE_WHATSMEOW=1 ./install.sh
#
# then commit the resulting go.mod/go.sum.
#
set -euo pipefail

# ---- configurable knobs (override via env) --------------------------------
INSTALL_DIR="${INSTALL_DIR:-/opt/whatsmeow-gateway}"
DATA_DIR="${DATA_DIR:-/var/lib/whatsmeow-gateway}"
SERVICE_USER="${SERVICE_USER:-wagw}"
LISTEN_ADDR="${LISTEN_ADDR:-127.0.0.1:8080}"
ODOO_WEBHOOK_URL="${ODOO_WEBHOOK_URL:-http://127.0.0.1:8069/whatsmeow/webhook}"
UPGRADE_WHATSMEOW="${UPGRADE_WHATSMEOW:-0}"
# ---------------------------------------------------------------------------

log() { echo -e "\033[1;32m[whatsmeow-install]\033[0m $*"; }
err() { echo -e "\033[1;31m[whatsmeow-install]\033[0m $*" >&2; }

[ "$(id -u)" -eq 0 ] || { err "Please run as root (sudo)."; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Find the gateway source by walking up from the script rather than counting
# "../.." levels: a hardcoded depth silently points at the wrong directory the
# moment this script moves, which is exactly how it once resolved to /home/dev.
if [ -z "${GATEWAY_SRC:-}" ]; then
  DIR="$SCRIPT_DIR"
  while : ; do
    if [ -f "$DIR/gateway/main.go" ]; then GATEWAY_SRC="$DIR/gateway"; break; fi
    [ "$DIR" = "/" ] && break
    DIR="$(dirname "$DIR")"
  done
fi

if [ -z "${GATEWAY_SRC:-}" ] || [ ! -f "$GATEWAY_SRC/main.go" ]; then
  err "Could not find the gateway source (a directory holding main.go + go.mod)."
  err "Looked upwards from $SCRIPT_DIR for gateway/main.go."
  err "Run this from a checkout, or point at it:  sudo GATEWAY_SRC=/path/to/gateway $0"
  exit 1
fi
log "Gateway source: $GATEWAY_SRC"

for f in go.mod go.sum; do
  [ -f "$GATEWAY_SRC/$f" ] || { err "$GATEWAY_SRC/$f is missing — incomplete checkout?"; exit 1; }
done

command -v apt-get >/dev/null 2>&1 || {
  err "This installer targets Debian/Ubuntu (apt-get not found)."; exit 1; }

# The toolchain must be at least what go.mod asks for, or the build fails with
# "go.mod requires go >= x". Take the version from there rather than hardcoding
# a second copy that has to be remembered when the pin moves.
GO_VERSION="${GO_VERSION:-$(awk '/^go /{print $2; exit}' "$GATEWAY_SRC/go.mod" 2>/dev/null)}"
GO_VERSION="${GO_VERSION:-1.26.5}"

# ---- build dependencies ----------------------------------------------------
log "Installing build dependencies..."
apt-get update -qq
# build-essential provides the gcc that go-sqlite3 needs (CGO).
apt-get install -y -qq git build-essential ca-certificates wget openssl curl

# ---- Go --------------------------------------------------------------------
NEED_GO=1
if command -v go >/dev/null 2>&1; then
  CURRENT="$(go version | awk '{print $3}' | sed 's/go//')"
  # Newer is fine — only an older toolchain than go.mod's pin needs replacing.
  if [ "$(printf '%s\n%s\n' "$GO_VERSION" "$CURRENT" | sort -V | head -1)" = "$GO_VERSION" ]; then
    NEED_GO=0; log "Go $CURRENT already present (need >= $GO_VERSION)."
  fi
fi
if [ "$NEED_GO" -eq 1 ]; then
  log "Installing Go $GO_VERSION..."
  ARCH="$(dpkg --print-architecture)"
  case "$ARCH" in amd64) GOARCH=amd64 ;; arm64) GOARCH=arm64 ;; *) GOARCH="$ARCH" ;; esac
  wget -qO /tmp/go.tgz "https://go.dev/dl/go${GO_VERSION}.linux-${GOARCH}.tar.gz"
  # go.dev serves an HTML page (with HTTP 200!) rather than a 404 when a .sha256
  # is missing, so only trust the response if it really is a hex digest.
  GO_SHA="$(wget -qO- "https://go.dev/dl/go${GO_VERSION}.linux-${GOARCH}.tar.gz.sha256" 2>/dev/null | tr -d '[:space:]' || true)"
  if [[ "$GO_SHA" =~ ^[0-9a-f]{64}$ ]]; then
    echo "${GO_SHA}  /tmp/go.tgz" | sha256sum -c - >/dev/null \
      || { err "Go tarball checksum mismatch — refusing to install."; exit 1; }
    log "Go tarball checksum verified."
  else
    log "No usable published checksum for go${GO_VERSION} — skipping verification (transport was HTTPS)."
  fi
  rm -rf /usr/local/go
  tar -C /usr/local -xzf /tmp/go.tgz
  rm -f /tmp/go.tgz
  echo 'export PATH=$PATH:/usr/local/go/bin' > /etc/profile.d/go.sh
fi
export PATH="$PATH:/usr/local/go/bin"

# ---- service user & directories --------------------------------------------
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  log "Creating system user $SERVICE_USER..."
  useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi
mkdir -p "$INSTALL_DIR" "$DATA_DIR"
chown "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR"
chmod 700 "$DATA_DIR"

# ---- build the gateway (CGO required by go-sqlite3) ------------------------
log "Building gateway..."
# Don't copy a locally-built binary or dev secrets into the install dir.
rm -f "$INSTALL_DIR/whatsmeow-gateway"
cp "$GATEWAY_SRC/main.go" "$GATEWAY_SRC/go.mod" "$GATEWAY_SRC/go.sum" "$INSTALL_DIR/"
cd "$INSTALL_DIR"

if [ "$UPGRADE_WHATSMEOW" = "1" ]; then
  log "UPGRADE_WHATSMEOW=1 — moving the pin to the latest whatsmeow."
  go get go.mau.fi/whatsmeow@latest
  go get github.com/mattn/go-sqlite3
else
  log "Building the whatsmeow version pinned in go.mod ($(awk '/go.mau.fi\/whatsmeow /{print $2}' go.mod))."
fi

# go get alone does not add every transitive go.sum entry (e.g. go.mau.fi/util's
# deps), and the build then fails with "missing go.sum entry". tidy fixes that.
go mod tidy
CGO_ENABLED=1 go build -o "$INSTALL_DIR/whatsmeow-gateway" .
chmod 755 "$INSTALL_DIR/whatsmeow-gateway"

if [ "$UPGRADE_WHATSMEOW" = "1" ] || [ -n "${COPY_BACK_PINS:-}" ]; then
  cp go.mod go.sum "$GATEWAY_SRC/" 2>/dev/null || true
  log "Updated go.mod/go.sum copied back to $GATEWAY_SRC — commit them."
fi

# ---- env file (preserve existing secrets on re-run) ------------------------
ENV_FILE="$INSTALL_DIR/gateway.env"
if [ -f "$ENV_FILE" ]; then
  log "Existing env file found — keeping current secrets and settings."
else
  log "Generating fresh API key and webhook secret..."
  cat > "$ENV_FILE" <<ENV
WMG_LISTEN=$LISTEN_ADDR
WMG_API_KEY=$(openssl rand -hex 32)
WMG_WEBHOOK_SECRET=$(openssl rand -hex 32)
WMG_ODOO_WEBHOOK_URL=$ODOO_WEBHOOK_URL
WMG_DATA_DIR=$DATA_DIR

# Optional tunables (media size/TTL, recipient-check budget, webhook workers)
# have working defaults and are left out on purpose. See gateway.env.example in
# the repo for the full list; add a line here and restart to change one.
ENV
fi
chmod 600 "$ENV_FILE"
chown "$SERVICE_USER:$SERVICE_USER" "$ENV_FILE"

# Report what is actually in the env file, not what the knobs defaulted to:
# on a re-run the file wins, and printing the defaults would be a lie.
get_env() { grep -E "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2-; }
API_KEY="$(get_env WMG_API_KEY)"
WEBHOOK_SECRET="$(get_env WMG_WEBHOOK_SECRET)"
ACTUAL_LISTEN="$(get_env WMG_LISTEN)"
ACTUAL_DATA_DIR="$(get_env WMG_DATA_DIR)"

# ---- systemd unit ----------------------------------------------------------
# Always generated from the knobs above: a checked-in unit file would hardcode
# paths and silently ignore INSTALL_DIR / DATA_DIR / SERVICE_USER overrides.
log "Installing systemd unit..."
cat > /etc/systemd/system/whatsmeow-gateway.service <<UNIT
[Unit]
Description=whatsmeow HTTP gateway for Odoo
After=network-online.target
Wants=network-online.target

[Service]
User=$SERVICE_USER
Group=$SERVICE_USER
EnvironmentFile=$ENV_FILE
ExecStart=$INSTALL_DIR/whatsmeow-gateway
Restart=always
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=$ACTUAL_DATA_DIR
ProtectHome=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now whatsmeow-gateway
systemctl restart whatsmeow-gateway

# ---- verify it actually came up --------------------------------------------
# A listen address may be host-less (":8080") or a wildcard ("0.0.0.0:8080");
# neither is dialable as written, so probe the loopback on that port instead.
PROBE_HOST="${ACTUAL_LISTEN%:*}"
PROBE_PORT="${ACTUAL_LISTEN##*:}"
case "$PROBE_HOST" in ""|"0.0.0.0"|"[::]"|"::") PROBE_HOST="127.0.0.1" ;; esac
PROBE_URL="http://${PROBE_HOST}:${PROBE_PORT}/health"

log "Waiting for the gateway to answer on ${PROBE_URL} ..."
OK=0
for _ in $(seq 1 15); do
  if curl -fsS -m 2 "$PROBE_URL" >/dev/null 2>&1; then OK=1; break; fi
  sleep 1
done
if [ "$OK" -ne 1 ]; then
  err "Gateway did not become healthy. Recent logs:"
  journalctl -u whatsmeow-gateway -n 30 --no-pager >&2 || true
  exit 1
fi
log "Gateway is healthy."

# ---- summary ---------------------------------------------------------------
echo
log "Installation complete."
echo "--------------------------------------------------------------------"
echo "  In Odoo: WhatsApp > Configuration > Gateways > New"
echo
echo "    Gateway URL     : http://${PROBE_HOST}:${PROBE_PORT}"
echo "    API Key         : ${API_KEY}"
echo "    Webhook Secret  : ${WEBHOOK_SECRET}"
echo
echo "  Health check      : curl ${PROBE_URL}"
echo "  Logs              : journalctl -u whatsmeow-gateway -f"
echo "  Session stores    : ${ACTUAL_DATA_DIR}  <-- back this up, it holds the pairing keys"
echo "--------------------------------------------------------------------"
