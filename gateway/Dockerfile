# whatsmeow gateway as a container image.
#
#     docker build -t whatsmeow-gateway ./gateway
#     docker run -d --name whatsmeow-gateway \
#         -p 127.0.0.1:8080:8080 \
#         -v whatsmeow-gateway-data:/var/lib/whatsmeow-gateway \
#         --env-file gateway/gateway.env \
#         whatsmeow-gateway
#
# Or `docker compose up -d --build` from the repository root, which wires the
# volume, the port and the env file for you.
#
# Two stages: a Go toolchain that can build CGO, and a runtime carrying the
# binary and a CA bundle and nothing else. The image itself is disposable —
# everything that must survive it (the sqlite session stores and
# registry.json, i.e. the pairings) lives in the data volume.

# Keep in sync with the `go` line in go.mod: an older toolchain fails the build
# with "go.mod requires go >= x", which is loud, but there is no reason to hit it.
# A newer toolchain is fine: `--build-arg GO_VERSION=1.26` if that exact patch
# release has no published image tag.
ARG GO_VERSION=1.26.5

FROM golang:${GO_VERSION}-bookworm AS build
WORKDIR /src

# Modules first, so editing a .go file does not re-download the world. go.sum in
# this repo is tidy — the build below needs no `go mod tidy`, unlike install.sh,
# which tidies because it may have just moved the whatsmeow pin with `go get`.
COPY go.mod go.sum ./
RUN go mod download

# Every .go file, never a list — same reason install.sh copies them all: a
# hardcoded list does not fail when it goes stale, it builds a tree missing the
# new code. Tests are excluded by .dockerignore; `go build` ignores them anyway.
COPY *.go ./

# CGO is not optional: go-sqlite3 is a C library, and the session stores are
# sqlite. That is why the builder is the full golang image and not a scratch
# cross-compile — and why this must be built on the architecture it will run on.
RUN CGO_ENABLED=1 go build -trimpath -ldflags="-s -w" -o /out/whatsmeow-gateway .

FROM debian:bookworm-slim

# ca-certificates: TLS to WhatsApp's servers and to Odoo's webhook. Without it
# every connection dies with "x509: certificate signed by unknown authority".
# curl: only for the HEALTHCHECK below — bookworm-slim ships neither curl nor wget.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

# Unprivileged, like the systemd install's `wagw`. The data directory is created
# and chowned here so it is owned correctly when Docker seeds a named volume from
# the image; a *bind* mount keeps the host's ownership instead, so chown that
# directory to uid 10001 yourself or the gateway cannot write its stores.
RUN useradd --system --uid 10001 --home-dir /var/lib/whatsmeow-gateway \
        --shell /usr/sbin/nologin wagw \
 && mkdir -p /var/lib/whatsmeow-gateway \
 && chown wagw:wagw /var/lib/whatsmeow-gateway \
 && chmod 700 /var/lib/whatsmeow-gateway

COPY --from=build /out/whatsmeow-gateway /usr/local/bin/whatsmeow-gateway

# 0.0.0.0, not the binary's 127.0.0.1 default: loopback inside a container is the
# container's own, so a published port would answer nothing at all. Restricting
# who may reach it is the job of the port publication (`-p 127.0.0.1:8080:8080`)
# and the firewall, not of the bind address in here.
#
# An env file written for a bare-metal install carries WMG_LISTEN=127.0.0.1:8080
# and would undo this — the compose file re-asserts it for that reason.
ENV WMG_LISTEN=0.0.0.0:8080 \
    WMG_DATA_DIR=/var/lib/whatsmeow-gateway

# The pairing keys, the sqlite stores and registry.json. Declared so that the
# ordinary upgrade — `docker rm` then `docker run` on a new image — cannot take
# the pairings with it. Name the volume in `docker run -v` / compose anyway, or
# you get an anonymous one that is hard to identify later and easy to prune.
VOLUME ["/var/lib/whatsmeow-gateway"]

EXPOSE 8080
USER wagw

# /health is unauthenticated on purpose. The port is read back out of WMG_LISTEN
# so overriding the listen address does not leave this checking the wrong port.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${WMG_LISTEN##*:}/health" >/dev/null || exit 1

# Exec form, no shell wrapper: the gateway drains its webhook queues and
# disconnects the sessions on SIGTERM, and it only receives one if it is PID 1.
ENTRYPOINT ["/usr/local/bin/whatsmeow-gateway"]
