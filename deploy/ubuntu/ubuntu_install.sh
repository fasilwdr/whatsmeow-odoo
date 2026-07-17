#!/usr/bin/env bash
#
# ubuntu_install.sh — install the whatsmeow gateway as a systemd service.
# Target: Ubuntu 22.04 / 24.04. Run as root:  sudo ./ubuntu_install.sh
#
# Re-running is safe: it keeps existing secrets and just rebuilds/restarts.
#
set -euo pipefail

# ---- configurable knobs (override via env) --------------------------------
GO_VERSION="${GO_VERSION:-1.23.4}"
INSTALL_DIR="${INSTALL_DIR:-/opt/whatsmeow-gateway}"
DATA_DIR="${DATA_DIR:-/var/lib/whatsmeow-gateway}"
SERVICE_USER="${SERVICE_USER:-wagw}"
LISTEN_ADDR="${LISTEN_ADDR:-127.0.0.1:8080}"
ODOO_WEBHOOK_URL="${ODOO_WEBHOOK_URL:-http://127.0.0.1:8069/whatsmeow/webhook}"
# ---------------------------------------------------------------------------

log() { echo -e "\033[1;32m[whatsmeow-install]\033[0m $*"; }
err() { echo -e "\033[1;31m[whatsmeow-install]\033[0m $*" >&2; }

[ "$(id -u)" -eq 0 ] || { err "Please run as root (sudo)."; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
GATEWAY_SRC="${GATEWAY_SRC:-$REPO_ROOT/gateway}"

[ -f "$GATEWAY_SRC/main.go" ] || {
  err "gateway source not found at $GATEWAY_SRC — set GATEWAY_SRC=/path/to/gateway"; exit 1; }

# ---- build dependencies ----------------------------------------------------
log "Installing build dependencies..."
apt-get update -qq
apt-get install -y -qq git build-essential ca-certificates wget openssl curl

# ---- Go --------------------------------------------------------------------
NEED_GO=1
if command -v go >/dev/null 2>&1; then
  CURRENT="$(go version | awk '{print $3}' | sed 's/go//')"
  [ "$CURRENT" = "$GO_VERSION" ] && { NEED_GO=0; log "Go $GO_VERSION already present."; }
fi
if [ "$NEED_GO" -eq 1 ]; then
  log "Installing Go $GO_VERSION..."
  ARCH="$(dpkg --print-architecture)"
  case "$ARCH" in amd64) GOARCH=amd64 ;; arm64) GOARCH=arm64 ;; *) GOARCH="$ARCH" ;; esac
  wget -qO /tmp/go.tgz "https://go.dev/dl/go${GO_VERSION}.linux-${GOARCH}.tar.gz"
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

# ---- build the gateway (CGO required by go-sqlite3) ------------------------
log "Building gateway..."
cp -r "$GATEWAY_SRC/." "$INSTALL_DIR/"
cd "$INSTALL_DIR"
[ -f go.mod ] || go mod init whatsmeow-gateway
go get go.mau.fi/whatsmeow@latest github.com/mattn/go-sqlite3
CGO_ENABLED=1 go build -o "$INSTALL_DIR/whatsmeow-gateway" .

# ---- env file (preserve existing secrets on re-run) ------------------------
ENV_FILE="$INSTALL_DIR/gateway.env"
if [ -f "$ENV_FILE" ]; then
  log "Existing env file found — keeping current secrets."
  API_KEY="$(grep -E '^WMG_API_KEY=' "$ENV_FILE" | cut -d= -f2-)"
  WEBHOOK_SECRET="$(grep -E '^WMG_WEBHOOK_SECRET=' "$ENV_FILE" | cut -d= -f2-)"
else
  log "Generating fresh API key and webhook secret..."
  API_KEY="$(openssl rand -hex 32)"
  WEBHOOK_SECRET="$(openssl rand -hex 32)"
  cat > "$ENV_FILE" <<ENV
WMG_LISTEN=$LISTEN_ADDR
WMG_API_KEY=$API_KEY
WMG_WEBHOOK_SECRET=$WEBHOOK_SECRET
WMG_ODOO_WEBHOOK_URL=$ODOO_WEBHOOK_URL
WMG_DATA_DIR=$DATA_DIR
ENV
fi
chmod 600 "$ENV_FILE"
chown "$SERVICE_USER:$SERVICE_USER" "$ENV_FILE"

# ---- systemd unit ----------------------------------------------------------
log "Installing systemd unit..."
if [ -f "$SCRIPT_DIR/whatsmeow-gateway.service" ]; then
  install -m 644 "$SCRIPT_DIR/whatsmeow-gateway.service" \
    /etc/systemd/system/whatsmeow-gateway.service
else
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
ReadWritePaths=$DATA_DIR
ProtectHome=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
UNIT
fi

systemctl daemon-reload
systemctl enable --now whatsmeow-gateway
sleep 2
systemctl --no-pager --full status whatsmeow-gateway || true

# ---- summary ---------------------------------------------------------------
echo
log "Installation complete."
echo "--------------------------------------------------------------------"
echo "  In Odoo, create a  whatsmeow.connection  record with:"
echo
echo "    Gateway URL     : http://${LISTEN_ADDR}"
echo "    API Key         : ${API_KEY}"
echo "    Webhook Secret  : ${WEBHOOK_SECRET}"
echo
echo "  Health check      : curl http://${LISTEN_ADDR}/health"
echo "  Logs              : journalctl -u whatsmeow-gateway -f"
echo "--------------------------------------------------------------------"
