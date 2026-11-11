#!/usr/bin/env bash
# =============================================================================
#  overlay-panel-setup.sh  -  Overlay panel setup
#
#  Installs the GassLight Tunnel panel together with Xray on the IRAN server, automatically:
#    1. Xray (official XTLS installer) + the "gl-xray" service the panel uses
#    2. the panel (tunnel_panel.py) in /opt/gl-tunnel, with Python + Flask
#    3. the "gl-tunnel" service (starts on boot) with a random 8-digit admin password
#  At the end it prints:  username / password / url
#
#  Usage (put tunnel_panel.py in the same folder as this script, or in the folder you run it from):
#    sudo bash overlay-panel-setup.sh [options]
#
#  Options:
#    --port N         panel port (default 8090)
#    --password PW    use this password instead of a random 8-digit one (6+ chars: letters, digits . _ - @)
#    --panel FILE     path of tunnel_panel.py (default: next to this script, or in the current folder)
#    --repo-url URL   download tunnel_panel.py from URL/tunnel_panel.py when it is not found locally
#    --xray-zip FILE  install Xray from a zip you uploaded (when GitHub is not reachable from this server)
# =============================================================================
set -eu

PORT=8090; PASSWORD=""; PANEL=""; REPO_URL="${OVERLAY_REPO_URL:-}"; XRAY_ZIP=""
die()  { echo "ERROR: $*" >&2; exit 1; }
info() { echo "[*] $*"; }
HERE="$(cd "$(dirname "$0")" 2>/dev/null && pwd || echo .)"

while [ $# -gt 0 ]; do
  case "$1" in
    --port)      PORT="${2:-}"; shift 2 ;;
    --password)  PASSWORD="${2:-}"; shift 2 ;;
    --panel)     PANEL="${2:-}"; shift 2 ;;
    --repo-url)  REPO_URL="${2:-}"; shift 2 ;;
    --xray-zip)  XRAY_ZIP="${2:-}"; shift 2 ;;
    -h|--help)   sed -n '2,21p' "$0"; exit 0 ;;
    *)           die "unknown option: $1 (use --help)" ;;
  esac
done
[ "$(id -u)" -eq 0 ] || die "run as root (sudo bash $0)"
command -v apt-get >/dev/null 2>&1 || die "this script needs Debian / Ubuntu (apt)"
command -v systemctl >/dev/null 2>&1 || die "systemd is required"

DATA=/opt/gl-tunnel
UNIT=/etc/systemd/system/gl-tunnel.service

# ------------------------------------------------------------------ checks
case "$PORT" in ''|*[!0-9]*) die "--port must be a number" ;; esac
[ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || die "--port must be 1-65535"
if [ -n "$PASSWORD" ]; then
  [ "${#PASSWORD}" -ge 6 ] || die "--password must be at least 6 characters"
  case "$PASSWORD" in *[!A-Za-z0-9._@-]*) die "--password: use only letters, digits and . _ - @" ;; esac
fi
if [ -n "$REPO_URL" ]; then
  echo "$REPO_URL" | grep -Eq '^https?://[A-Za-z0-9._~:/@%+-]+$' || die "--repo-url must be a plain http(s) address without spaces or quotes"
fi
if [ -n "$XRAY_ZIP" ]; then [ -f "$XRAY_ZIP" ] || die "--xray-zip: file not found"; XRAY_ZIP="$(readlink -f "$XRAY_ZIP")"; fi

