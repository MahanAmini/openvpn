#!/usr/bin/env bash
# =============================================================================
#  iran-tunnel-setup.sh  -  packet relay for the IRAN server
#
#  What it does: everything that reaches this server on the given ports (UDP + TCP) is passed on,
#  untouched, to your foreign OpenVPN/panel server. Nothing is decrypted or inspected here; this
#  server is only a carrier. Encryption stays between the user and the foreign server.
#
#      user  --->  IRAN server (this script)  --->  FOREIGN server (OpenVPN + panel)
#
#  Run on the IRAN server as root:
#      sudo bash iran-tunnel-setup.sh --to FOREIGN_SERVER_IP [--ports 1194:3000]
#
#  Options:
#    --to IP         IPv4 address of the foreign server (required)
#    --ports LIST    ports to carry, ranges allowed, comma separated, at most 15 entries
#                    (default 1194:3000 - the panel hands out user ports starting at 1195)
#    --status        show the current setup and how many packets were carried
#    --uninstall     remove the tunnel completely
#
#  Afterwards, in the panel (Advanced -> .ovpn address) put the IP of THIS server.
#  Also open the same ports (UDP and TCP) in the firewall / security group of this server.
# =============================================================================
set -eu

TARGET=""; PORTS="1194:3000"; ACTION="install"
die()  { echo "ERROR: $*" >&2; exit 1; }
info() { echo "[*] $*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --to)        TARGET="${2:-}"; shift 2 ;;
    --ports)     PORTS="${2:-}"; shift 2 ;;
    --status)    ACTION="status"; shift ;;
    --uninstall) ACTION="uninstall"; shift ;;
    -h|--help)   sed -n '2,24p' "$0"; exit 0 ;;
    *)           die "unknown option: $1 (use --help)" ;;
  esac
done
[ "$(id -u)" -eq 0 ] || die "run as root (sudo bash $0 ...)"

CONF=/etc/gl-tunnel.conf
BIN=/usr/local/sbin/gl-tunnel
UNIT=/etc/systemd/system/gl-tunnel.service
SYSCTL=/etc/sysctl.d/99-gl-tunnel.conf

# ------------------------------------------------------------------ status / uninstall
if [ "$ACTION" = "status" ]; then
  [ -x "$BIN" ] || die "the tunnel is not installed"
  exec "$BIN" status
fi
if [ "$ACTION" = "uninstall" ]; then
  [ -x "$BIN" ] && "$BIN" stop || true
  systemctl disable --now gl-tunnel 2>/dev/null || true
  rm -f "$UNIT" "$CONF" "$BIN" "$SYSCTL"
  systemctl daemon-reload 2>/dev/null || true
  info "tunnel removed. IP forwarding was left enabled (disable it with: sysctl -w net.ipv4.ip_forward=0)."
  exit 0
fi

# ------------------------------------------------------------------ checks
[ -n "$TARGET" ] || die "--to FOREIGN_SERVER_IP is required (see --help)"
echo "$TARGET" | grep -Eq '^([0-9]{1,3}\.){3}[0-9]{1,3}$' || die "--to must be an IPv4 address"
for o in $(echo "$TARGET" | tr '.' ' '); do [ "$o" -le 255 ] || die "--to is not a valid IPv4 address"; done

echo "$PORTS" | grep -Eq '^[0-9]+(:[0-9]+)?(,[0-9]+(:[0-9]+)?)*$' || die "--ports: use numbers and ranges like 1194:3000,8443"
[ "$(echo "$PORTS" | tr ',' '\n' | wc -l)" -le 15 ] || die "--ports: at most 15 entries"
for entry in $(echo "$PORTS" | tr ',' ' '); do
  lo="${entry%%:*}"; hi="${entry##*:}"
  [ "$lo" -ge 1 ] && [ "$hi" -le 65535 ] && [ "$lo" -le "$hi" ] || die "--ports: bad range '$entry'"
  if [ 22 -ge "$lo" ] && [ 22 -le "$hi" ]; then die "--ports includes port 22 (SSH) - you would lock yourself out"; fi
done

# the SSH session you are using right now must never be carried away
SSH_PORT="$(echo "${SSH_CONNECTION:-}" | awk '{print $4}')"
if [ -n "$SSH_PORT" ]; then
  for entry in $(echo "$PORTS" | tr ',' ' '); do
    lo="${entry%%:*}"; hi="${entry##*:}"
    if [ "$SSH_PORT" -ge "$lo" ] && [ "$SSH_PORT" -le "$hi" ]; then die "--ports includes your SSH port ($SSH_PORT)"; fi
  done
fi

if ! command -v iptables >/dev/null 2>&1; then
  command -v apt-get >/dev/null 2>&1 || die "iptables is not installed"
  info "installing iptables..."
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq iptables >/dev/null || die "could not install iptables"
fi

