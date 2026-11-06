#!/usr/bin/env bash
#
# GassLight installer: OpenVPN core + user-management panel + user portal.
#
#   wget https://raw.githubusercontent.com/USERNAME/openvpn/main/install.sh -O install.sh
#   sudo bash install.sh
#
# Run it again whenever you want to update; your users and settings are kept.
# Options:  --panel-port N   --portal-port N   --local-only   --repo user/repo   --ref branch-or-tag
#

# --- must be bash (checked before any bash-only syntax is read) ---------------
if readlink /proc/$$/exe 2>/dev/null | grep -q "dash"; then
    echo 'This installer needs "bash", not "sh". Run:  sudo bash install.sh'
    exit 1
fi

set -Eeuo pipefail

# =============================================================== settings ====
# >>> Before publishing: put your own GitHub "user/repo" here <<<
DEFAULT_REPO="mahanamini/openvpn"

PANEL_DIR="${PANEL_DIR:-/opt/ovpn-panel}"            # panel code + all its data (users, usage, .ovpn files)
PORTAL_DIR="${PORTAL_DIR:-/opt/gasslight-portal}"    # portal code
ETC_DIR="${ETC_DIR:-/etc/gasslight}"                 # settings, shared secret
BIN_DIR="${BIN_DIR:-/usr/local/bin}"                 # the "gasslight" command
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
OVPN_SERVER_DIR="${OVPN_SERVER_DIR:-/etc/openvpn/server}"

# Precedence: built-in defaults < previous install < environment < command-line flags
env_panel_port="${PANEL_PORT:-}"
env_portal_port="${PORTAL_PORT:-}"
env_panel_host="${PANEL_HOST:-}"
env_repo="${GASSLIGHT_REPO:-}"
env_ref="${GASSLIGHT_REF:-}"

REPO="$DEFAULT_REPO"
REF="main"
PANEL_PORT=8088
PORTAL_PORT=8089
PANEL_HOST="0.0.0.0"

if [[ -f "$ETC_DIR/install.conf" ]]; then
    # shellcheck disable=SC1091
    . "$ETC_DIR/install.conf"
fi
if [[ -n "$env_panel_port" ]];  then PANEL_PORT="$env_panel_port"; fi
if [[ -n "$env_portal_port" ]]; then PORTAL_PORT="$env_portal_port"; fi
if [[ -n "$env_panel_host" ]];  then PANEL_HOST="$env_panel_host"; fi
if [[ -n "$env_repo" ]];        then REPO="$env_repo"; fi
if [[ -n "$env_ref" ]];         then REF="$env_ref"; fi

# ================================================================= helpers ===
if [[ -t 1 ]]; then
    C_B=$'\e[1m'; C_G=$'\e[32m'; C_Y=$'\e[33m'; C_R=$'\e[31m'; C_N=$'\e[0m'
else
    C_B=""; C_G=""; C_Y=""; C_R=""; C_N=""
fi
say()  { printf '%s==>%s %s\n' "$C_B" "$C_N" "$*"; }
ok()   { printf '%s ok%s  %s\n' "$C_G" "$C_N" "$*"; }
warn() { printf '%s !!%s  %s\n' "$C_Y" "$C_N" "$*" >&2; }
die()  { printf '%s error:%s %s\n' "$C_R" "$C_N" "$*" >&2; exit 1; }

TMP=""
cleanup() { if [[ -n "$TMP" ]]; then rm -rf "$TMP"; fi; }
trap cleanup EXIT
trap 'printf "\nInstallation stopped at line %s (exit code %s). Fix the problem and run it again.\n" "$LINENO" "$?" >&2' ERR

