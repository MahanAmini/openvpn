#!/usr/bin/env bash
# =============================================================================
#  ovpn-autopatch-setup.sh  -  let a reverse tunnel (Backhaul) reach the GassLight OpenVPN instances
#
#  The panel builds every user's OpenVPN config from /etc/openvpn/server/server.conf, which has a line
#  "local <public IP>": OpenVPN then listens ONLY on that address. A tunnel that connects to localhost:PORT
#  on this server is refused. This script comments that line out ("#local ...") so OpenVPN listens on all
#  addresses (the public IP keeps working, localhost starts working).
#
#  What it does:
#    1. patches every /etc/openvpn/server/*.conf that still has a "local" line, and restarts only the
#       instances it changed (and only if they are running)
#    2. installs a small service (ovpn-autopatch) that repeats this every 3 seconds, so configs created
#       later (or by re-running the Nyr script) are fixed too
#    3. makes sure IP forwarding is on
#
#  Usage:
#    sudo bash ovpn-autopatch-setup.sh [options]
#
#  Options:
#    --no-restart   patch the files but do not restart any OpenVPN instance. Connected users are not touched;
#                   a patched instance only listens on localhost after its next restart (e.g. a reboot).
#    --once         patch now and exit: do not install the background service
#    --uninstall    remove the service and the script (the "#local" lines stay commented)
#    -h, --help     this help
# =============================================================================
set -eu

NO_RESTART=0; ONCE=0; ACTION="install"
die()  { echo "ERROR: $*" >&2; exit 1; }
info() { echo "[*] $*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --no-restart) NO_RESTART=1; shift ;;
    --once)       ONCE=1; shift ;;
    --uninstall)  ACTION="uninstall"; shift ;;
    -h|--help)    sed -n '2,26p' "$0"; exit 0 ;;
    *)            die "unknown option: $1 (use --help)" ;;
  esac
done
[ "$(id -u)" -eq 0 ] || die "run as root (sudo bash $0 ...)"
command -v systemctl >/dev/null 2>&1 || die "systemd is required"

DIR=/etc/openvpn/server
WATCHER=/usr/local/bin/ovpn-autopatch.sh
UNIT=/etc/systemd/system/ovpn-autopatch.service

if [ "$ACTION" = "uninstall" ]; then
  systemctl disable --now ovpn-autopatch 2>/dev/null || true
  rm -f "$UNIT" "$WATCHER"
  systemctl daemon-reload 2>/dev/null || true
  info "removed. The '#local' lines in $DIR were left commented out."
  exit 0
fi

[ -d "${OVPN_DIR:-$DIR}" ] || die "${OVPN_DIR:-$DIR} not found: install OpenVPN with the Nyr script first"

# ------------------------------------------------------------------ the patcher
cat > "$WATCHER" <<'WATCHER_EOF'
#!/bin/bash
# GassLight auto patcher: comment out "local <ip>" in every OpenVPN server config.
# NO_RESTART=1 -> only edit files. ONCE=1 -> run a single pass and exit.
DIR="${OVPN_DIR:-/etc/openvpn/server}"

patch_all() {
  for conf in "$DIR"/*.conf; do
    [ -f "$conf" ] || continue
    grep -q '^local ' "$conf" || continue
    sed -i 's/^local /#local /' "$conf"
    echo "patched $(basename "$conf")"
    [ "${NO_RESTART:-0}" = 1 ] && continue
    unit="openvpn-server@$(basename "$conf" .conf)"
    if systemctl is-active --quiet "$unit"; then
      systemctl restart "$unit" && echo "restarted $unit"
    fi
  done
}

if [ "${ONCE:-0}" = 1 ]; then patch_all; exit 0; fi
while true; do
  patch_all
  sleep 3
done
WATCHER_EOF
chmod 755 "$WATCHER"
bash -n "$WATCHER" || die "internal error: the generated patcher has a syntax error"

# ------------------------------------------------------------------ IP forwarding (the panel adds its own NAT rules per user)
if [ "$(sysctl -n net.ipv4.ip_forward 2>/dev/null || echo 0)" != "1" ]; then
  sysctl -w net.ipv4.ip_forward=1 >/dev/null
  echo "net.ipv4.ip_forward=1" > /etc/sysctl.d/99-gl-forward.conf
  info "IP forwarding enabled"
fi

# ------------------------------------------------------------------ patch now
if [ "$ONCE" -eq 1 ]; then
  info "patching once..."
  NO_RESTART="$NO_RESTART" ONCE=1 "$WATCHER"
  info "done"
  exit 0
fi

cat > "$UNIT" <<EOF
[Unit]
Description=GassLight OpenVPN auto patcher
After=network.target

[Service]
Environment=NO_RESTART=$NO_RESTART
ExecStart=$WATCHER
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
chmod 644 "$UNIT"
systemctl daemon-reload
systemctl enable --now ovpn-autopatch >/dev/null 2>&1 || true
systemctl restart ovpn-autopatch
sleep 4
systemctl is-active --quiet ovpn-autopatch || { journalctl -u ovpn-autopatch -n 15 --no-pager 2>/dev/null || true; die "the patcher service did not start (see log above)"; }

left="$(grep -l '^local ' "$DIR"/*.conf 2>/dev/null | wc -l | tr -d ' ')"
cat <<EOF

=====================================================================
  Auto patcher is running (starts on boot).

  Configs still bound to a single IP : $left   (should be 0)
  Restarting patched instances       : $([ "$NO_RESTART" -eq 1 ] && echo "no (--no-restart)" || echo "yes, only the ones that were changed")

  Check a user's port  : ss -lnup | grep 1195     (UDP)   /   ss -lntp | grep 1195   (TCP)
                         it should show 0.0.0.0:1195, not your public IP
  Log                  : journalctl -u ovpn-autopatch -n 20 --no-pager
  Remove               : sudo bash $0 --uninstall
=====================================================================
EOF
