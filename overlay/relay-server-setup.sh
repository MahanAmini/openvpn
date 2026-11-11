#!/usr/bin/env bash
# =============================================================================
#  relay-server-setup.sh  -  REALITY relay for a FOREIGN "middle" server
#
#      user -> IRAN server (GassLight Tunnel panel) --REALITY--> THIS relay --> MAIN OpenVPN / GassLight server
#
#  Run it on every foreign relay server (as root, Debian / Ubuntu). It installs Xray, creates a VLESS + REALITY
#  inbound that looks like a normal HTTPS site, and lets traffic go ONLY to your main server. At the end it prints a
#  vless:// link: paste it in the tunnel panel (Relays -> Import).
#
#  Usage:
#    sudo bash relay-server-setup.sh --main MAIN_SERVER_IP [options]
#
#  Options:
#    --main IP          IPv4 of the main OpenVPN / GassLight server (required). The relay can reach nothing else.
#    --port N           port the relay listens on (default 443; anything else is easier for censors to spot)
#    --dest HOST:PORT   real HTTPS site the relay imitates (default www.microsoft.com:443). It must be reachable
#                       from this server and speak TLS 1.3 + HTTP/2. Pick one that is not blocked in Iran.
#    --sni NAME         server name clients present (default: the host of --dest)
#    --name NAME        label shown in the panel (default: this server's hostname)
#    --ports LIST       ports of the main server the relay may reach (default 1024-65535; the relay never
#                       reaches SSH or other low ports). Ranges / lists: 1195-3000,8088
#    --allow-from IP[,IP]  only forward traffic that comes from these addresses (your Iran server). Strongly advised.
#    --public-ip IP     public address written into the link (default: detected)
#    --xray-zip FILE    install Xray from a zip you uploaded (when GitHub is not reachable from this server)
#    --regen            create a NEW uuid / keys / short id (old panel entries stop working)
#    --skip-dest-check  do not test the --dest site
#    --show             print the link of the installed relay again
#    --uninstall        remove the relay (the Xray binary is kept)
# =============================================================================
set -eu

MAIN=""; PORT=443; DEST="www.microsoft.com:443"; SNI=""; NAME=""; PORTS="1024-65535"; ALLOW=""
PUBLIC_IP=""; XRAY_ZIP=""; REGEN=0; SKIP_DEST=0; ACTION="install"
die()  { echo "ERROR: $*" >&2; exit 1; }
info() { echo "[*] $*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --main)           MAIN="${2:-}"; shift 2 ;;
    --port)           PORT="${2:-}"; shift 2 ;;
    --dest)           DEST="${2:-}"; shift 2 ;;
    --sni)            SNI="${2:-}"; shift 2 ;;
    --name)           NAME="${2:-}"; shift 2 ;;
    --ports)          PORTS="${2:-}"; shift 2 ;;
    --allow-from)     ALLOW="${2:-}"; shift 2 ;;
    --public-ip)      PUBLIC_IP="${2:-}"; shift 2 ;;
    --xray-zip)       XRAY_ZIP="${2:-}"; shift 2 ;;
    --regen)          REGEN=1; shift ;;
    --skip-dest-check) SKIP_DEST=1; shift ;;
    --show)           ACTION="show"; shift ;;
    --uninstall)      ACTION="uninstall"; shift ;;
    -h|--help)        sed -n '2,32p' "$0"; exit 0 ;;
    *)                die "unknown option: $1 (use --help)" ;;
  esac
done
[ "$(id -u)" -eq 0 ] || die "run as root (sudo bash $0 ...)"

XRAY=/usr/local/bin/xray
DIR=/etc/gl-relay
ENVF=$DIR/relay.env
CONF=$DIR/config.json
UNIT=/etc/systemd/system/gl-relay.service
SVC_USER=gl-relay

ipv4_ok() {
  echo "$1" | grep -Eq '^([0-9]{1,3}\.){3}[0-9]{1,3}$' || return 1
  for o in $(echo "$1" | tr '.' ' '); do [ "$o" -le 255 ] || return 1; done
}
rand_hex() { head -c 64 /dev/urandom | od -An -tx1 | tr -d ' \n' | head -c "$1"; }