usage() {
    cat <<'EOF'
GassLight installer

  sudo bash install.sh [options]

  --panel-port N    port of the admin panel          (default 8088)
  --portal-port N   port of the user portal          (default 8089)
  --local-only      panel listens on 127.0.0.1 only (open it through an SSH tunnel)
  --repo user/repo  GitHub repository to download from when run on its own
  --ref NAME        branch or tag to download        (default main)
  -h, --help        show this help

Environment variables with the same meaning also work:
  PANEL_PORT, PORTAL_PORT, PANEL_HOST, GASSLIGHT_REPO, GASSLIGHT_REF
EOF
}

valid_port() {
    if [[ "$1" =~ ^[0-9]+$ ]] && (( $1 >= 1 && $1 <= 65535 )); then return 0; fi
    return 1
}

random_password() {
    python3 -c 'import secrets,string; a=string.ascii_letters+string.digits; print("".join(secrets.choice(a) for _ in range(16)))'
}

wait_http() {   # url, seconds
    local i
    for ((i = 0; i < $2; i++)); do
        if curl -fsS -o /dev/null --max-time 2 "$1" 2>/dev/null; then return 0; fi
        sleep 1
    done
    return 1
}

wait_file() {   # path, seconds
    local i
    for ((i = 0; i < $2; i++)); do
        if [[ -s "$1" ]]; then return 0; fi
        sleep 1
    done
    return 1
}

server_addr() {
    local a=""
    if [[ -r "$OVPN_SERVER_DIR/client-common.txt" ]]; then
        a="$(awk '/^remote /{print $2; exit}' "$OVPN_SERVER_DIR/client-common.txt")"
    fi
    if [[ -z "$a" ]]; then
        a="$(hostname -I 2>/dev/null | awk '{print $1}')"
    fi
    printf '%s' "${a:-SERVER_IP}"
}

# ================================================================== steps ====
parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --panel-port)  PANEL_PORT="${2:-}";  shift 2 || die "--panel-port needs a value" ;;
            --portal-port) PORTAL_PORT="${2:-}"; shift 2 || die "--portal-port needs a value" ;;
            --local-only)  PANEL_HOST="127.0.0.1"; shift ;;
            --repo)        REPO="${2:-}";        shift 2 || die "--repo needs a value" ;;
            --ref)         REF="${2:-}";         shift 2 || die "--ref needs a value" ;;
            -h|--help)     usage; exit 0 ;;
            *)             die "Unknown option: $1 (try --help)" ;;
        esac
    done
    if ! valid_port "$PANEL_PORT";  then die "Invalid panel port: $PANEL_PORT"; fi
    if ! valid_port "$PORTAL_PORT"; then die "Invalid portal port: $PORTAL_PORT"; fi
    if [[ "$PANEL_PORT" == "$PORTAL_PORT" ]]; then die "The panel and the portal need different ports."; fi
    local p
    for p in "$PANEL_PORT" "$PORTAL_PORT"; do
        if [[ "$p" == "22" || "$p" == "1194" ]]; then die "Port $p is reserved (SSH / OpenVPN). Choose another."; fi
    done
}

check_system() {
    if (( EUID != 0 )); then die "Please run as root:  sudo bash install.sh"; fi
    [[ -r /etc/os-release ]] || die "Cannot detect the operating system."
    local id ver
    # shellcheck disable=SC1091
    id="$(. /etc/os-release && printf '%s' "${ID:-}")"
    # shellcheck disable=SC1091
    ver="$(. /etc/os-release && printf '%s' "${VERSION_ID:-0}")"
    case "$id" in
        ubuntu) if (( ${ver//./} < 2204 )); then die "Ubuntu 22.04 or newer is required (found $ver)."; fi ;;
        debian) if (( ${ver%%.*} < 11 ));   then die "Debian 11 or newer is required (found $ver)."; fi ;;
        *)      die "Supported systems: Ubuntu 22.04+ and Debian 11+ (found: ${id:-unknown})." ;;
    esac
    if ! command -v systemctl >/dev/null 2>&1; then die "systemd is required."; fi
    ok "System check passed ($id $ver)"
}