# warn about local services that would be hidden behind the tunnel
if command -v ss >/dev/null 2>&1; then
  for entry in $(echo "$PORTS" | tr ',' ' '); do
    lo="${entry%%:*}"; hi="${entry##*:}"
    ss -H -lntu 2>/dev/null | awk '{print $5}' | sed 's/.*://' | grep -E '^[0-9]+$' | sort -un | while read -r p; do
      if [ "$p" -ge "$lo" ] && [ "$p" -le "$hi" ]; then
        echo "WARNING: this server has a service listening on port $p; it will become unreachable (traffic goes to the foreign server)." >&2
      fi
    done
  done
fi

# ------------------------------------------------------------------ the worker (rules)
cat > "$BIN" <<'EOS'
#!/usr/bin/env bash
# gl-tunnel start|stop|restart|status   (installed by iran-tunnel-setup.sh)
set -u
CONF=/etc/gl-tunnel.conf
[ -f "$CONF" ] && . "$CONF"
: "${TARGET:=}" "${PORTS:=}"
ipt() { iptables "$@"; }

stop() {
  for spec in "nat PREROUTING GL_TUN_PRE" "nat POSTROUTING GL_TUN_POST" "filter FORWARD GL_TUN_FWD"; do
    set -- $spec; t=$1; parent=$2; chain=$3
    while ipt -t "$t" -D "$parent" -j "$chain" 2>/dev/null; do :; done
    ipt -t "$t" -F "$chain" 2>/dev/null || true
    ipt -t "$t" -X "$chain" 2>/dev/null || true
  done
}

start() {
  [ -n "$TARGET" ] && [ -n "$PORTS" ] || { echo "gl-tunnel: $CONF is incomplete" >&2; exit 1; }
  echo 1 > /proc/sys/net/ipv4/ip_forward
  stop
  ipt -t nat -N GL_TUN_PRE
  ipt -t nat -N GL_TUN_POST
  ipt -t filter -N GL_TUN_FWD
  for proto in udp tcp; do
    # same port on the other side (no port in --to-destination keeps it); packets are never opened
    ipt -t nat -A GL_TUN_PRE  -p "$proto" -m multiport --dports "$PORTS" -j DNAT --to-destination "$TARGET"
    ipt -t nat -A GL_TUN_POST -d "$TARGET" -p "$proto" -m multiport --dports "$PORTS" -j MASQUERADE
    ipt -t filter -A GL_TUN_FWD -d "$TARGET" -p "$proto" -m multiport --dports "$PORTS" -j ACCEPT
  done
  ipt -t filter -A GL_TUN_FWD -s "$TARGET" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
  ipt -t nat -I PREROUTING 1 -j GL_TUN_PRE
  ipt -t nat -I POSTROUTING 1 -j GL_TUN_POST
  ipt -t filter -I FORWARD 1 -j GL_TUN_FWD
}

status() {
  echo "target (foreign server): ${TARGET:-?}"
  echo "carried ports          : ${PORTS:-?}"
  echo "ip_forward             : $(cat /proc/sys/net/ipv4/ip_forward 2>/dev/null)"
  if ipt -t nat -S GL_TUN_PRE >/dev/null 2>&1; then
    echo "rules                  : active"
    echo; echo "packets carried towards the foreign server (all zeros = nobody connected yet):"
    ipt -t nat -L GL_TUN_PRE -n -v -x 2>/dev/null
  else
    echo "rules                  : NOT active (run: systemctl restart gl-tunnel)"
  fi
}

case "${1:-}" in
  start) start ;;
  stop) stop ;;
  restart) start ;;
  status) status ;;
  *) echo "usage: gl-tunnel start|stop|restart|status" >&2; exit 2 ;;
esac
EOS
chmod 755 "$BIN"

printf 'TARGET=%s\nPORTS=%s\n' "$TARGET" "$PORTS" > "$CONF"
chmod 644 "$CONF"

# ------------------------------------------------------------------ keep it after reboot
printf 'net.ipv4.ip_forward = 1\n' > "$SYSCTL"
cat > "$UNIT" <<EOF
[Unit]
Description=Packet relay to the foreign server (gl-tunnel)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=$BIN start
ExecStop=$BIN stop

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable gl-tunnel >/dev/null 2>&1 || true
systemctl restart gl-tunnel || die "the tunnel service failed to start (see: journalctl -u gl-tunnel -n 20)"

"$BIN" status | head -n 5
cat <<EOF

=====================================================================
  Tunnel is running (it starts again by itself after a reboot).

  Everything on UDP/TCP ports $PORTS of this server now goes to $TARGET.

  Next steps:
   1. Open those ports (UDP and TCP) in this server's firewall / provider panel.
   2. In the panel: Advanced -> .ovpn address -> enter the IP of THIS server.
   3. Check traffic any time with:  sudo bash $0 --status
  Remove it with:  sudo bash $0 --uninstall
=====================================================================
EOF