port_in_use() {
  if command -v ss >/dev/null 2>&1; then ss -H -ltn "sport = :$1" 2>/dev/null | grep -q .
  else (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; fi
}
# the port may be held by our own running panel (re-run): that is fine
if port_in_use "$PORT" && ! systemctl is-active --quiet gl-tunnel 2>/dev/null; then
  die "port $PORT is already used by another program (stop it, or choose --port)"
fi

# the panel file
TMPD="$(mktemp -d)"; trap 'rm -rf "$TMPD"' EXIT
if [ -z "$PANEL" ]; then
  if   [ -f "$HERE/tunnel_panel.py" ]; then PANEL="$HERE/tunnel_panel.py"
  elif [ -f "./tunnel_panel.py" ];     then PANEL="./tunnel_panel.py"
  elif [ -n "$REPO_URL" ]; then
    command -v curl >/dev/null 2>&1 || { apt-get update -qq >/dev/null && apt-get install -y -qq ca-certificates curl >/dev/null; }
    info "downloading tunnel_panel.py..."
    curl -fsSL --retry 3 -m 60 -o "$TMPD/tunnel_panel.py" "${REPO_URL%/}/tunnel_panel.py" || die "could not download ${REPO_URL%/}/tunnel_panel.py"
    PANEL="$TMPD/tunnel_panel.py"
  fi
fi
[ -f "$PANEL" ] || die "tunnel_panel.py not found (put it next to this script, or use --panel FILE / --repo-url URL)"
head -c 300 "$PANEL" | grep -q "python" || die "$PANEL does not look like the panel (wrong file or wrong URL?)"

# ------------------------------------------------------------------ 1. Xray (official installer) + gl-xray service
export DEBIAN_FRONTEND=noninteractive
if ! command -v curl >/dev/null 2>&1; then
  info "installing curl..."
  apt-get update -qq >/dev/null && apt-get install -y -qq ca-certificates curl >/dev/null
fi
if [ ! -x /usr/local/bin/xray ] || [ -n "$XRAY_ZIP" ]; then
  info "installing Xray (official XTLS installer)..."
  if [ -n "$XRAY_ZIP" ]; then
    bash -c "$(curl -fsSL https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install --local "$XRAY_ZIP" \
      || die "the Xray installer failed (see above)"
  else
    bash -c "$(curl -fsSL https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install \
      || die "the Xray installer failed (see above). If GitHub is not reachable from this server, upload the Xray zip and use --xray-zip FILE"
  fi
fi
[ -x /usr/local/bin/xray ] || die "Xray was not installed at /usr/local/bin/xray"

info "creating the gl-xray service..."
printf '%s\n' '[Unit]' 'Description=GassLight Tunnel Xray' 'After=network-online.target' 'Wants=network-online.target' '' '[Service]' 'ExecStart=/usr/local/bin/xray run -config /opt/gl-tunnel/xray/config.json' 'Restart=always' 'RestartSec=3' 'LimitNOFILE=1048576' '' '[Install]' 'WantedBy=multi-user.target' > /etc/systemd/system/gl-xray.service
systemctl daemon-reload

# ------------------------------------------------------------------ 2. the panel files
mkdir -p "$DATA"
if [ "$(readlink -f "$PANEL")" != "$(readlink -f "$DATA/tunnel_panel.py")" ]; then cp "$PANEL" "$DATA/tunnel_panel.py"; fi
chmod 700 "$DATA/tunnel_panel.py"

info "installing Python and Flask..."
apt-get update -qq >/dev/null
apt-get install -y -qq python3 python3-flask python3-markupsafe python3-werkzeug >/dev/null
python3 -m py_compile "$DATA/tunnel_panel.py" || die "tunnel_panel.py has a syntax error"
rm -rf "$DATA/__pycache__"

# ------------------------------------------------------------------ 3. the gl-tunnel service
# The password only matters the first time (it creates the admin). If the panel already has an admin, keep it.
FIRST=1; [ -f "$DATA/auth.json" ] && FIRST=0
if [ "$FIRST" -eq 1 ]; then
  [ -n "$PASSWORD" ] || PASSWORD="$(tr -dc '0-9' < /dev/urandom | head -c 8)"
  [ "${#PASSWORD}" -ge 6 ] || die "could not generate a password"
else
  old="$(sed -n 's/^Environment="PANEL_PASSWORD=\(.*\)"$/\1/p' "$UNIT" 2>/dev/null | head -n1 || true)"
  [ -z "$PASSWORD" ] || echo "NOTE: the panel already has an admin, so --password is ignored (change it inside the panel)." >&2
  PASSWORD="${old:-unused}"
fi

info "creating the gl-tunnel service..."
cat > "$UNIT" <<EOF
[Unit]
Description=GassLight Tunnel Panel
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$DATA
Environment="PANEL_PASSWORD=$PASSWORD"
Environment="TUNNEL_PORT=$PORT"
ExecStart=/usr/bin/python3 $DATA/tunnel_panel.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
chmod 600 "$UNIT"     # the file holds the password: only root may read it
systemctl daemon-reload
systemctl enable gl-tunnel >/dev/null 2>&1
systemctl restart gl-tunnel

ok=0
for _ in $(seq 1 20); do
  if [ -f "$DATA/auth.json" ] && port_in_use "$PORT"; then ok=1; break; fi
  sleep 1
done
if [ "$ok" -ne 1 ]; then
  journalctl -u gl-tunnel -n 25 --no-pager 2>/dev/null || true
  die "the panel did not start (see log above)"
fi

if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  ufw allow "$PORT"/tcp >/dev/null && info "ufw: port $PORT/tcp opened"
fi

# ------------------------------------------------------------------ result
ipv4_ok() { echo "$1" | grep -Eq '^([0-9]{1,3}\.){3}[0-9]{1,3}$'; }
IP=""
for u in https://api.ipify.org https://ifconfig.me/ip https://icanhazip.com; do
  IP="$(curl -fsS -m 5 "$u" 2>/dev/null | tr -d ' \n' || true)"
  ipv4_ok "$IP" && break || IP=""
done
if [ -z "$IP" ] && command -v ip >/dev/null 2>&1; then
  IP="$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -n1 || true)"
fi
[ -n "$IP" ] || IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -n "$IP" ] || IP="SERVER_IP"

echo
echo "username : gasslight"
if [ "$FIRST" -eq 1 ]; then echo "password : $PASSWORD"; else echo "password : (unchanged - the admin already existed)"; fi
echo "url      : http://$IP:$PORT"