ensure_packages() {
    local need=()
    command -v curl     >/dev/null 2>&1 || need+=(curl)
    command -v tar      >/dev/null 2>&1 || need+=(tar)
    command -v ip       >/dev/null 2>&1 || need+=(iproute2)   # also provides "tc" (speed limits)
    command -v iptables >/dev/null 2>&1 || need+=(iptables)
    command -v openssl  >/dev/null 2>&1 || need+=(openssl)
    command -v python3  >/dev/null 2>&1 || need+=(python3)
    python3 -c 'import flask' >/dev/null 2>&1 || need+=(python3-flask)
    [[ -d /etc/ssl/certs ]] || need+=(ca-certificates)
    if (( ${#need[@]} > 0 )); then
        say "Installing packages: ${need[*]}"
        apt-get update -qq
        DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${need[@]}"
    fi
    ok "Required packages are present"
}

fetch_sources() {
    local here=""
    here="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
    if [[ -n "$here" && -f "$here/panel/panel.py" && -f "$here/portal/portal.py" && -f "$here/core/openvpn-install.sh" ]]; then
        SRC="$here"
        ok "Using the files next to this script ($SRC)"
        return
    fi
    if [[ "$REPO" == "USERNAME/openvpn" ]]; then
        die "No GitHub repository is configured. Edit DEFAULT_REPO at the top of install.sh, or run:  sudo bash install.sh --repo YOURNAME/openvpn"
    fi
    say "Downloading $REPO ($REF)"
    TMP="$(mktemp -d)"
    if ! curl -fsSL --retry 3 "https://github.com/${REPO}/archive/${REF}.tar.gz" | tar -xz -C "$TMP" --strip-components=1; then
        die "Could not download https://github.com/${REPO}/archive/${REF}.tar.gz (check the repository name, branch/tag and that the repo is public)."
    fi
    [[ -f "$TMP/panel/panel.py" && -f "$TMP/portal/portal.py" && -f "$TMP/core/openvpn-install.sh" ]] \
        || die "The downloaded repository does not contain panel/, portal/ and core/."
    SRC="$TMP"
    ok "Downloaded"
}

install_core() {
    if [[ -e "$OVPN_SERVER_DIR/server.conf" ]]; then
        ok "OpenVPN core is already installed - skipping"
        return
    fi
    [[ -e /dev/net/tun ]] || die "The TUN device is not available on this server. Enable TUN/TAP in your VPS control panel first."
    [[ -t 0 ]] || die "The OpenVPN setup asks a few questions, so it must run in a terminal. Download the script first, then run it:  sudo bash install.sh"
    say "Installing the OpenVPN core. Answer the questions (Enter = default)."
    echo
    install -m 755 "$SRC/core/openvpn-install.sh" /root/openvpn-install.sh
    ( cd /root && bash /root/openvpn-install.sh )
    [[ -e "$OVPN_SERVER_DIR/server.conf" ]] || die "The OpenVPN core installation did not finish."
    ok "OpenVPN core installed"
}

write_env() {   # $1 = admin password for the very first start (optional)
    local f="$ETC_DIR/panel.env"
    install -m 600 /dev/null "$f"
    {
        printf 'PANEL_PORT=%s\n'  "$PANEL_PORT"
        printf 'PANEL_HOST=%s\n'  "$PANEL_HOST"
        printf 'PORTAL_PORT=%s\n' "$PORTAL_PORT"
        if [[ -n "${1:-}" ]]; then printf 'PANEL_PASSWORD=%s\n' "$1"; fi
    } > "$f"
}

write_conf() {
    install -m 644 /dev/null "$ETC_DIR/install.conf"
    cat > "$ETC_DIR/install.conf" <<EOF
REPO="$REPO"
REF="$REF"
PANEL_PORT="$PANEL_PORT"
PORTAL_PORT="$PORTAL_PORT"
PANEL_HOST="$PANEL_HOST"
PANEL_DIR="$PANEL_DIR"
PORTAL_DIR="$PORTAL_DIR"
ETC_DIR="$ETC_DIR"
BIN_DIR="$BIN_DIR"
SYSTEMD_DIR="$SYSTEMD_DIR"
OVPN_SERVER_DIR="$OVPN_SERVER_DIR"
EOF
}

write_units() {
    cat > "$SYSTEMD_DIR/ovpn-panel.service" <<EOF
[Unit]
Description=GassLight admin panel
After=network-online.target openvpn-server@server.service
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=$ETC_DIR/panel.env
Environment=PANEL_DATA=$PANEL_DIR
Environment=OVPN_SERVER_DIR=$OVPN_SERVER_DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 $PANEL_DIR/panel.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
    cat > "$SYSTEMD_DIR/gasslight-portal.service" <<EOF
[Unit]
Description=GassLight user portal
After=network-online.target ovpn-panel.service
Wants=network-online.target

[Service]
Type=simple
User=nobody
Group=$PORTAL_GROUP
Environment=PORTAL_PORT=$PORTAL_PORT
Environment=PANEL_URL=http://127.0.0.1:$PANEL_PORT
Environment=PORTAL_SECRET_FILE=$ETC_DIR/portal.secret
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/usr/bin/python3 $PORTAL_DIR/portal.py
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
}

install_cli() {
    cat > "$BIN_DIR/gasslight" <<'CLI_EOF'
#!/usr/bin/env bash
# gasslight - small management command for the GassLight panel
set -Eeuo pipefail
CONF="__ETC_DIR__/install.conf"
[[ -r "$CONF" ]] || { echo "GassLight is not installed (missing $CONF)"; exit 1; }
# shellcheck disable=SC1090
. "$CONF"

need_root() { if (( EUID != 0 )); then echo "Run as root:  sudo gasslight $*"; exit 1; fi; }

server_addr() {
    local a=""
    if [[ -r "$OVPN_SERVER_DIR/client-common.txt" ]]; then
        a="$(awk '/^remote /{print $2; exit}' "$OVPN_SERVER_DIR/client-common.txt")"
    fi
    if [[ -z "$a" ]]; then a="$(hostname -I 2>/dev/null | awk '{print $1}')"; fi
    printf '%s' "${a:-SERVER_IP}"
}

rand_pw() {
    python3 -c 'import secrets,string; a=string.ascii_letters+string.digits; print("".join(secrets.choice(a) for _ in range(16)))'
}

usage() {
    cat <<'EOF'
Usage: gasslight <command>

  info                    show the panel / portal addresses
  status                  service status
  restart                 restart the panel and the portal
  logs [N]                last N log lines (default 100)
  reset-password [user]   set a new random password (default user: admin)
  update                  update to the latest version from GitHub
  uninstall [--purge]     remove the panel and portal (VPN users keep working)
EOF
}

case "${1:-help}" in
    info)
        a="$(server_addr)"
        echo "Panel:  http://$a:$PANEL_PORT"
        echo "Portal: http://$a:$PORTAL_PORT   (links are copied from the panel)"
        if [[ "$PANEL_HOST" == "127.0.0.1" ]]; then
            echo "The panel listens locally only. From your computer: ssh -L $PANEL_PORT:127.0.0.1:$PANEL_PORT user@$a"
        fi
        ;;
    status)
        systemctl --no-pager -l status ovpn-panel gasslight-portal 2>&1 | sed -n '1,30p' || true
        ;;
    restart)
        need_root restart
        systemctl restart ovpn-panel gasslight-portal
        echo "restarted"
        ;;
    logs)
        journalctl -u ovpn-panel -u gasslight-portal -n "${2:-100}" --no-pager
        ;;
    reset-password)
        need_root reset-password
        name="${2:-gasslight}"
        if [[ ! "$name" =~ ^[A-Za-z0-9_.-]{3,32}$ ]]; then echo "Invalid username."; exit 1; fi
        pw="$(rand_pw)"
        PANEL_DIR="$PANEL_DIR" NAME="$name" NEWPW="$pw" python3 - <<'PY'