make_link() {   # reads the values from relay.env
  # shellcheck disable=SC1090
  . "$ENVF"
  echo "vless://${UUID}@${PUBLIC_IP}:${PORT}?encryption=none&flow=xtls-rprx-vision&security=reality&sni=${SNI}&fp=chrome&pbk=${PBK}&sid=${SID}&type=tcp#${NAME}"
}

# ------------------------------------------------------------------ show / uninstall
if [ "$ACTION" = "show" ]; then
  [ -f "$ENVF" ] || die "the relay is not installed"
  make_link; exit 0
fi
if [ "$ACTION" = "uninstall" ]; then
  systemctl disable --now gl-relay 2>/dev/null || true
  rm -f "$UNIT"; rm -rf "$DIR"
  userdel "$SVC_USER" 2>/dev/null || true
  systemctl daemon-reload 2>/dev/null || true
  info "relay removed. The Xray program ($XRAY) was kept; close the port in your firewall if you opened it."
  exit 0
fi

# ------------------------------------------------------------------ checks
[ -n "$MAIN" ] || die "--main MAIN_SERVER_IP is required (see --help)"
ipv4_ok "$MAIN" || die "--main must be an IPv4 address"
case "$PORT" in ''|*[!0-9]*) die "--port must be a number" ;; esac
[ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || die "--port must be 1-65535"
echo "$DEST" | grep -Eq '^[A-Za-z0-9.-]+:[0-9]{1,5}$' || die "--dest must look like host:443"
[ -n "$SNI" ] || SNI="${DEST%:*}"
echo "$SNI" | grep -Eq '^[A-Za-z0-9.-]+\.[A-Za-z0-9-]+$' || die "--sni must be a host name such as www.example.com"
[ -n "$NAME" ] || NAME="$(hostname 2>/dev/null || echo relay)"
NAME="$(printf '%s' "$NAME" | tr -c 'A-Za-z0-9._-' '-' | head -c 40)"
echo "$PORTS" | grep -Eq '^[0-9]+(-[0-9]+)?(,[0-9]+(-[0-9]+)?)*$' || die "--ports: use numbers and ranges like 1195-3000,8088"
if [ -n "$ALLOW" ]; then
  for ip in $(echo "$ALLOW" | tr ',' ' '); do ipv4_ok "$ip" || die "--allow-from: '$ip' is not an IPv4 address"; done
else
  echo "WARNING: no --allow-from. Anyone who knows the link's uuid and keys could use this relay." >&2
  echo "         Re-run with --allow-from YOUR_IRAN_SERVER_IP to lock it down." >&2
fi
[ -z "$PUBLIC_IP" ] || ipv4_ok "$PUBLIC_IP" || die "--public-ip must be an IPv4 address"

# port must be free (our own earlier run is fine)
if command -v ss >/dev/null 2>&1; then
  holder="$(ss -H -ltnp "sport = :$PORT" 2>/dev/null | head -n1 || true)"
  if [ -n "$holder" ] && ! echo "$holder" | grep -q '"xray"'; then
    die "port $PORT is already used by another program: $holder  (stop it, or choose --port)"
  fi
elif { [ "$(sed -n 's/^PORT=//p' "$ENVF" 2>/dev/null | head -n1)" != "$PORT" ] || ! systemctl is-active --quiet gl-relay 2>/dev/null; } \
     && (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then
  die "port $PORT is already used by another program (stop it, or choose --port)"   # no 'ss' on this server
fi

# ------------------------------------------------------------------ packages
need=""
for c in curl unzip openssl; do command -v "$c" >/dev/null 2>&1 || need="$need $c"; done
if [ -n "$need" ]; then
  command -v apt-get >/dev/null 2>&1 || die "missing:$need (install them, then run again)"
  info "installing:$need"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq >/dev/null && apt-get install -y -qq ca-certificates$need >/dev/null
fi

# ------------------------------------------------------------------ Xray
install_xray() {
  tmp="$(mktemp -d)"
  if [ -n "$XRAY_ZIP" ]; then
    [ -f "$XRAY_ZIP" ] || die "--xray-zip: file not found"
    cp "$XRAY_ZIP" "$tmp/x.zip"
  else
    case "$(uname -m)" in
      x86_64|amd64)  asset=Xray-linux-64.zip ;;
      aarch64|arm64) asset=Xray-linux-arm64-v8a.zip ;;
      *) die "unsupported CPU $(uname -m); install Xray yourself and use --xray-zip" ;;
    esac
    base="https://github.com/XTLS/Xray-core/releases/latest/download"
    info "downloading Xray ($asset)..."
    curl -fsSL --retry 3 -m 180 -o "$tmp/x.zip" "$base/$asset" \
      || die "could not download Xray from GitHub. Upload the zip to this server and run again with --xray-zip FILE"
    if curl -fsSL -m 30 -o "$tmp/x.dgst" "$base/$asset.dgst" 2>/dev/null; then
      want="$(sed -n 's/^SHA2-256= *//p' "$tmp/x.dgst" | head -n1)"
      have="$(sha256sum "$tmp/x.zip" | awk '{print $1}')"
      if [ -n "$want" ] && [ "$want" != "$have" ]; then die "Xray download is corrupt (checksum mismatch)"; fi
    fi
  fi
  unzip -o -q "$tmp/x.zip" xray -d "$tmp" || die "the zip does not contain the xray program"
  install -m 755 "$tmp/xray" "$XRAY"
  rm -rf "$tmp"
}
if [ ! -x "$XRAY" ] || [ -n "$XRAY_ZIP" ]; then install_xray; fi
"$XRAY" version >/dev/null 2>&1 || die "$XRAY does not run on this server"

