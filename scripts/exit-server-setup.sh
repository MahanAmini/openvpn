#!/usr/bin/env bash
# =============================================================================
#  exit-server-setup.sh  -  SOCKS5 proxy (Dante) with username/password
#  Run this on every EXIT server (Germany, Netherlands, France, ...), NOT on the panel server.
#  Supports Debian / Ubuntu.  Safe to run again: it only rewrites its own config.
#
#  Usage:
#    sudo bash exit-server-setup.sh --allow-ip MAIN_SERVER_IP [--port 1080] [--user NAME] [--pass PASSWORD]
#
#  Options:
#    --allow-ip IP[,IP2]  only these addresses may connect (the panel server). STRONGLY recommended.
#    --port N             proxy port (default 1080)
#    --user NAME          proxy username (default: random)
#    --pass PASSWORD      proxy password (default: random, 20 chars)
#    --iface NAME         network interface used for outgoing traffic (default: auto-detected)
#    --allow-private      let clients reach private/local networks of this server (blocked by default)
#    --uninstall          remove the proxy, its user and its config
# =============================================================================
set -eu

PORT=1080; PUSER=""; PPASS=""; ALLOW=""; IFACE=""; BLOCK_PRIVATE=1; UNINSTALL=0
USER_PREFIX="gl"

die()  { echo "ERROR: $*" >&2; exit 1; }
info() { echo "[*] $*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --port)          PORT="${2:-}"; shift 2 ;;
    --user)          PUSER="${2:-}"; shift 2 ;;
    --pass)          PPASS="${2:-}"; shift 2 ;;
    --allow-ip)      ALLOW="${2:-}"; shift 2 ;;
    --iface)         IFACE="${2:-}"; shift 2 ;;
    --allow-private) BLOCK_PRIVATE=0; shift ;;
    --uninstall)     UNINSTALL=1; shift ;;
    -h|--help)       sed -n '2,20p' "$0"; exit 0 ;;
    *)               die "unknown option: $1 (use --help)" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "run as root (sudo bash $0 ...)"
CONF=/etc/danted.conf
STATE=/etc/danted.gl-user      # remembers which system user this script created

# ---------------------------------------------------------------- uninstall
if [ "$UNINSTALL" -eq 1 ]; then
  systemctl disable --now danted 2>/dev/null || true
  rm -f /etc/systemd/system/danted.service.d/gl-restart.conf "$CONF"
  if [ -f "$STATE" ]; then
    u="$(cat "$STATE")"; userdel "$u" 2>/dev/null || true; rm -f "$STATE"
  fi
  systemctl daemon-reload 2>/dev/null || true
  DEBIAN_FRONTEND=noninteractive apt-get purge -y dante-server >/dev/null 2>&1 || true
  info "removed. (Remember to close the port in your cloud firewall / ufw if you opened it.)"
  exit 0
fi

# ---------------------------------------------------------------- checks
command -v apt-get >/dev/null 2>&1 || die "this script supports Debian/Ubuntu (apt) only"
case "$PORT" in ''|*[!0-9]*) die "--port must be a number" ;; esac
[ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || die "--port must be 1-65535"

rand() { head -c 256 /dev/urandom | tr -dc "$1" | head -c "$2"; }
[ -n "$PUSER" ] || PUSER="${USER_PREFIX}$(rand 'a-z0-9' 6)"
[ -n "$PPASS" ] || PPASS="$(rand 'A-Za-z0-9' 20)"
echo "$PUSER" | grep -Eq '^[a-z_][a-z0-9_-]{2,30}$' || die "--user: 3-31 chars, lowercase letters / digits / _ / -, must start with a letter"
case "$PPASS" in *[[:space:]]*|'') die "--pass must not contain spaces" ;; esac
[ "${#PPASS}" -ge 8 ] || die "--pass must be at least 8 characters"
id "$PUSER" >/dev/null 2>&1 && [ ! -f "$STATE" -o "$(cat "$STATE" 2>/dev/null)" != "$PUSER" ] \
  && die "system user '$PUSER' already exists and was not created by this script; pick another --user"

if [ -n "$ALLOW" ]; then
  echo "$ALLOW" | grep -Eq '^[0-9]{1,3}(\.[0-9]{1,3}){3}(,[0-9]{1,3}(\.[0-9]{1,3}){3})*$' \
    || die "--allow-ip must be IPv4 address(es), comma separated"
else
  echo "WARNING: no --allow-ip given. Anyone on the internet can try to log in to this proxy." >&2
  echo "         Re-run with --allow-ip YOUR_PANEL_SERVER_IP to lock it down." >&2
fi

if [ -z "$IFACE" ]; then
  IFACE="$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* dev \([^ ]*\).*/\1/p' | head -n1)"
  [ -n "$IFACE" ] || die "could not detect the network interface; pass --iface NAME (see: ip -br a)"
fi