import json, os
from werkzeug.security import generate_password_hash
path = os.path.join(os.environ["PANEL_DIR"], "auth.json")
data = json.load(open(path)) if os.path.exists(path) else {"users": []}
h = generate_password_hash(os.environ["NEWPW"])
for u in data["users"]:
    if u["username"] == os.environ["NAME"]:
        u["hash"] = h
        break
else:
    data["users"].append({"username": os.environ["NAME"], "hash": h})
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(data, f, indent=2)
os.chmod(tmp, 0o600)
os.replace(tmp, path)
PY
        echo "Username: $name"
        echo "Password: $pw"
        ;;
    update)
        need_root update
        tmp="$(mktemp)"
        curl -fsSL "https://raw.githubusercontent.com/$REPO/$REF/install.sh" -o "$tmp"
        bash "$tmp"
        rm -f "$tmp"
        ;;
    uninstall)
        need_root uninstall
        read -r -p "Remove the GassLight panel and portal? Your VPN users keep working. [y/N]: " ans
        if [[ ! "$ans" =~ ^[yY]$ ]]; then echo "Aborted."; exit 0; fi
        systemctl disable --now ovpn-panel gasslight-portal >/dev/null 2>&1 || true
        rm -f "$SYSTEMD_DIR/ovpn-panel.service" "$SYSTEMD_DIR/gasslight-portal.service"
        systemctl daemon-reload || true
        rm -rf "$PORTAL_DIR"
        rm -f "$PANEL_DIR/panel.py"
        if [[ "${2:-}" == "--purge" ]]; then
            rm -rf "$PANEL_DIR" "$ETC_DIR"
            rm -f "$BIN_DIR/gasslight"
            echo "Removed, including all panel data."
        else
            echo "Removed. Panel data was kept in $PANEL_DIR (use 'gasslight uninstall --purge' to delete it too)."
        fi
        echo "The OpenVPN core and your users' VPN services were not touched."
        ;;
    *)
        usage
        ;;