# ------------------------------------------------------------------ the site we imitate
if [ "$SKIP_DEST" -eq 0 ]; then
  out="$(echo | timeout 12 openssl s_client -connect "$DEST" -servername "$SNI" -tls1_3 -alpn h2 2>&1 || true)"
  if ! echo "$out" | grep -q "TLSv1.3"; then
    die "$DEST does not answer with TLS 1.3 from this server. Pick another --dest (or --skip-dest-check if you are sure)."
  fi
  echo "$out" | grep -qi "ALPN protocol: *h2" || echo "WARNING: $DEST did not select HTTP/2; REALITY works best with a site that does." >&2
fi

# ------------------------------------------------------------------ credentials (kept between runs)
mkdir -p "$DIR"; chmod 750 "$DIR"
if [ ! -f "$ENVF" ] || [ "$REGEN" -eq 1 ]; then
  UUID="$("$XRAY" uuid)"
  KEYS="$("$XRAY" x25519)"
  PRIV="$(echo "$KEYS" | sed -n 's/^[Pp]rivate[ _-]\{0,1\}[Kk]ey: *//p' | head -n1)"
  PBK="$(echo "$KEYS" | sed -n 's/^[Pp]ublic[ _-]\{0,1\}[Kk]ey: *//p;s/^Password (PublicKey): *//p' | head -n1)"
  SID="$(rand_hex 16)"
  echo "$PRIV" | grep -Eq '^[A-Za-z0-9_-]{43}$' && echo "$PBK" | grep -Eq '^[A-Za-z0-9_-]{43}$' \
    || die "could not read the keys printed by 'xray x25519' (unexpected format)"
else
  getv() { sed -n "s/^$1=//p" "$ENVF" | head -n1; }      # keep the old credentials, but take new options from the command line
  UUID="$(getv UUID)"; PRIV="$(getv PRIV)"; PBK="$(getv PBK)"; SID="$(getv SID)"
fi
echo "$UUID" | grep -Eq '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' || die "bad uuid"

if [ -z "$PUBLIC_IP" ]; then
  for u in https://api.ipify.org https://ifconfig.me/ip https://icanhazip.com; do
    PUBLIC_IP="$(curl -fsS -m 6 "$u" 2>/dev/null | tr -d ' \n' || true)"
    ipv4_ok "$PUBLIC_IP" && break || PUBLIC_IP=""
  done
fi
if [ -z "$PUBLIC_IP" ] && command -v ip >/dev/null 2>&1; then
  PUBLIC_IP="$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -n1)"