# ---------------------------------------------------------------- install
info "installing dante-server..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null
apt-get install -y -qq dante-server curl >/dev/null

# ---------------------------------------------------------------- user
if [ -f "$STATE" ] && [ "$(cat "$STATE")" != "$PUSER" ]; then
  userdel "$(cat "$STATE")" 2>/dev/null || true       # replace the previous proxy user
fi
id "$PUSER" >/dev/null 2>&1 || useradd -M -N -s /usr/sbin/nologin "$PUSER"
printf '%s:%s\n' "$PUSER" "$PPASS" | chpasswd
echo "$PUSER" > "$STATE"; chmod 600 "$STATE"

# ---------------------------------------------------------------- config
rule() {   # rule <client|socks> <pass|block> <from> <to> [commands]
  echo "$1 $2 {"
  echo "    from: $3 to: $4"
  if [ -n "${5:-}" ]; then echo "    command: $5"; fi
  echo "    log: error"
  echo "}"
}
[ -f "$CONF" ] && [ ! -f "$CONF.orig" ] && cp "$CONF" "$CONF.orig"
{
  echo "# generated by exit-server-setup.sh - changes will be overwritten when it runs again"
  echo "logoutput: syslog"
  echo "internal: 0.0.0.0 port = $PORT"
  echo "external: $IFACE"
  echo "socksmethod: username"
  echo "clientmethod: none"
  echo "user.privileged: root"
  echo "user.unprivileged: nobody"
  echo
  rule client pass 127.0.0.1/32 0.0.0.0/0
  if [ -n "$ALLOW" ]; then
    for ip in $(echo "$ALLOW" | tr ',' ' '); do rule client pass "$ip/32" 0.0.0.0/0; done
  else
    rule client pass 0.0.0.0/0 0.0.0.0/0
  fi
  rule client block 0.0.0.0/0 0.0.0.0/0
  echo
  if [ "$BLOCK_PRIVATE" -eq 1 ]; then
    for net in 127.0.0.0/8 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 169.254.0.0/16; do
      rule socks block 0.0.0.0/0 "$net"
    done
  fi
  rule socks pass 0.0.0.0/0 0.0.0.0/0 "connect udpassociate"
  rule socks block 0.0.0.0/0 0.0.0.0/0
} > "$CONF"
chmod 644 "$CONF"
# validate before touching the running service
sed 's/^logoutput: syslog/logoutput: stderr/' "$CONF" > /tmp/.gl-danted-check.conf
if ! danted -V -f /tmp/.gl-danted-check.conf >/tmp/.gl-danted-check.log 2>&1; then
  cat /tmp/.gl-danted-check.log >&2; rm -f /tmp/.gl-danted-check.*; die "generated danted config is invalid (see above)"
fi
rm -f /tmp/.gl-danted-check.*

# ---------------------------------------------------------------- systemd (always on, restarts by itself)
mkdir -p /etc/systemd/system/danted.service.d
cat > /etc/systemd/system/danted.service.d/gl-restart.conf <<'EOF'
[Service]
Restart=always
RestartSec=3
EOF
systemctl daemon-reload
systemctl enable danted >/dev/null 2>&1 || true
systemctl restart danted
sleep 1
systemctl is-active --quiet danted || { journalctl -u danted -n 20 --no-pager 2>/dev/null || true; die "danted did not start (see log above)"; }

# ---------------------------------------------------------------- firewall (ufw only; cloud firewalls are up to you)
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  if [ -n "$ALLOW" ]; then
    echo "$ALLOW" | tr ',' '\n' | while read -r ip; do ufw allow from "$ip" to any port "$PORT" proto tcp >/dev/null; done
  else
    ufw allow "$PORT"/tcp >/dev/null
  fi
  info "ufw: port $PORT opened"
fi

# ---------------------------------------------------------------- self-test (informational only)
EXIT_IP="$(curl -s -m 8 https://api.ipify.org 2>/dev/null || true)"
TEST="$(curl -s -m 10 --proxy "socks5://$PUSER:$PPASS@127.0.0.1:$PORT" https://api.ipify.org 2>/dev/null || true)"
if [ -n "$TEST" ]; then info "self-test OK (traffic leaves from $TEST)"; else
  echo "[!] self-test could not reach the internet through the proxy (no internet on this server, or ipify blocked). Check: journalctl -u danted -n 30" >&2
fi

cat <<EOF

=====================================================================
  SOCKS5 exit server is running (auto-starts on boot, restarts on failure)

  Add this in the panel  ->  Advanced  ->  Exit servers:

     IP        : ${EXIT_IP:-<public IP of this server>}
     Port      : $PORT
     Username  : $PUSER
     Password  : $PPASS

  Allowed clients : ${ALLOW:-ANYONE (not recommended)}
  Also open TCP $PORT in your provider's cloud firewall / security group if it has one.
  Remove everything later with:  sudo bash $0 --uninstall
=====================================================================
EOF