esac
CLI_EOF
    sed -i "s|__ETC_DIR__|$ETC_DIR|" "$BIN_DIR/gasslight"
    chmod 755 "$BIN_DIR/gasslight"
}

deploy() {
    install -d -m 700 "$PANEL_DIR"
    install -m 600 "$SRC/panel/panel.py" "$PANEL_DIR/panel.py"
    install -d -m 755 "$PORTAL_DIR"
    install -m 644 "$SRC/portal/portal.py" "$PORTAL_DIR/portal.py"
    install -d -m 755 "$ETC_DIR"
    install -d -m 755 "$BIN_DIR" "$SYSTEMD_DIR"
    ok "Files installed"
}

start_all() {
    PORTAL_GROUP="nogroup"
    if ! getent group nogroup >/dev/null 2>&1; then PORTAL_GROUP="nobody"; fi

    FIRST_RUN=0
    ADMIN_PW=""
    if [[ ! -e "$PANEL_DIR/auth.json" ]]; then
        FIRST_RUN=1
        ADMIN_PW="$(random_password)"
    fi

    write_env "$ADMIN_PW"
    write_conf
    write_units

    systemctl daemon-reload
    systemctl enable ovpn-panel.service >/dev/null 2>&1
    systemctl restart ovpn-panel.service
    say "Starting the panel"
    if ! wait_http "http://127.0.0.1:$PANEL_PORT/login" 40; then
        journalctl -u ovpn-panel -n 25 --no-pager >&2 || true
        die "The panel did not start (see the log above)."
    fi
    # The first request makes the panel create its admin account and the portal secret.
    curl -fsS -o /dev/null --max-time 5 "http://127.0.0.1:$PANEL_PORT/" 2>/dev/null || true
    wait_file "$PANEL_DIR/auth.json" 15       || die "The panel did not create its admin account."
    wait_file "$PANEL_DIR/portal.secret" 15   || die "The panel did not create the portal secret."
    if (( FIRST_RUN == 1 )); then
        write_env ""        # the password is no longer needed on disk
    fi
    ok "Panel is running"

    install -m 640 -o root -g "$PORTAL_GROUP" "$PANEL_DIR/portal.secret" "$ETC_DIR/portal.secret"
    systemctl enable gasslight-portal.service >/dev/null 2>&1
    systemctl restart gasslight-portal.service
    sleep 1
    if systemctl is-active --quiet gasslight-portal.service; then
        ok "User portal is running"
    else
        warn "The user portal did not start. Check:  journalctl -u gasslight-portal -n 30"
    fi
}