fi
ipv4_ok "$PUBLIC_IP" || die "could not detect this server's public IP; pass --public-ip"

umask 077
cat > "$ENVF" <<EOF
UUID=$UUID
PRIV=$PRIV
PBK=$PBK
SID=$SID
PORT=$PORT
DEST=$DEST
SNI=$SNI
NAME=$NAME
MAIN=$MAIN
PUBLIC_IP=$PUBLIC_IP
EOF

# ------------------------------------------------------------------ Xray config
SRC=""
if [ -n "$ALLOW" ]; then
  SRC='"source": ['"$(echo "$ALLOW" | tr ',' '\n' | sed 's/.*/"&"/' | paste -sd, -)"'],'
fi
PORTRANGE="$PORTS"
cat > "$CONF" <<EOF
{
  "log": { "loglevel": "warning" },
  "inbounds": [
    {
      "tag": "in", "listen": "0.0.0.0", "port": $PORT, "protocol": "vless",
      "settings": { "clients": [ { "id": "$UUID", "flow": "xtls-rprx-vision" } ], "decryption": "none" },
      "streamSettings": {
        "network": "tcp", "security": "reality",
        "realitySettings": { "show": false, "dest": "$DEST", "xver": 0, "serverNames": [ "$SNI" ],
                             "privateKey": "$PRIV", "shortIds": [ "$SID" ] }
      }
    }
  ],
  "outbounds": [
    { "tag": "direct", "protocol": "freedom", "settings": { "finalRules": [ { "action": "allow" } ] } },
    { "tag": "block", "protocol": "blackhole" }
  ],
  "routing": {
    "domainStrategy": "AsIs",
    "rules": [
      { "type": "field", "inboundTag": [ "in" ], $SRC "ip": [ "$MAIN" ], "port": "$PORTRANGE", "outboundTag": "direct" },
      { "type": "field", "network": "tcp,udp", "outboundTag": "block" }
    ]
  }
}
EOF
"$XRAY" run -test -config "$CONF" >/tmp/.gl-relay-test.log 2>&1 || { cat /tmp/.gl-relay-test.log >&2; rm -f /tmp/.gl-relay-test.log; die "the generated Xray config is invalid (see above)"; }
rm -f /tmp/.gl-relay-test.log

# ------------------------------------------------------------------ service (own user, restarts by itself)
id "$SVC_USER" >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin "$SVC_USER"
chown -R root:"$SVC_USER" "$DIR"; chmod 750 "$DIR"; chmod 640 "$CONF"; chmod 600 "$ENVF"
cat > "$UNIT" <<EOF
[Unit]
Description=GassLight relay (Xray VLESS + REALITY)
After=network-online.target
Wants=network-online.target

[Service]
User=$SVC_USER
ExecStart=$XRAY run -config $CONF
Restart=always
RestartSec=3
LimitNOFILE=1048576
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
NoNewPrivileges=true
ProtectSystem=full
ProtectHome=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
chmod 644 "$UNIT"
systemctl daemon-reload
systemctl enable gl-relay >/dev/null 2>&1 || true
systemctl restart gl-relay
sleep 1
systemctl is-active --quiet gl-relay || { journalctl -u gl-relay -n 20 --no-pager 2>/dev/null || true; die "the relay did not start (see log above)"; }

# firewall: ufw only (cloud firewalls are up to you)
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  ufw allow "$PORT"/tcp >/dev/null && info "ufw: port $PORT/tcp opened"
fi

LINK="$(make_link)"
printf '%s\n' "$LINK" > "$DIR/link.txt"; chmod 600 "$DIR/link.txt"

cat <<EOF

=====================================================================
  Relay "$NAME" is running (starts on boot, restarts on failure)

  Paste this link in the tunnel panel (Relays -> Import):

  $LINK

  Listens on        : TCP $PORT   (open it in your provider's firewall / security group)
  Imitates          : $DEST  (SNI $SNI)
  Can reach only    : $MAIN  ports $PORTS
  Accepts only from : ${ALLOW:-ANY address (not recommended)}

  Show the link again : sudo bash $0 --show
  Remove this relay   : sudo bash $0 --uninstall
=====================================================================
EOF