open_firewall() {
    if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
        if [[ "$PANEL_HOST" != "127.0.0.1" ]]; then ufw allow "$PANEL_PORT/tcp" >/dev/null; fi
        ufw allow "$PORTAL_PORT/tcp" >/dev/null
        ok "ufw: opened TCP $PANEL_PORT and $PORTAL_PORT"
    fi
}

summary() {
    local addr vpn_port vpn_proto first
    addr="$(server_addr)"
    vpn_port="$(awk '/^port /{print $2; exit}' "$OVPN_SERVER_DIR/server.conf" 2>/dev/null || true)"
    vpn_proto="$(awk '/^proto /{print $2; exit}' "$OVPN_SERVER_DIR/server.conf" 2>/dev/null || true)"
    shopt -s nullglob
    local files=(/root/*.ovpn)
    shopt -u nullglob
    first="${files[0]:-}"

    echo
    echo "${C_B}============================================================${C_N}"
    if (( FIRST_RUN == 1 )); then
        echo "${C_B} GassLight is ready${C_N}"
    else
        echo "${C_B} GassLight updated${C_N}"
    fi
    echo "------------------------------------------------------------"
    echo " Panel     :  http://$addr:$PANEL_PORT"
    if (( FIRST_RUN == 1 )); then
        echo " Username  :  gasslight"
        echo " Password  :  $ADMIN_PW"
        echo "              (shown only now - save it; you can change it in the panel under"
        echo "               Advanced, or run:  gasslight reset-password)"
    else
        echo " Login     :  your existing admin account (forgot it? run: gasslight reset-password)"
    fi
    echo " Portal    :  http://$addr:$PORTAL_PORT   (user links are copied from the panel)"
    if [[ -n "$first" ]]; then
        echo " First VPN client file:  $first"
    fi
    if [[ "$PANEL_HOST" == "127.0.0.1" ]]; then
        echo
        echo " The panel listens locally only. From your computer run:"
        echo "   ssh -L $PANEL_PORT:127.0.0.1:$PANEL_PORT USER@$addr"
        echo " then open http://127.0.0.1:$PANEL_PORT"
    fi
    echo "------------------------------------------------------------"
    echo " Open these ports in your cloud firewall / security group:"
    echo "   TCP $PANEL_PORT (panel), TCP $PORTAL_PORT (portal),"
    echo "   ${vpn_proto:-udp} ${vpn_port:-1194} (OpenVPN) and every port you give to users"
    echo "------------------------------------------------------------"
    echo " Manage:  gasslight info | status | logs | restart | update | reset-password | uninstall"
    echo "${C_B}============================================================${C_N}"
}

# =================================================================== main ====
main() {
    parse_args "$@"
    check_system
    say "GassLight installer"
    ensure_packages
    fetch_sources
    install_core
    deploy
    start_all
    install_cli
    open_firewall
    summary
}

SRC=""
FIRST_RUN=0
ADMIN_PW=""
PORTAL_GROUP="nogroup"
main "$@"
