#!/usr/bin/env python3
"""GassLight - OpenVPN multi-instance panel (Flask, single file).

Requires: python3-flask, root, and the Nyr openvpn-install script already
installed on the host (/etc/openvpn/server/server.conf must exist).
Env: PANEL_PASSWORD (required), PANEL_PORT (default 8088), PANEL_HOST (default 0.0.0.0)
"""
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import zipfile
from collections import deque
from functools import wraps
from urllib.parse import urlparse

from markupsafe import Markup
from werkzeug.security import check_password_hash, generate_password_hash

from flask import (Flask, abort, flash, jsonify, redirect, render_template_string,
                   request, send_file, session, url_for)

APP_NAME = "GassLight"
BASE = os.environ.get("PANEL_DATA", "/opt/ovpn-panel")
DB = f"{BASE}/users.json"
USAGE_FILE = f"{BASE}/usage.json"
OVPN_DIR = f"{BASE}/ovpn"
BACKUP_DIR = f"{BASE}/backups"
AUTH_FILE = f"{BASE}/auth.json"
SETTINGS_FILE = f"{BASE}/settings.json"
STATUS_DIR = os.environ.get("OVPN_STATUS_DIR", "/run/openvpn-server")
ACTIVE_WINDOW = 45   # seconds without incoming bytes from the client => shown as offline
SECRET_FILE = f"{BASE}/secret.key"
SERVER_DIR = os.environ.get("OVPN_SERVER_DIR", "/etc/openvpn/server")
ED = f"{SERVER_DIR}/easy-rsa"
PASSWORD = os.environ.get("PANEL_PASSWORD", "")
HOST = os.environ.get("PANEL_HOST", "0.0.0.0")
PORT = int(os.environ.get("PANEL_PORT", "8088"))
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
RESERVED_PORTS = {1194, 22}  # main OpenVPN panel + SSH
GB = 1024 ** 3
PORTAL_PORT = int(os.environ.get("PORTAL_PORT", "8089"))          # where portal.py listens
PORTAL_BASE_URL = os.environ.get("PORTAL_BASE_URL", "").rstrip("/")  # optional, e.g. https://vpn.example.com
PORTAL_SECRET_FILE = f"{BASE}/portal.secret"
USER_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
LOCK = threading.RLock()     # users.json read-modify-write
SLOCK = threading.Lock()     # in-memory usage / network state
FAILS = {}  # ip -> (count, until)

os.makedirs(OVPN_DIR, mode=0o700, exist_ok=True)
os.makedirs(BACKUP_DIR, mode=0o700, exist_ok=True)
if not PASSWORD and not os.path.exists(AUTH_FILE):
    sys.exit("PANEL_PASSWORD is not set (needed once, to create the first 'admin' account)")
if not os.path.exists(SECRET_FILE):
    with open(SECRET_FILE, "w") as f:
        f.write(secrets.token_hex(32))
    os.chmod(SECRET_FILE, 0o600)
if not os.path.exists(PORTAL_SECRET_FILE):   # shared secret between the panel and portal.py
    with open(PORTAL_SECRET_FILE, "w") as f:
        f.write(secrets.token_hex(32))
    os.chmod(PORTAL_SECRET_FILE, 0o600)
PORTAL_SECRET = open(PORTAL_SECRET_FILE).read().strip()

app = Flask(__name__)
app.secret_key = open(SECRET_FILE).read().strip()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict",
                  MAX_CONTENT_LENGTH=30 * 1024 * 1024)


def fmtb(n):
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.2f} {u}"
        n /= 1024


app.add_template_filter(fmtb)


# ====================== icons (inline SVG) ======================
ICONS = {
 "home": '<path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/>',
 "wifi": '<path d="M5 12.55a11 11 0 0 1 14.08 0"/><path d="M1.42 9a16 16 0 0 1 21.16 0"/><path d="M8.53 16.11a6 6 0 0 1 6.95 0"/><line x1="12" y1="20" x2="12.01" y2="20"/>',
 "bars": '<line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/>',
 "drive": '<line x1="22" y1="12" x2="2" y2="12"/><path d="M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/><line x1="6" y1="16" x2="6.01" y2="16"/><line x1="10" y1="16" x2="10.01" y2="16"/>',
 "sliders": '<line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/><line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/><line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/><line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="17" y1="16" x2="23" y2="16"/>',
 "globe": '<circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>',
 "logout": '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/>',
 "user": '<path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
 "users": '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
 "user-plus": '<path d="M16 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="8.5" cy="7" r="4"/><line x1="20" y1="8" x2="20" y2="14"/><line x1="23" y1="11" x2="17" y2="11"/>',
 "plus": '<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>',
 "down": '<line x1="12" y1="5" x2="12" y2="19"/><polyline points="19 12 12 19 5 12"/>',
 "up": '<line x1="12" y1="19" x2="12" y2="5"/><polyline points="5 12 12 5 19 12"/>',
 "download": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>',
 "upload": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/>',
 "trash": '<polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/>',
 "edit": '<path d="M17 3a2.828 2.828 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"/>',
 "clock": '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
 "calendar": '<rect x="3" y="4" width="18" height="18" rx="2" ry="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/>',
 "zap": '<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>',
 "shield": '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
 "key": '<path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.778 7.778 5.5 5.5 0 0 1 7.777-7.777zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4"/>',
 "lock": '<rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
 "unlock": '<rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 9.9-1"/>',
 "server": '<rect x="2" y="2" width="20" height="8" rx="2" ry="2"/><rect x="2" y="14" width="20" height="8" rx="2" ry="2"/><line x1="6" y1="6" x2="6.01" y2="6"/><line x1="6" y1="18" x2="6.01" y2="18"/>',
 "alert": '<path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
 "slash": '<circle cx="12" cy="12" r="10"/><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/>',
 "check": '<polyline points="20 6 9 17 4 12"/>',
 "refresh": '<polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>',
 "activity": '<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>',
 "copy": '<rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
 "link": '<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>',
 "external": '<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/>',
 "cpu": '<rect x="4" y="4" width="16" height="16" rx="2" ry="2"/><rect x="9" y="9" width="6" height="6"/><line x1="9" y1="1" x2="9" y2="4"/><line x1="15" y1="1" x2="15" y2="4"/><line x1="9" y1="20" x2="9" y2="23"/><line x1="15" y1="20" x2="15" y2="23"/><line x1="20" y1="9" x2="23" y2="9"/><line x1="20" y1="14" x2="23" y2="14"/><line x1="1" y1="9" x2="4" y2="9"/><line x1="1" y1="14" x2="4" y2="14"/>',
 "memory": '<rect x="2" y="6" width="20" height="12" rx="2"/><line x1="6" y1="10" x2="6" y2="14"/><line x1="10" y1="10" x2="10" y2="14"/><line x1="14" y1="10" x2="14" y2="14"/><line x1="18" y1="10" x2="18" y2="14"/>',
 "infinity": '<path d="M18.178 8c5.096 0 5.096 8 0 8-5.095 0-7.133-8-12.356-8-5.096 0-5.096 8 0 8 5.223 0 7.26-8 12.356-8z"/>',
 "map": '<path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"/><circle cx="12" cy="10" r="3"/>',
 "chevron": '<polyline points="6 9 12 15 18 9"/>',
}


def icon(name, size=18, cls=""):
    return Markup(f'<svg class="ic {cls}" width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" '
                  f'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" '
                  f'aria-hidden="true">{ICONS[name]}</svg>')


# ====================== i18n (default: English) ======================
TR = {
 "en": {
  "lang_name": "فارسی", "nav_dash": "Dashboard", "nav_net": "Network Usage", "nav_usage": "User Usage",
  "logout": "Logout", "menu": "Menu",
  "login_title": "Sign in", "login_sub": "Enter your password to continue", "login_pw": "Password",
  "login_btn": "Sign in", "err_pw": "Wrong password", "err_many": "Too many attempts, wait a few minutes",
  "warn_nohost": "OpenVPN is not installed on the server (/etc/openvpn/server/server.conf not found). Run the Nyr script once on the server first.",
  "st_total": "Total users", "st_active": "Active", "st_soon": "Expiring soon (< 3 days)", "st_unl": "No expiry",
  "c_title": "Create new user", "c_id": "ID (auto)", "c_name": "Name (English)", "c_port": "Port",
  "c_manual": "Manual", "c_auto": "Auto", "c_days": "Duration (days) — 0 = unlimited", "c_btn": "Create",
  "u_title": "Users", "u_search": "Search by name or port...", "u_none": "No users yet",
  "port": "Port", "on": "Active", "off": "Inactive", "nolimit": "No time limit",
  "dl_ovpn": "Download .ovpn", "extend": "Extend", "days_ph": "days", "unl_title": "Remove expiry",
  "delete": "Delete", "confirm_del": "Delete user {name} and revoke its certificate?",
  "left_dh": "{d}d {h}h left", "left_hm": "{h}h {m}m left", "left_m": "{m} min left", "left_exp": "Expired",
  "f_num": "Port and days must be numbers", "f_nohost": "OpenVPN is not installed on the server (server.conf not found)",
  "f_name": "Name: English letters, digits, - and _ only (max 32 chars)",
  "f_days": "Days must be between 0 and 3650 (0 = unlimited)", "f_exists": "This name already exists",
  "f_noport": "No free port found", "f_build": "Creation failed: {err}", "f_svc": "Service failed to start:\n{log}",
  "f_created": "User {name} created (ID #{id}, {proto} port {port})", "f_created_exp": ", expires in {days} days",
  "f_badday": "Invalid number of days", "f_bad": "Invalid request", "f_extended": "Added {days} days to {name}",
  "f_unlimited": "Expiry removed for {name}", "f_deleted": "User {name} deleted and certificate revoked",
  "p_range": "Port must be between 1 and 65535", "p_reserved": "Port {port} is reserved (main panel, SSH or this panel)",
  "p_taken": "Port {port} is already used by user “{name}”", "p_busy": "Port {port} is in use on the server",
  "p_free": "Available", "p_bad": "Invalid number",
  "nav_backup": "Backup",
  "b_create_title": "Create backup",
  "b_create_desc": "Download a backup of all current users: panel data (IDs, ports, protocol, speed limits, expiry, usage totals) plus the certificates and keys needed to restore them.",
  "b_warn": "The backup contains private keys (including the CA key). Keep it somewhere safe and never share it.",
  "b_download": "Download backup", "b_users": "Users in backup",
  "b_import_title": "Import backup",
  "b_import_desc": "Upload a backup made by GassLight. Users whose name already exists are skipped. IDs and ports are kept when free, otherwise the next free ones are used. Download links (.ovpn) are rebuilt with this server's address.",
  "b_file": "Backup file (.zip)",
  "b_restore_pki": "Restore certificates from the backup (needed when the backup comes from a different server / CA)",
  "b_restore_hint": "This replaces the server's current certificates. Clients created outside this panel with the old certificates will stop working. A safety copy of the current certificates is saved first.",
  "b_confirm": "This will REPLACE the server's certificates with the ones from the backup. Continue?",
  "b_import_btn": "Import",
  "f_b_nofile": "Choose a backup file first", "f_b_bad": "Invalid or corrupted backup file",
  "f_b_big": "Backup file is too large (max 30 MB)", "f_b_ver": "Unsupported backup version",
  "f_b_ca": "This backup was made with a different CA. Tick “Restore certificates” to import it (this replaces the server's current certificates).",
  "f_b_done": "Import finished: {added} added, {skipped} already existed, {expired} expired (skipped), {failed} failed",
  "f_b_failed": "Failed users: {names} (see: journalctl -u ovpn-panel)",
  "f_b_safety": "A safety copy of the previous certificates was saved to {path}",
  "nav_adv": "Advanced", "nav_adv_acc": "My account", "nav_adv_px": "Proxy routing", "nav_adv_ex": "Exit servers & .ovpn address", "signed_as": "Signed in as", "login_user": "Username",
  "err_login": "Wrong username or password",
  "adv_title_acc": "My account", "adv_acc_desc": "Change your username and password. Leave the new password empty to keep the current one.",
  "adv_cur_pw": "Current password", "adv_username": "Username", "adv_new_pw": "New password",
  "adv_rep_pw": "Repeat new password", "adv_save": "Save changes",
  "adv_title_users": "Panel users", "adv_users_desc": "Everyone listed here can sign in to this panel.",
  "adv_you": "you", "adv_add_title": "Add panel user", "adv_password": "Password", "adv_add_btn": "Add user",
  "adv_del_confirm": "Delete panel user {name}?",
  "f_acc_pw": "Current password is wrong", "f_acc_user": "Username must be 3-32 characters: letters, digits, . _ -",
  "f_acc_exists": "This username already exists", "f_acc_short": "Password must be at least 6 characters",
  "f_acc_mismatch": "Passwords do not match", "f_acc_saved": "Account updated",
  "f_adm_added": "Panel user {name} added", "f_adm_deleted": "Panel user {name} deleted",
  "f_adm_self": "You can't delete your own account here", "f_adm_last": "At least one panel user is required",
  "c_daily": "Daily limit (GB) — 0 = none", "c_total": "Total limit (GB) — 0 = none",
  "edit_btn": "Edit", "edit_title": "Edit user", "edit_id": "ID", "edit_port": "Port", "edit_proto": "Protocol",
  "edit_speed": "Max speed (Mbps) — 0 = unlimited", "edit_daily": "Daily limit (GB) — 0 = none",
  "edit_total": "Total limit (GB) — 0 = none", "edit_save": "Save", "edit_cancel": "Cancel",
  "edit_warn": "Changing ID, port or protocol restarts this user's VPN and the old .ovpn file stops working — download the new one.",
  "reset_usage": "Reset usage", "confirm_reset": "Reset the usage counters of {name}?",
  "f_edit_ok": "User {name} updated", "f_edit_id": "ID must be a positive number that no other user uses",
  "f_edit_lim": "Limits must be between 0 and 1000000 GB",
  "f_edit_fail": "Update failed, previous settings were restored: {err}", "f_reset_ok": "Usage of {name} was reset",
  "blk_daily": "Blocked: daily limit", "blk_total": "Blocked: total limit", "lim_daily": "Daily", "lim_total": "Total",
  "u_local_ip": "Local IP", "u_last": "Last connection", "u_online": "Online", "u_offline": "Offline",
  "u_never": "Never connected", "u_since": "since", "u_today": "Today", "u_daily_lim": "Daily limit",
  "u_total_lim": "Total limit", "u_nolimit": "No limit", "ago_now": "just now", "ago_min": "{n} min ago",
  "ago_h": "{n} h ago", "ago_d": "{n} d ago",
  "ad_title": "Existing clients",
  "ad_desc": "These clients were created earlier (for example with openvpn-install.sh on the main port 1194) and are not managed by the panel yet. Add one to give it its own port, speed and data limits, and usage tracking. The same certificate is reused, so its old .ovpn on port 1194 keeps working; download the new file from the user list.",
  "ad_add": "Add as",
  "f_adopt_ok": "{name} was added to the panel (ID #{id}, {proto} port {port}). Download its new .ovpn from the user list.",
  "f_adopt_bad": "This client can't be added (not found, or already in the panel)",
  "dl_pw": "Download password", "copy_pw": "Copy password", "copy_link": "Copy link",
  "open_portal": "Open the user page", "regen_pw": "New password", "regen_link": "New link", "copied": "Copied",
  "confirm_regen_pw": "Create a new download password for {name}? The old one stops working.",
  "confirm_regen_link": "Create a new link for {name}? The old link stops working.",
  "f_pw_regen": "New download password created for {name}", "f_link_regen": "New link created for {name}",
  "c_proto": "Protocol", "c_speed": "Max speed (Mbps) — 0 = unlimited", "speed_btn": "Set speed",
  "speed_hint": "Mbps, 0 = unlimited", "nospeed": "No speed limit", "f_proto": "Invalid protocol",
  "f_speed": "Speed must be between 0 and 10000 Mbps (0 = unlimited)",
  "f_speed_set": "Speed limit of {name} set to {speed} Mbps", "f_speed_off": "Speed limit removed for {name}",
  "f_tc_warn": "Speed limit is saved but could not be applied yet (is 'tc' installed and the user's service running?). The panel retries every 30 seconds.",
  "n_cpu": "CPU", "n_ram": "RAM", "n_sys": "System load", "n_cores": "cores", "n_load": "load", "n_swap": "swap",
  "n_down": "Download (RX)", "n_up": "Upload (TX)", "n_live": "Live traffic", "n_iface": "interface",
  "n_dn": "Download", "n_upl": "Upload", "n_today": "Today", "n_month": "This month", "n_boot": "Since server boot",
  "n_peak": "Peak (last 3 min)", "m3": "-3m", "m15": "-1.5m", "now": "now",
  "us_n": "Total users", "us_d": "Total download", "us_u": "Total upload", "us_t": "Total traffic",
  "us_title": "Per-user usage (since creation)", "us_refresh": "live, every 2s",
  "th_user": "User", "th_created": "Created", "th_down": "Download", "th_up": "Upload", "th_total": "Total",
  "th_share": "Share", "th_live": "Live speed", "us_load": "Loading...", "d_ago": "{n} days ago",
  "us_note": "Usage comes from OpenVPN's own per-session counters (what really went through the tunnel, including encryption overhead). Download = data sent to the user. A user counts as online only while their device keeps talking to the server.",
 },
 "fa": {
  "lang_name": "English", "nav_dash": "داشبورد", "nav_net": "مصرف شبکه", "nav_usage": "مصرف کاربران",
  "logout": "خروج", "menu": "منو",
  "login_title": "ورود", "login_sub": "برای ورود رمز رو وارد کن", "login_pw": "رمز عبور",
  "login_btn": "ورود", "err_pw": "رمز اشتباه است", "err_many": "تعداد تلاش زیاد بود، چند دقیقه صبر کن",
  "warn_nohost": "OpenVPN روی سرور نصب نیست (فایل /etc/openvpn/server/server.conf پیدا نشد). اول اسکریپت Nyr رو یه بار روی خود سرور اجرا کن.",
  "st_total": "کل کاربرها", "st_active": "فعال", "st_soon": "نزدیک انقضا (کمتر از ۳ روز)", "st_unl": "بدون انقضا",
  "c_title": "ساخت کاربر جدید", "c_id": "ID (خودکار)", "c_name": "اسم (انگلیسی)", "c_port": "پورت",
  "c_manual": "دستی", "c_auto": "خودکار", "c_days": "مدت (روز) — ۰ = نامحدود", "c_btn": "ساخت",
  "u_title": "کاربرها", "u_search": "جستجو بر اساس اسم یا پورت...", "u_none": "هنوز کاربری نساختی",
  "port": "پورت", "on": "فعال", "off": "غیرفعال", "nolimit": "بدون محدودیت زمانی",
  "dl_ovpn": "دانلود .ovpn", "extend": "تمدید", "days_ph": "روز", "unl_title": "برداشتن انقضا",
  "delete": "حذف", "confirm_del": "کاربر {name} حذف و گواهیش باطل بشه؟",
  "left_dh": "{d} روز و {h} ساعت مانده", "left_hm": "{h} ساعت و {m} دقیقه مانده", "left_m": "{m} دقیقه مانده",
  "left_exp": "منقضی شده",
  "f_num": "پورت و تعداد روز باید عدد باشن", "f_nohost": "OpenVPN روی سرور نصب نیست (فایل server.conf پیدا نشد)",
  "f_name": "اسم فقط حروف انگلیسی، عدد، - و _ (حداکثر ۳۲ کاراکتر)",
  "f_days": "تعداد روز باید بین ۰ تا ۳۶۵۰ باشه (۰ = نامحدود)", "f_exists": "این اسم قبلاً ساخته شده",
  "f_noport": "پورت آزاد پیدا نشد", "f_build": "خطا در ساخت: {err}", "f_svc": "سرویس بالا نیومد:\n{log}",
  "f_created": "کاربر {name} ساخته شد (ID #{id} ، {proto} پورت {port})", "f_created_exp": "، انقضا: {days} روز دیگه",
  "f_badday": "تعداد روز نامعتبره", "f_bad": "درخواست نامعتبره", "f_extended": "{days} روز به {name} اضافه شد",
  "f_unlimited": "انقضای {name} برداشته شد", "f_deleted": "کاربر {name} حذف و گواهیش باطل شد",
  "p_range": "پورت باید بین ۱ تا ۶۵۵۳۵ باشه", "p_reserved": "پورت {port} رزرو شده (پنل اصلی، SSH یا همین پنل)",
  "p_taken": "پورت {port} قبلاً برای کاربر «{name}» استفاده شده", "p_busy": "پورت {port} روی سرور در حال استفاده‌ست",
  "p_free": "آزاده", "p_bad": "عدد نامعتبره",
  "nav_backup": "بکاپ",
  "b_create_title": "ساخت بکاپ",
  "b_create_desc": "از همه‌ی کاربرهای فعلی بکاپ بگیر: اطلاعات پنل (ID، پورت، پروتکل، محدودیت سرعت، انقضا، مجموع مصرف) به‌علاوه‌ی گواهی‌ها و کلیدهای لازم برای بازگردانی.",
  "b_warn": "بکاپ شامل کلیدهای خصوصی (از جمله کلید CA) هست. جای امنی نگهش دار و به کسی نده.",
  "b_download": "دانلود بکاپ", "b_users": "کاربرها در بکاپ",
  "b_import_title": "وارد کردن بکاپ (Import)",
  "b_import_desc": "فایل بکاپی که با GassLight ساخته شده رو آپلود کن. کاربرهایی که اسمشون الان هست رد می‌شن. ID و پورت اگه آزاد باشن همون می‌مونن، وگرنه اولین مقدار آزاد داده می‌شه. فایل‌های .ovpn با آدرس همین سرور دوباره ساخته می‌شن.",
  "b_file": "فایل بکاپ (.zip)",
  "b_restore_pki": "بازگردانی گواهی‌ها از بکاپ (وقتی لازمه که بکاپ از سرور / CA دیگه‌ای باشه)",
  "b_restore_hint": "این کار گواهی‌های فعلی سرور رو جایگزین می‌کنه. کلاینت‌هایی که بیرون از این پنل با گواهی قبلی ساخته شدن از کار می‌افتن. قبلش یه نسخه‌ی امن از گواهی‌های فعلی ذخیره می‌شه.",
  "b_confirm": "گواهی‌های سرور با گواهی‌های بکاپ جایگزین می‌شن. ادامه بدم؟",
  "b_import_btn": "وارد کردن",
  "f_b_nofile": "اول یه فایل بکاپ انتخاب کن", "f_b_bad": "فایل بکاپ نامعتبر یا خراب است",
  "f_b_big": "فایل بکاپ خیلی بزرگه (حداکثر ۳۰ مگابایت)", "f_b_ver": "نسخه‌ی بکاپ پشتیبانی نمی‌شه",
  "f_b_ca": "این بکاپ با CA دیگه‌ای ساخته شده. برای وارد کردنش تیک «بازگردانی گواهی‌ها» رو بزن (گواهی‌های فعلی سرور جایگزین می‌شن).",
  "f_b_done": "وارد کردن تموم شد: {added} اضافه شد، {skipped} از قبل بود، {expired} منقضی (رد شد)، {failed} ناموفق",
  "f_b_failed": "کاربرهای ناموفق: {names} (لاگ: journalctl -u ovpn-panel)",
  "f_b_safety": "نسخه‌ی امن گواهی‌های قبلی اینجا ذخیره شد: {path}",
  "nav_adv": "پیشرفته", "nav_adv_acc": "حساب من", "nav_adv_px": "مسیریابی پروکسی", "nav_adv_ex": "سرورهای خروجی و آدرس .ovpn", "signed_as": "واردشده با", "login_user": "نام کاربری",
  "err_login": "نام کاربری یا رمز اشتباه است",
  "adv_title_acc": "حساب من", "adv_acc_desc": "نام کاربری و رمزت رو عوض کن. اگه رمز جدید رو خالی بذاری رمز فعلی می‌مونه.",
  "adv_cur_pw": "رمز فعلی", "adv_username": "نام کاربری", "adv_new_pw": "رمز جدید",
  "adv_rep_pw": "تکرار رمز جدید", "adv_save": "ذخیره‌ی تغییرات",
  "adv_title_users": "کاربران پنل", "adv_users_desc": "همه‌ی کسایی که اینجا هستن می‌تونن وارد پنل بشن.",
  "adv_you": "شما", "adv_add_title": "افزودن کاربر پنل", "adv_password": "رمز عبور", "adv_add_btn": "افزودن",
  "adv_del_confirm": "کاربر پنل {name} حذف بشه؟",
  "f_acc_pw": "رمز فعلی اشتباهه", "f_acc_user": "نام کاربری باید ۳ تا ۳۲ کاراکتر باشه: حروف انگلیسی، عدد، . _ -",
  "f_acc_exists": "این نام کاربری قبلاً هست", "f_acc_short": "رمز باید حداقل ۶ کاراکتر باشه",
  "f_acc_mismatch": "رمزها یکی نیستن", "f_acc_saved": "حساب به‌روز شد",
  "f_adm_added": "کاربر پنل {name} اضافه شد", "f_adm_deleted": "کاربر پنل {name} حذف شد",
  "f_adm_self": "حساب خودت رو از اینجا نمی‌تونی حذف کنی", "f_adm_last": "حداقل یه کاربر پنل لازمه",
  "c_daily": "سقف روزانه (GB) — ۰ = بدون سقف", "c_total": "سقف کلی (GB) — ۰ = بدون سقف",
  "edit_btn": "ویرایش", "edit_title": "ویرایش کاربر", "edit_id": "ID", "edit_port": "پورت", "edit_proto": "پروتکل",
  "edit_speed": "حداکثر سرعت (Mbps) — ۰ = نامحدود", "edit_daily": "سقف روزانه (GB) — ۰ = بدون سقف",
  "edit_total": "سقف کلی (GB) — ۰ = بدون سقف", "edit_save": "ذخیره", "edit_cancel": "انصراف",
  "edit_warn": "تغییر ID، پورت یا پروتکل، VPN این کاربر رو ری‌استارت می‌کنه و فایل .ovpn قبلی از کار می‌افته؛ فایل جدید رو دانلود کن.",
  "reset_usage": "ریست مصرف", "confirm_reset": "شمارنده‌های مصرف {name} صفر بشن؟",
  "f_edit_ok": "کاربر {name} به‌روز شد", "f_edit_id": "ID باید عدد مثبتی باشه که کاربر دیگه‌ای ازش استفاده نمی‌کنه",
  "f_edit_lim": "سقف‌ها باید بین ۰ تا ۱۰۰۰۰۰۰ گیگابایت باشن",
  "f_edit_fail": "به‌روزرسانی ناموفق بود، تنظیمات قبلی برگشت: {err}", "f_reset_ok": "مصرف {name} صفر شد",
  "blk_daily": "مسدود: سقف روزانه", "blk_total": "مسدود: سقف کلی", "lim_daily": "روزانه", "lim_total": "کلی",
  "u_local_ip": "آی‌پی لوکال", "u_last": "آخرین اتصال", "u_online": "آنلاین", "u_offline": "آفلاین",
  "u_never": "هیچ‌وقت وصل نشده", "u_since": "از", "u_today": "امروز", "u_daily_lim": "سقف روزانه",
  "u_total_lim": "سقف کلی", "u_nolimit": "بدون سقف", "ago_now": "همین الان", "ago_min": "{n} دقیقه پیش",
  "ago_h": "{n} ساعت پیش", "ago_d": "{n} روز پیش",
  "ad_title": "کلاینت‌های موجود",
  "ad_desc": "این کلاینت‌ها قبلاً ساخته شدن (مثلاً با openvpn-install.sh روی پورت اصلی 1194) و هنوز توسط پنل مدیریت نمی‌شن. با اضافه کردن، هر کدوم پورت، سرعت، سقف حجم و ردیابی مصرف جدا می‌گیره. همون گواهی قبلی استفاده می‌شه، پس .ovpn قدیمی روی پورت 1194 هم کار می‌کنه؛ فایل جدید رو از لیست کاربرها دانلود کن.",
  "ad_add": "افزودن با",
  "f_adopt_ok": "{name} به پنل اضافه شد (ID #{id} ، {proto} پورت {port}). فایل .ovpn جدیدش رو از لیست کاربرها دانلود کن.",
  "f_adopt_bad": "این کلاینت قابل افزودن نیست (پیدا نشد یا قبلاً توی پنل هست)",
  "dl_pw": "رمز دانلود", "copy_pw": "کپی رمز", "copy_link": "کپی لینک",
  "open_portal": "باز کردن صفحه‌ی کاربر", "regen_pw": "رمز جدید", "regen_link": "لینک جدید", "copied": "کپی شد",
  "confirm_regen_pw": "برای {name} رمز دانلود جدید ساخته بشه؟ رمز قبلی از کار می‌افته.",
  "confirm_regen_link": "برای {name} لینک جدید ساخته بشه؟ لینک قبلی از کار می‌افته.",
  "f_pw_regen": "رمز دانلود جدید برای {name} ساخته شد", "f_link_regen": "لینک جدید برای {name} ساخته شد",
  "c_proto": "پروتکل", "c_speed": "حداکثر سرعت (Mbps) — ۰ = نامحدود", "speed_btn": "تنظیم سرعت",
  "speed_hint": "Mbps ، ۰ = نامحدود", "nospeed": "بدون محدودیت سرعت", "f_proto": "پروتکل نامعتبره",
  "f_speed": "سرعت باید بین ۰ تا ۱۰۰۰۰ Mbps باشه (۰ = نامحدود)",
  "f_speed_set": "سرعت {name} روی {speed} Mbps تنظیم شد", "f_speed_off": "محدودیت سرعت {name} برداشته شد",
  "f_tc_warn": "محدودیت سرعت ذخیره شد ولی هنوز اعمال نشد (آیا tc نصبه و سرویس کاربر بالاست؟). پنل هر ۳۰ ثانیه دوباره تلاش می‌کنه.",
  "n_cpu": "پردازنده (CPU)", "n_ram": "رم (RAM)", "n_sys": "منابع سیستم", "n_cores": "هسته", "n_load": "لود", "n_swap": "swap",
  "n_down": "دانلود (RX)", "n_up": "آپلود (TX)", "n_live": "ترافیک زنده", "n_iface": "کارت شبکه",
  "n_dn": "دانلود", "n_upl": "آپلود", "n_today": "امروز", "n_month": "این ماه", "n_boot": "از زمان بوت سرور",
  "n_peak": "پیک ۳ دقیقه اخیر", "m3": "-3m", "m15": "-1.5m", "now": "now",
  "us_n": "کل کاربرها", "us_d": "کل دانلود کاربرها", "us_u": "کل آپلود کاربرها", "us_t": "مجموع ترافیک",
  "us_title": "مصرف هر کاربر (از زمان ساخت تا امروز)", "us_refresh": "زنده، هر ۲ ثانیه",
  "th_user": "کاربر", "th_created": "تاریخ ساخت", "th_down": "دانلود", "th_up": "آپلود", "th_total": "مجموع",
  "th_share": "سهم از کل", "th_live": "سرعت لحظه‌ای", "us_load": "در حال بارگذاری...", "d_ago": "{n} روز پیش",
  "us_note": "مصرف از روی شمارنده‌های خود OpenVPN برای هر نشست حساب می‌شه (چیزی که واقعاً از تونل رد شده، با سربار رمزنگاری). دانلود یعنی داده‌ای که به سمت کاربر رفته. کاربر فقط وقتی آنلاین حساب می‌شه که دستگاهش با سرور در تماس باشه.",
 },
}


TR["en"].update({
  "px_title": "Proxy routing (change exit IP)",
  "px_desc": "Send users' TCP traffic through the upstream SOCKS4 / SOCKS5 / HTTP proxies below, so websites see the proxy's IP instead of this server's. Applies to every user of the panel.",
  "px_enable": "Enable proxy routing",
  "px_list": "Proxy list (one per line)",
  "px_hint": "Formats: ip:port, socks5://ip:port, socks4://ip:port, http://ip:port, optionally with user:pass@ before the address. Lines without a scheme use the default type below. Lines starting with # are ignored.",
  "px_dtype": "Default type (for lines without a scheme)",
  "px_mode": "Rotation",
  "px_m_sticky": "One proxy per user (changes only when it fails or after the interval)",
  "px_m_random": "Random proxy for every connection",
  "px_m_round": "Round-robin for every connection",
  "px_rot": "Re-pick every (minutes, 0 = only when it fails) — one-proxy-per-user mode",
  "px_fb": "If every proxy fails, connect directly (exposes this server's IP)",
  "px_quic": "Block QUIC / HTTP3 (UDP 443) so browsers fall back to TCP and use the proxy",
  "px_note": "Only TCP is proxied: UDP (DNS, calls, games) still leaves from this server. Free public proxies are slow and unreliable, and can read or alter traffic that is not encrypted. If the panel is stopped while this is enabled, users have no internet until it starts again.",
  "px_save": "Save", "px_test": "Test all", "px_prune": "Remove failed",
  "px_testing_n": "Testing {done} / {total}...",
  "px_st_ok": "Working", "px_st_bad": "Failed", "px_st_unk": "Not tested", "px_st_cool": "Cooling down",
  "px_none": "No proxies yet", "px_relay_bad": "The relay could not start on port {port} (port busy?)",
  "px_status": "Proxies",
  "f_px_saved": "Proxy settings saved: {n} proxies", "f_px_bad": " ({bad} invalid lines skipped)",
  "f_px_empty": "Add at least one valid proxy before enabling", "f_px_many": "Too many proxies (max {max})",
  "f_px_testing": "Testing started", "f_px_busy": "A test is already running", "f_px_pruned": "{n} failed proxies removed",
})
TR["fa"].update({
  "px_title": "مسیردهی از طریق پروکسی (تغییر IP خروجی)",
  "px_desc": "ترافیک TCP کاربرها از طریق پروکسی‌های SOCKS4 / SOCKS5 / HTTP زیر رد می‌شه، پس سایت‌ها به جای IP این سرور، IP پروکسی رو می‌بینن. برای همه‌ی کاربرهای پنل اعمال می‌شه.",
  "px_enable": "فعال‌سازی مسیردهی از پروکسی",
  "px_list": "لیست پروکسی‌ها (هر خط یکی)",
  "px_hint": "فرمت‌ها: ip:port ، socks5://ip:port ، socks4://ip:port ، http://ip:port ، و اختیاری user:pass@ قبل از آدرس. خط‌های بدون scheme از نوع پیش‌فرض پایین استفاده می‌کنن. خط‌هایی که با # شروع بشن نادیده گرفته می‌شن.",
  "px_dtype": "نوع پیش‌فرض (برای خط‌های بدون scheme)",
  "px_mode": "چرخش",
  "px_m_sticky": "یک پروکسی برای هر کاربر (فقط وقتی خراب بشه یا بعد از بازه‌ی زمانی عوض می‌شه)",
  "px_m_random": "پروکسی تصادفی برای هر اتصال",
  "px_m_round": "چرخشی (نوبتی) برای هر اتصال",
  "px_rot": "تغییر پروکسی هر چند دقیقه (۰ = فقط وقتی خراب بشه) — برای حالت یک پروکسی برای هر کاربر",
  "px_fb": "اگه همه‌ی پروکسی‌ها خراب بودن، مستقیم وصل شو (IP این سرور فاش می‌شه)",
  "px_quic": "بستن QUIC / HTTP3 (‏UDP 443) تا مرورگر به TCP برگرده و از پروکسی رد بشه",
  "px_note": "فقط TCP از پروکسی رد می‌شه: UDP (دی‌ان‌اس، تماس، بازی) هنوز از خود این سرور خارج می‌شه. پروکسی‌های رایگان عمومی کند و ناپایدارن و می‌تونن ترافیک رمزنشده رو بخونن یا تغییر بدن. اگه پنل وسط کار خاموش بشه تا دوباره بالا نیاد کاربرها اینترنت ندارن.",
  "px_save": "ذخیره", "px_test": "تست همه", "px_prune": "حذف خراب‌ها",
  "px_testing_n": "در حال تست {done} / {total}...",
  "px_st_ok": "سالم", "px_st_bad": "خراب", "px_st_unk": "تست نشده", "px_st_cool": "موقتاً کنار گذاشته شده",
  "px_none": "هنوز پروکسی‌ای نیست", "px_relay_bad": "رله روی پورت {port} بالا نیومد (پورت اشغاله؟)",
  "px_status": "پروکسی‌ها",
  "f_px_saved": "تنظیمات پروکسی ذخیره شد: {n} پروکسی", "f_px_bad": " ({bad} خط نامعتبر رد شد)",
  "f_px_empty": "قبل از فعال کردن، حداقل یک پروکسی معتبر اضافه کن", "f_px_many": "تعداد پروکسی‌ها زیاده (حداکثر {max})",
  "f_px_testing": "تست شروع شد", "f_px_busy": "یه تست دیگه در حال اجراست", "f_px_pruned": "{n} پروکسی خراب حذف شد",
})


TR["en"].update({
  "ex_title": "Exit servers (your own SOCKS5 servers)",
  "ex_desc": "Send users' TCP traffic out through your own servers in other countries (SOCKS5 with username and password). The panel rotates between the active servers and skips any that go down. Users' .ovpn files never change. While this is enabled it replaces Proxy Routing above.",
  "ex_enable": "Enable exit servers",
  "ex_m_sticky": "One exit server per user (changes only when it goes down or after the interval)",
  "ex_m_random": "Random exit server for every connection",
  "ex_m_round": "Round-robin for every connection",
  "ex_rot": "Re-pick every (minutes, 0 = only when it goes down) — one-per-user mode",
  "ex_fb": "If every exit server is down, connect directly (exposes this server's IP)",
  "ex_note": "Only TCP leaves through the exit servers: UDP (DNS, calls, games) still leaves from this server. Servers are health-checked every 30 seconds. If the panel is stopped while this is enabled, users have no internet until it starts again. Set up each exit server with exit-server-setup.sh.",
  "ex_servers": "Servers", "ex_none": "No exit servers yet", "ex_add": "Add server", "ex_add_btn": "Add",
  "ex_ip": "IP or hostname", "ex_port": "Port", "ex_user": "Username", "ex_pw": "Password",
  "ex_pw_keep": "Leave empty to keep the current password",
  "ex_edit": "Edit", "ex_on": "Enable", "ex_off": "Disable", "ex_del": "Delete",
  "ex_confirm_del": "Delete exit server {ip}?",
  "ex_st_ok": "Working", "ex_st_bad": "Down", "ex_st_unk": "Not checked", "ex_st_cool": "Skipped for now", "ex_st_off": "Disabled",
  "ex_testing_n": "Testing {done} / {total}...",
  "f_ex_saved": "Exit server settings saved", "f_ex_added": "Exit server added", "f_ex_updated": "Exit server updated",
  "f_ex_deleted": "Exit server deleted", "f_ex_toggled": "Exit server updated",
  "f_ex_noactive": "Add and enable at least one exit server first",
  "f_ex_autooff": "No active exit server is left, so exit routing was turned off",
  "f_ex_bad_ip": "Invalid IP or hostname", "f_ex_bad_port": "Invalid port (1-65535)",
  "f_ex_bad_user": "Username is required", "f_ex_bad_pw": "Password is required",
  "f_ex_missing": "Exit server not found", "f_ex_many": "Too many exit servers (max {max})",
})
TR["fa"].update({
  "ex_title": "سرورهای خروجی (سرورهای SOCKS5 خودت)",
  "ex_desc": "ترافیک TCP کاربرها از سرورهای خودت توی کشورهای دیگه خارج می‌شه (SOCKS5 با یوزر و پسورد). پنل بین سرورهای فعال می‌چرخونه و سروری که قطع بشه رو دور می‌زنه. فایل .ovpn کاربرها هیچ‌وقت تغییر نمی‌کنه. تا وقتی این فعاله، جای «مسیردهی از طریق پروکسی» بالا رو می‌گیره.",
  "ex_enable": "فعال‌سازی سرورهای خروجی",
  "ex_m_sticky": "یک سرور خروجی برای هر کاربر (فقط وقتی قطع بشه یا بعد از بازه‌ی زمانی عوض می‌شه)",
  "ex_m_random": "سرور خروجی تصادفی برای هر اتصال",
  "ex_m_round": "چرخشی (نوبتی) برای هر اتصال",
  "ex_rot": "تغییر سرور هر چند دقیقه (۰ = فقط وقتی قطع بشه) — برای حالت یک سرور برای هر کاربر",
  "ex_fb": "اگه همه‌ی سرورهای خروجی قطع بودن، مستقیم وصل شو (IP این سرور فاش می‌شه)",
  "ex_note": "فقط TCP از سرورهای خروجی خارج می‌شه: UDP (دی‌ان‌اس، تماس، بازی) هنوز از خود این سرور خارج می‌شه. سلامت سرورها هر ۳۰ ثانیه چک می‌شه. اگه پنل وسط کار خاموش بشه تا دوباره بالا نیاد کاربرها اینترنت ندارن. هر سرور خروجی رو با exit-server-setup.sh راه‌اندازی کن.",
  "ex_servers": "سرورها", "ex_none": "هنوز سرور خروجی‌ای نیست", "ex_add": "افزودن سرور", "ex_add_btn": "افزودن",
  "ex_ip": "آی‌پی یا هاست", "ex_port": "پورت", "ex_user": "یوزرنیم", "ex_pw": "پسورد",
  "ex_pw_keep": "برای نگه داشتن پسورد فعلی خالی بذار",
  "ex_edit": "ویرایش", "ex_on": "فعال", "ex_off": "غیرفعال", "ex_del": "حذف",
  "ex_confirm_del": "سرور خروجی {ip} حذف بشه؟",
  "ex_st_ok": "سالم", "ex_st_bad": "قطع", "ex_st_unk": "چک نشده", "ex_st_cool": "موقتاً دور زده می‌شه", "ex_st_off": "غیرفعال",
  "ex_testing_n": "در حال تست {done} / {total}...",
  "f_ex_saved": "تنظیمات سرورهای خروجی ذخیره شد", "f_ex_added": "سرور خروجی اضافه شد", "f_ex_updated": "سرور خروجی ویرایش شد",
  "f_ex_deleted": "سرور خروجی حذف شد", "f_ex_toggled": "سرور خروجی به‌روز شد",
  "f_ex_noactive": "اول حداقل یک سرور خروجی اضافه و فعال کن",
  "f_ex_autooff": "هیچ سرور خروجی فعالی نمونده، برای همین مسیردهی از سرور خروجی خاموش شد",
  "f_ex_bad_ip": "آی‌پی یا هاست نامعتبره", "f_ex_bad_port": "پورت نامعتبره (۱ تا ۶۵۵۳۵)",
  "f_ex_bad_user": "یوزرنیم لازمه", "f_ex_bad_pw": "پسورد لازمه",
  "f_ex_missing": "سرور خروجی پیدا نشد", "f_ex_many": "تعداد سرورهای خروجی زیاده (حداکثر {max})",
})


TR["en"].update({
  "oa_title": "Address inside .ovpn files",
  "oa_desc": "The IP or domain written in every downloaded .ovpn file. Put the IP of your Iran tunnel server here so users never have to edit the file. Leave it empty to use this server's own address. Users keep their own port, so the tunnel must carry those ports (see iran-tunnel-setup.sh).",
  "oa_label": "Address (IP or domain)", "oa_ph": "e.g. 185.10.20.30",
  "oa_hint": "Applies to every download right away, including users created earlier; clearing it brings the original address back. Files users already downloaded are not changed.",
  "f_oa_saved": "Address saved", "f_oa_cleared": "Address cleared: files use this server's own address",
  "f_oa_bad": "Enter only an IP or a domain name (no port, no http://)",
})
TR["fa"].update({
  "oa_title": "آدرس داخل فایل‌های .ovpn",
  "oa_desc": "آی‌پی یا دامنه‌ای که توی هر فایل .ovpn دانلودی نوشته می‌شه. آی‌پی سرور تانل ایران رو اینجا بذار تا کاربرها مجبور نشن فایل رو دستی ویرایش کنن. خالی بذاری آدرس خود همین سرور استفاده می‌شه. هر کاربر پورت خودش رو نگه می‌داره، پس تانل باید اون پورت‌ها رو جابه‌جا کنه (iran-tunnel-setup.sh).",
  "oa_label": "آدرس (آی‌پی یا دامنه)", "oa_ph": "مثلاً 185.10.20.30",
  "oa_hint": "همین الان برای همه‌ی دانلودها اعمال می‌شه، از جمله کاربرهای قبلی؛ خالی کردنش آدرس اصلی رو برمی‌گردونه. فایل‌هایی که کاربرها قبلاً دانلود کردن تغییر نمی‌کنن.",
  "f_oa_saved": "آدرس ذخیره شد", "f_oa_cleared": "آدرس پاک شد: فایل‌ها از آدرس خود این سرور استفاده می‌کنن",
  "f_oa_bad": "فقط آی‌پی یا نام دامنه وارد کن (بدون پورت و بدون http://)",
})


def cur_lang():
    l = request.cookies.get("lang")
    return l if l in TR else "en"


def tr(key, **kw):
    s = TR[cur_lang()].get(key) or TR["en"].get(key, key)
    return s.format(**kw) if kw else s


@app.context_processor
def inject_i18n():
    lang = cur_lang()
    return dict(t=tr, lang=lang, dirn="rtl" if lang == "fa" else "ltr",
                jt=lambda *keys: {k: tr(k) for k in keys},
                ic=icon, icj=lambda *names: {n: str(icon(n, 14)) for n in names},
                me=session.get("user"))


@app.route("/lang/<code>")
def set_lang(code):
    if code not in TR:
        abort(404)
    p = urlparse(request.referrer or "")
    target = "/"
    if p.netloc == request.host and p.path.startswith("/") and not p.path.startswith("//") \
            and not p.path.startswith("/lang/"):
        target = p.path + ("?" + p.query if p.query else "")
    resp = redirect(target)
    resp.set_cookie("lang", code, max_age=31536000, samesite="Lax")
    return resp


# ====================== state (users.json) ======================
_PW_POOLS = ("abcdefghijkmnpqrstuvwxyz", "ABCDEFGHJKLMNPQRSTUVWXYZ", "23456789", "@#$%&*+=?!-_")
FIELDS_LOCK = threading.Lock()


def gen_dl_password():
    """8 random characters: at least one lowercase, uppercase, digit and symbol (no look-alikes)."""
    chars = [secrets.choice(p) for p in _PW_POOLS]
    allc = "".join(_PW_POOLS)
    chars += [secrets.choice(allc) for _ in range(8 - len(chars))]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def new_token():
    return secrets.token_urlsafe(24)


def load_state():
    st = _read_state()
    if any(not u.get("token") or not u.get("dl_password") for u in st["users"]):
        with FIELDS_LOCK:
            st = _read_state()          # another thread may have filled them while we waited
            changed = False
            for u in st["users"]:
                if not u.get("token"):
                    u["token"], changed = new_token(), True
                if not u.get("dl_password"):
                    u["dl_password"], changed = gen_dl_password(), True
            if changed:
                save_state(st)
    return st


def _read_state():
    st = {"users": []}
    if os.path.exists(DB):
        with open(DB) as f:
            raw = json.load(f)
        if isinstance(raw, list):
            st["users"] = raw
        else:
            st.update(raw)
    for u in st["users"]:
        if "index" in u:
            u["id"] = u.pop("index")
        u.setdefault("subnet", f"10.{100 + u['id']}.0")
        u.setdefault("created", time.time())
        u.setdefault("expires", None)
        u.setdefault("proto", "udp")
        u.setdefault("speed", None)
        u.setdefault("limit_daily", None)
        u.setdefault("limit_total", None)
        u.setdefault("blocked", None)
    st.pop("next_id", None)   # ids / ports are now reused (smallest free)
    st.pop("last_port", None)
    return st


def save_state(st):
    tmp = f"{DB}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DB)


# ====================== system helpers ======================
def run(cmd, **kw):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, **kw)
    except (FileNotFoundError, PermissionError):
        return subprocess.CompletedProcess(cmd, 127, "", "command not found")


def host_ready():
    return os.path.exists(f"{SERVER_DIR}/server.conf") and os.path.isdir(ED)


def port_free(port, proto="udp"):
    kind = socket.SOCK_STREAM if proto == "tcp" else socket.SOCK_DGRAM
    sk = socket.socket(socket.AF_INET, kind)
    try:
        if proto == "tcp":
            sk.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sk.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        sk.close()


def is_active(name):
    return run(["systemctl", "is-active", f"openvpn-server@{name}"]).stdout.strip() == "active"


def port_error(st, port, proto="udp"):
    if not 1 <= port <= 65535:
        return tr("p_range")
    if port in RESERVED_PORTS or port == PORT:
        return tr("p_reserved", port=port)
    for u in st["users"]:
        if u["port"] == port:
            return tr("p_taken", port=port, name=u["name"])
    if not port_free(port, proto):
        return tr("p_busy", port=port)
    return None


def free_id(st):
    """Smallest unused ID: fills gaps left by deleted users first."""
    used = {u["id"] for u in st["users"]}
    i = 1
    while i in used:
        i += 1
    return i


def next_port(st):
    """Smallest free port above 1194 (fills gaps left by deleted users first)."""
    used = {u["port"] for u in st["users"]} | RESERVED_PORTS | {PORT}
    for p in range(1195, 65536):
        if p not in used and port_free(p, "udp") and port_free(p, "tcp"):
            return p
    return None


def new_subnet(idx, st):
    used = {u["subnet"] for u in st["users"]}
    n = idx - 1
    while True:
        sub = f"10.{101 + n // 250}.{n % 250 + 1}"
        if sub not in used:
            return sub
        n += 1


def nat_spec(sub):
    return ["-s", f"{sub}.0/24", "!", "-d", f"{sub}.0/24", "-j", "MASQUERADE"]


def drop_legacy_exit_ip():
    """The 'exit IP' feature was removed: undo any SNAT rules it left behind and restore MASQUERADE."""
    if not os.path.exists(SETTINGS_FILE):
        return
    try:
        with open(SETTINGS_FILE) as f:
            old = json.load(f).get("exit_ip")
    except Exception:
        old = None
    if old:
        for u in load_state()["users"]:
            sub = u["subnet"]
            snat = ["-s", f"{sub}.0/24", "!", "-d", f"{sub}.0/24", "-j", "SNAT", "--to-source", old]
            while run(["iptables", "-t", "nat", "-D", "POSTROUTING", *snat]).returncode == 0:
                pass
            if run(["iptables", "-t", "nat", "-C", "POSTROUTING", *nat_spec(sub)]).returncode != 0:
                run(["iptables", "-t", "nat", "-A", "POSTROUTING", *nat_spec(sub)])
    try:
        os.remove(SETTINGS_FILE)
    except OSError:
        pass


def rule_specs(sub, port, proto="udp"):
    return [
        (["-t", "nat"], "POSTROUTING", nat_spec(sub)),
        ([], "INPUT", ["-p", proto, "--dport", str(port), "-j", "ACCEPT"]),
        ([], "FORWARD", ["-s", f"{sub}.0/24", "-j", "ACCEPT"]),
        ([], "FORWARD", ["-d", f"{sub}.0/24", "-m", "state", "--state", "RELATED,ESTABLISHED", "-j", "ACCEPT"]),
    ]


def apply_rules(sub, port, proto="udp"):
    for t, chain, spec in rule_specs(sub, port, proto):
        if run(["iptables", *t, "-C", chain, *spec]).returncode != 0:
            run(["iptables", *t, "-A" if chain == "POSTROUTING" else "-I", chain, *spec])


def drop_rules(sub, port, proto="udp"):
    for t, chain, spec in rule_specs(sub, port, proto):
        while run(["iptables", *t, "-D", chain, *spec]).returncode == 0:
            pass


def build_ovpn(name, port, dst, proto="udp"):
    rd = lambda p: open(p).read()
    common = rd(f"{SERVER_DIR}/client-common.txt")
    common = re.sub(r"^remote (\S+) \d+", rf"remote \1 {port}", common, flags=re.M)
    common = re.sub(r"^proto .*", lambda m: f"proto {proto}", common, flags=re.M)
    if proto == "tcp":  # explicit-exit-notify only works with UDP
        common = re.sub(r"^explicit-exit-notify.*\n?", "", common, flags=re.M)
    cert = rd(f"{ED}/pki/issued/{name}.crt")
    cert = cert[cert.index("-----BEGIN CERTIFICATE-----"):]
    tc = rd(f"{SERVER_DIR}/tc.key")
    tc = tc[tc.index("-----BEGIN OpenVPN Static key"):]
    out = (common.rstrip("\n") + "\n"
           f"<ca>\n{rd(ED + '/pki/ca.crt')}</ca>\n"
           f"<cert>\n{cert}</cert>\n"
           f"<key>\n{rd(f'{ED}/pki/private/{name}.key')}</key>\n"
           f"<tls-crypt>\n{tc}</tls-crypt>\n")
    with open(dst, "w") as f:
        f.write(out)
    os.chmod(dst, 0o600)


def create_instance(name, port, sub, proto="udp"):
    """Returns (ok, error_message)."""
    env = dict(os.environ, EASYRSA_CERT_EXPIRE="3650")
    r = run(["./easyrsa", "--batch", "build-client-full", name, "nopass"], cwd=ED, env=env)
    if r.returncode != 0:
        return False, ((r.stderr or r.stdout)[-400:] or "easyrsa failed")
    return activate_instance(name, port, sub, proto)


def activate_instance(name, port, sub, proto="udp", revoke_on_fail=True, cleanup_on_fail=True):
    """Write conf/ccd, firewall rules, start the service, build the .ovpn.
    The client certificate must already exist in the PKI. Returns (ok, error)."""
    conf = open(f"{SERVER_DIR}/server.conf").read()
    conf = re.sub(r"^port .*", lambda m: f"port {port}", conf, flags=re.M)
    conf = re.sub(r"^server .*", lambda m: f"server {sub}.0 255.255.255.0", conf, flags=re.M)
    if re.search(r"^proto ", conf, re.M):
        conf = re.sub(r"^proto .*", lambda m: f"proto {proto}", conf, flags=re.M)
    else:
        conf = conf.rstrip("\n") + f"\nproto {proto}\n"
    if proto == "tcp":  # explicit-exit-notify only works with UDP
        conf = re.sub(r"^explicit-exit-notify.*\n?", "", conf, flags=re.M)
    conf = re.sub(r"^ifconfig-pool-persist .*", lambda m: f"ifconfig-pool-persist ipp-{name}.txt",
                  conf, flags=re.M)
    conf = conf.rstrip("\n") + f"\nclient-config-dir ccd-{name}\nccd-exclusive\n{mgmt_line(name)}\n"
    os.makedirs(f"{SERVER_DIR}/ccd-{name}", exist_ok=True)
    open(f"{SERVER_DIR}/ccd-{name}/{name}", "a").close()
    with open(f"{SERVER_DIR}/{name}.conf", "w") as f:
        f.write(conf)

    apply_rules(sub, port, proto)
    run(["ufw", "allow", f"{port}/{proto}"])
    run(["systemctl", "enable", "--now", f"openvpn-server@{name}"])
    time.sleep(1.5)
    if not is_active(name):
        log = run(["journalctl", "-u", f"openvpn-server@{name}", "-n", "8", "--no-pager"]).stdout
        if cleanup_on_fail:
            remove_instance({"name": name, "port": port, "subnet": sub, "proto": proto}, revoke=revoke_on_fail)
        else:
            run(["systemctl", "stop", f"openvpn-server@{name}"])
        return False, tr("f_svc", log=log[-500:])
    build_ovpn(name, port, f"{OVPN_DIR}/{name}.ovpn", proto)
    return True, ""


def edit_instance(u, new_id, new_port, new_proto):
    """Re-create a user's instance with another id (subnet) / port / protocol. Rolls back on failure."""
    name = u["name"]
    old_port, old_sub, old_proto = u["port"], u["subnet"], u.get("proto", "udp")
    sub = old_sub
    if new_id != u["id"]:
        others = {"users": [x for x in load_state()["users"] if x["name"] != name]}
        sub = new_subnet(new_id, others)
    run(["systemctl", "stop", f"openvpn-server@{name}"])
    drop_rules(old_sub, old_port, old_proto)
    run(["ufw", "delete", "allow", f"{old_port}/{old_proto}"])
    if sub != old_sub and os.path.exists(f"{SERVER_DIR}/ipp-{name}.txt"):
        os.remove(f"{SERVER_DIR}/ipp-{name}.txt")
    ok, err = activate_instance(name, new_port, sub, new_proto, revoke_on_fail=False, cleanup_on_fail=False)
    if not ok:
        drop_rules(sub, new_port, new_proto)
        run(["ufw", "delete", "allow", f"{new_port}/{new_proto}"])
        activate_instance(name, old_port, old_sub, old_proto, revoke_on_fail=False, cleanup_on_fail=False)
        return False, err
    u["id"], u["port"], u["subnet"], u["proto"] = new_id, new_port, sub, new_proto
    with SLOCK:
        if name in USAGE:
            USAGE[name]["lu"] = USAGE[name]["ld"] = 0   # new iptables rules start at zero
            USAGE[name]["sk"] = None                     # the old VPN session is gone
        CONN.pop(name, None)
    return True, ""


def remove_instance(u, revoke=True):
    name = u["name"]
    run(["systemctl", "disable", "--now", f"openvpn-server@{name}"])
    for p in (f"{SERVER_DIR}/{name}.conf", f"{SERVER_DIR}/ipp-{name}.txt", f"{OVPN_DIR}/{name}.ovpn"):
        if os.path.exists(p):
            os.remove(p)
    shutil.rmtree(f"{SERVER_DIR}/ccd-{name}", ignore_errors=True)
    proto = u.get("proto", "udp")
    drop_rules(u["subnet"], u["port"], proto)
    run(["ufw", "delete", "allow", f"{u['port']}/{proto}"])
    if revoke:
        run(["./easyrsa", "--batch", "revoke", name], cwd=ED)
        run(["./easyrsa", "gen-crl"], cwd=ED, env=dict(os.environ, EASYRSA_CRL_DAYS="3650"))
        try:
            shutil.copy(f"{ED}/pki/crl.pem", f"{SERVER_DIR}/crl.pem")
            os.chmod(f"{SERVER_DIR}/crl.pem", 0o644)
        except OSError:
            pass
    with SLOCK:
        USAGE.pop(name, None)
        RATES.pop(name, None)
        CONN.pop(name, None)


def sync_existing():
    """Import instances that were created earlier by hand."""
    if not os.path.isdir(SERVER_DIR):
        return
    st = load_state()
    known = {u["name"] for u in st["users"]}
    changed = False
    for fn in sorted(os.listdir(SERVER_DIR)):
        if not fn.endswith(".conf") or fn == "server.conf":
            continue
        name = fn[:-5]
        if name in known or not NAME_RE.match(name):
            continue
        try:
            txt = open(f"{SERVER_DIR}/{fn}").read()
            if "ccd-exclusive" not in txt:
                continue
            port = int(re.search(r"^port (\d+)", txt, re.M).group(1))
            m = re.search(r"^server (10\.(\d+)\.(\d+))\.0 ", txt, re.M)
            sub, a, b = m.group(1), int(m.group(2)), int(m.group(3))
        except Exception:
            continue
        ids = {u["id"] for u in st["users"]}
        idx = a - 100 if (b == 0 and a > 100 and a - 100 not in ids) else free_id(st)
        pm = re.search(r"^proto (\w+)", txt, re.M)
        proto = "tcp" if pm and pm.group(1).startswith("tcp") else "udp"
        dst = f"{OVPN_DIR}/{name}.ovpn"
        if not os.path.exists(dst):
            try:
                if os.path.exists(f"/root/{name}.ovpn"):
                    shutil.copy(f"/root/{name}.ovpn", dst)
                    os.chmod(dst, 0o600)
                else:
                    build_ovpn(name, port, dst, proto)
            except Exception:
                pass
        st["users"].append({"name": name, "id": idx, "port": port, "subnet": sub,
                            "created": time.time(), "expires": None, "proto": proto, "speed": None})
        known.add(name)
        changed = True
    if changed:
        st["users"].sort(key=lambda u: u["id"])
        save_state(st)


def fmt_left(sec):
    if sec <= 0:
        return tr("left_exp")
    d, h, m = int(sec // 86400), int(sec % 86400 // 3600), int(sec % 3600 // 60)
    if d:
        return tr("left_dh", d=d, h=h)
    if h:
        return tr("left_hm", h=h, m=m)
    return tr("left_m", m=max(m, 1))


def adoptable_clients(st):
    """Valid client certificates in the PKI that no panel user owns, e.g. clients made by
    openvpn-install.sh on the main port. They can be adopted by giving them their own instance."""
    names = []
    try:
        with open(f"{ED}/pki/index.txt") as f:
            for line in f:
                if line.startswith("V"):
                    m = re.search(r"/CN=([^/\s]+)", line)
                    if m:
                        names.append(m.group(1))
    except OSError:
        try:
            names = [fn[:-4] for fn in os.listdir(f"{ED}/pki/issued") if fn.endswith(".crt")]
        except OSError:
            return []
    try:
        server_crt = open(f"{SERVER_DIR}/server.crt").read().strip()
    except OSError:
        server_crt = None
    have = {u["name"] for u in st["users"]}
    out = []
    for n in sorted(set(names)):
        crt, key = f"{ED}/pki/issued/{n}.crt", f"{ED}/pki/private/{n}.key"
        if n in have or n.startswith("server") or not NAME_RE.match(n):
            continue
        if not (os.path.exists(crt) and os.path.exists(key)) or os.path.exists(f"{SERVER_DIR}/{n}.conf"):
            continue
        if server_crt and open(crt).read().strip() == server_crt:
            continue
        out.append(n)
    return out


# ====================== backup / import ======================
ROOT_FILES = ["ca.crt", "server.crt", "server.key", "crl.pem", "tc.key", "dh.pem"]
SUB_RE = re.compile(r"^10\.\d{1,3}\.\d{1,3}$")


def build_backup_bytes():
    """Zip with: panel state, usage totals, the whole PKI and the server's root cert files."""
    with LOCK:
        sync_existing()
        st = load_state()
    with SLOCK:
        usage = {u["name"]: dict(USAGE[u["name"]]) for u in st["users"] if u["name"] in USAGE}
    ca = ""
    try:
        ca = open(f"{ED}/pki/ca.crt").read().strip()
    except OSError:
        pass
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps({
            "app": APP_NAME, "format": 1, "created": time.time(), "users": len(st["users"]),
            "ca_sha256": hashlib.sha256(ca.encode()).hexdigest() if ca else ""}))
        z.writestr("panel/users.json", json.dumps(st, ensure_ascii=False))
        z.writestr("panel/usage.json", json.dumps({"users": usage}))
        pki = f"{ED}/pki"
        for dp, _dn, fns in os.walk(pki):
            for fn in fns:
                full = os.path.join(dp, fn)
                if os.path.islink(full):
                    continue
                z.write(full, os.path.relpath(full, ED))
        for fn in ROOT_FILES:
            if os.path.exists(f"{SERVER_DIR}/{fn}"):
                z.write(f"{SERVER_DIR}/{fn}", f"root/{fn}")
    return buf.getvalue()


def safe_member(name):
    return not (name.startswith("/") or ".." in name.split("/") or "\\" in name or "\x00" in name)


def extract_to(z, member, dest_root, rel):
    dest = os.path.normpath(os.path.join(dest_root, rel))
    if not dest.startswith(os.path.normpath(dest_root) + os.sep):
        raise ValueError("unsafe path")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "wb") as f:
        f.write(z.read(member))
    os.chmod(dest, 0o600 if "private" in rel or rel.endswith(".key") else 0o644)


def restore_pki_from_zip(z):
    """Replace the server PKI + root cert files with the backup's. Returns the safety copy path."""
    safety = f"{BACKUP_DIR}/before-import-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    with open(safety, "wb") as f:
        f.write(build_backup_bytes())
    os.chmod(safety, 0o600)
    shutil.rmtree(f"{ED}/pki", ignore_errors=True)
    for m in z.namelist():
        if m.endswith("/"):
            continue
        if m.startswith("pki/"):
            extract_to(z, m, ED, m)
        elif m.startswith("root/") and m[5:] in ROOT_FILES:
            extract_to(z, m, SERVER_DIR, m[5:])
    try:
        os.chmod(f"{ED}/pki/private", 0o700)
    except OSError:
        pass
    run(["systemctl", "restart", "openvpn-server@server"])
    return safety


def import_backup_zip(data, restore_pki):
    """Returns (kind, payload). kind: 'err' (payload = message) or 'ok' (payload = flash messages list)."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        infos = z.infolist()
        if len(infos) > 6000 or sum(i.file_size for i in infos) > 120 * 1024 * 1024:
            return "err", tr("f_b_bad")
        if not all(safe_member(i.filename) for i in infos):
            return "err", tr("f_b_bad")
        manifest = json.loads(z.read("manifest.json"))
        if manifest.get("app") != APP_NAME:
            return "err", tr("f_b_bad")
        if manifest.get("format") != 1:
            return "err", tr("f_b_ver")
        backup_users = json.loads(z.read("panel/users.json")).get("users", [])
        usage = json.loads(z.read("panel/usage.json")).get("users", {})
    except Exception:
        return "err", tr("f_b_bad")

    with LOCK:
        if not host_ready():
            return "err", tr("f_nohost")
        names = set(z.namelist())
        try:
            cur_ca = open(f"{ED}/pki/ca.crt", "rb").read().strip()
        except OSError:
            cur_ca = b""
        bak_ca = z.read("pki/ca.crt").strip() if "pki/ca.crt" in names else b""
        msgs = []
        if bak_ca and cur_ca != bak_ca:
            if not restore_pki:
                return "err", tr("f_b_ca")
            msgs.append(tr("f_b_safety", path=restore_pki_from_zip(z)))
        st = load_state()
        added = skipped = expired = 0
        failed = []
        for bu in sorted(backup_users, key=lambda x: x.get("id", 0)):
            try:
                name, proto = str(bu["name"]), bu.get("proto", "udp")
                if not NAME_RE.match(name) or proto not in ("udp", "tcp"):
                    raise ValueError("bad user")
                if any(u["name"] == name for u in st["users"]):
                    skipped += 1
                    continue
                exp = bu.get("expires")
                if exp and exp <= time.time():
                    expired += 1
                    continue
                for rel in (f"pki/issued/{name}.crt", f"pki/private/{name}.key", f"pki/reqs/{name}.req"):
                    if not os.path.exists(f"{ED}/{rel}") and rel in names:
                        extract_to(z, rel, ED, rel)
                if not (os.path.exists(f"{ED}/pki/issued/{name}.crt")
                        and os.path.exists(f"{ED}/pki/private/{name}.key")):
                    raise ValueError("certificate missing")
                used_ids = {u["id"] for u in st["users"]}
                idx = bu.get("id") if isinstance(bu.get("id"), int) and bu["id"] >= 1 and bu["id"] not in used_ids \
                    else free_id(st)
                sub = bu.get("subnet", "")
                if not (SUB_RE.match(sub) and idx == bu.get("id")
                        and sub not in {u["subnet"] for u in st["users"]} and sub != "10.8.0"):
                    sub = new_subnet(idx, st)
                port = bu.get("port")
                if not isinstance(port, int) or port_error(st, port, proto):
                    port = next_port(st)
                if port is None:
                    raise ValueError("no free port")
                ok, err = activate_instance(name, port, sub, proto, revoke_on_fail=False)
                if not ok:
                    raise ValueError(err[-120:])
                speed = bu.get("speed")
                user = {"name": name, "id": idx, "port": port, "subnet": sub, "proto": proto,
                        "created": bu.get("created", time.time()), "expires": exp,
                        "speed": speed if isinstance(speed, (int, float)) and 0 < speed <= 10000 else None}
                for lk in ("limit_daily", "limit_total"):
                    lv = bu.get(lk)
                    user[lk] = lv if isinstance(lv, (int, float)) and 0 < lv <= 1e6 else None
                tk, dpw = bu.get("token"), bu.get("dl_password")
                if isinstance(tk, str) and TOKEN_RE.match(tk) and not any(x.get("token") == tk for x in st["users"]):
                    user["token"] = tk
                if isinstance(dpw, str) and 8 <= len(dpw) <= 64:
                    user["dl_password"] = dpw
                st["users"].append(user)
                st["users"].sort(key=lambda u: u["id"])
                save_state(st)
                if user["speed"]:
                    apply_speed(user)
                rec = usage.get(name)
                if isinstance(rec, dict):
                    with SLOCK:
                        USAGE[name] = {"up": int(rec.get("up", 0)), "down": int(rec.get("down", 0)),
                                       "lu": 0, "ld": 0}
                added += 1
            except Exception as e:  # noqa
                failed.append(str(bu.get("name", "?"))[:32])
                print("import error:", bu.get("name"), e, flush=True)
        msgs.insert(0, tr("f_b_done", added=added, skipped=skipped, expired=expired, failed=len(failed)))
        if failed:
            msgs.append(tr("f_b_failed", names=", ".join(failed)))
    return "ok", msgs


# ====================== background: expiry ======================
def reaper():
    while True:
        time.sleep(60)
        try:
            with LOCK:
                st = load_state()
                keep = []
                for u in st["users"]:
                    if u.get("expires") and u["expires"] <= time.time():
                        remove_instance(u)
                    else:
                        keep.append(u)
                if len(keep) != len(st["users"]):
                    st["users"] = keep
                    save_state(st)
        except Exception as e:  # noqa
            print("reaper error:", e, flush=True)


def restore_rules():
    for u in load_state()["users"]:
        try:
            apply_rules(u["subnet"], u["port"], u.get("proto", "udp"))
        except Exception:
            pass


# ====================== per-user speed limit (tc) ======================
def find_dev(sub):
    """tun device of a user's instance = the interface that owns <subnet>.1"""
    out = run(["ip", "-o", "-4", "addr", "show"]).stdout
    m = re.search(rf"^\d+:\s+(\S+)\s+inet {re.escape(sub)}\.1[/ ]", out, re.M)
    return m.group(1) if m else None


def clear_limit(dev):
    run(["tc", "qdisc", "del", "dev", dev, "root"])
    run(["tc", "qdisc", "del", "dev", dev, "ingress"])


def set_limit(dev, mbit):
    """Download (server->user): tbf shaping on egress. Upload (user->server): ingress policing."""
    clear_limit(dev)
    rate = f"{mbit:g}mbit"
    burst = str(max(32768, int(mbit * 1e6 / 8 / 50)))
    r1 = run(["tc", "qdisc", "add", "dev", dev, "root", "tbf", "rate", rate, "burst", burst, "latency", "400ms"])
    r2 = run(["tc", "qdisc", "add", "dev", dev, "handle", "ffff:", "ingress"])
    r3 = run(["tc", "filter", "add", "dev", dev, "parent", "ffff:", "protocol", "all", "prio", "1", "u32",
              "match", "u32", "0", "0", "police", "rate", rate, "burst", burst, "drop", "flowid", ":1"])
    return r1.returncode == 0 and r2.returncode == 0 and r3.returncode == 0


def apply_speed(u):
    dev = find_dev(u["subnet"])
    if not dev:
        return False
    if u.get("speed"):
        return set_limit(dev, u["speed"])
    clear_limit(dev)
    return True


def enforce_limits():
    """tc rules vanish when an instance restarts / server reboots, so re-apply them."""
    for u in load_state()["users"]:
        if not u.get("speed"):
            continue
        dev = find_dev(u["subnet"])
        if dev and "tbf" not in run(["tc", "qdisc", "show", "dev", dev]).stdout:
            set_limit(dev, u["speed"])


# ====================== background: traffic sampling ======================
USAGE = {}      # name -> {"up","down","lu","ld"}   (bytes, since user creation)
RATES = {}      # name -> (up_bps, down_bps) in bytes/s
CONN = {}       # name -> {"online","last","ip","since"}
NET = {"iface": None, "last": None, "cur": (0, 0), "rate": (0.0, 0.0), "hist": deque(maxlen=180)}
NET_DAYS = {}   # "YYYY-MM-DD" -> [rx, tx]


def load_usage():
    try:
        with open(USAGE_FILE) as f:
            d = json.load(f)
    except Exception:
        d = {}
    USAGE.update(d.get("users", {}))
    CONN.update(d.get("conn", {}))
    for rec in CONN.values():
        rec["online"] = False
    NET_DAYS.update(d.get("net_days", {}))
    NET["last"] = d.get("net_last")


def save_usage():
    with SLOCK:
        data = {"users": USAGE, "conn": CONN, "net_days": NET_DAYS, "net_last": NET["last"]}
        tmp = USAGE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, USAGE_FILE)


def default_iface():
    try:  # /proc/net/route: default route has destination 00000000
        with open("/proc/net/route") as f:
            for line in f.readlines()[1:]:
                p = line.split()
                if len(p) > 1 and p[1] == "00000000":
                    return p[0]
    except OSError:
        pass
    m = re.search(r"dev (\S+)", run(["ip", "-o", "route", "show", "default"]).stdout)
    return m.group(1) if m else None


def read_net(iface):
    rx = tx = 0
    found = False
    try:
        with open("/proc/net/dev") as f:
            for line in f.readlines()[2:]:
                name, data = line.split(":", 1)
                name = name.strip()
                if iface:
                    if name != iface:
                        continue
                elif name == "lo" or name.startswith(("docker", "veth", "br-", "tun", "lxd", "virbr")):
                    continue
                p = data.split()
                rx += int(p[0])
                tx += int(p[8])
                found = True
    except OSError:
        return None
    return (rx, tx) if found else None


def net_tick(dt):
    cur = read_net(NET["iface"])
    if not cur:
        return
    with SLOCK:
        last = NET["last"]
        if last:
            drx = cur[0] - last[0] if cur[0] >= last[0] else cur[0]
            dtx = cur[1] - last[1] if cur[1] >= last[1] else cur[1]
        else:
            drx = dtx = 0
        NET["last"], NET["cur"] = list(cur), cur
        NET["rate"] = (drx / dt, dtx / dt)
        NET["hist"].append([round(time.time(), 1), drx / dt, dtx / dt])
        day = NET_DAYS.setdefault(time.strftime("%Y-%m-%d"), [0, 0])
        day[0] += drx
        day[1] += dtx
        if len(NET_DAYS) > 70:
            for k in sorted(NET_DAYS)[:-62]:
                del NET_DAYS[k]


SYS = {"last": None, "cpu": 0.0, "hist": deque(maxlen=180)}


def read_cpu_times():
    try:
        with open("/proc/stat") as f:
            p = [int(x) for x in f.readline().split()[1:9]]
        return sum(p), p[3] + p[4]          # total, idle (idle + iowait)
    except (OSError, ValueError, IndexError):
        return None


def read_meminfo():
    info = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                info[k] = int(v.split()[0]) * 1024
    except (OSError, ValueError):
        return None
    total = info.get("MemTotal", 0)
    avail = info.get("MemAvailable", info.get("MemFree", 0))
    swap_t, swap_f = info.get("SwapTotal", 0), info.get("SwapFree", 0)
    return {"total": total, "used": max(total - avail, 0), "avail": avail,
            "pct": round((total - avail) / total * 100, 1) if total else 0.0,
            "swap_total": swap_t, "swap_used": max(swap_t - swap_f, 0)}


def sys_tick():
    cur = read_cpu_times()
    mem = read_meminfo()
    if not cur or not mem:
        return
    with SLOCK:
        last = SYS["last"]
        if last and cur[0] > last[0]:
            SYS["cpu"] = round(max(0.0, min(100.0, 100.0 * (1 - (cur[1] - last[1]) / (cur[0] - last[0])))), 1)
        SYS["last"] = cur
        SYS["mem"] = mem
        SYS["hist"].append([round(time.time(), 1), SYS["cpu"], mem["pct"]])


def read_user_counters():
    out = {}
    r = run(["iptables", "-L", "FORWARD", "-n", "-v", "-x"])
    for line in r.stdout.splitlines()[2:]:
        p = line.split()
        if len(p) < 9:
            continue
        try:
            b = int(p[1])
        except ValueError:
            continue
        key = (p[7], p[8])
        out[key] = out.get(key, 0) + b
    return out


def mgmt_path(name):
    return f"{STATUS_DIR}/mgmt-{name}.sock"


def mgmt_line(name):
    return f"management {mgmt_path(name)} unix"


def mgmt_clients(name):
    """Connected clients read live from the instance's management socket.
    Returns None when the socket is not available (older instance / service stopped)."""
    path = mgmt_path(name)
    if not os.path.exists(path):
        return None
    sk = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    buf = b""
    try:
        sk.settimeout(2.0)
        sk.connect(path)
        sk.sendall(b"status 2\n")
        while b"\nEND" not in buf and len(buf) < 2_000_000:
            chunk = sk.recv(65536)
            if not chunk:
                break
            buf += chunk
        try:
            sk.sendall(b"quit\n")
        except OSError:
            pass
    except OSError:
        return None
    finally:
        sk.close()
    text = buf.decode("utf-8", "replace")
    if "\nEND" not in text:
        return None
    rows = []
    for line in text.splitlines():
        if line.startswith("CLIENT_LIST,"):
            p = line.split(",")
            try:
                rows.append({"ip": p[3], "rx": int(p[5]), "tx": int(p[6]), "since": int(p[8]),
                             "cid": p[10] if len(p) > 10 else "0"})
            except (IndexError, ValueError):
                continue
    return rows


def _add_usage(rec, du, dd, today):
    rec["up"] = rec.get("up", 0) + du
    rec["down"] = rec.get("down", 0) + dd
    if rec.get("d") != today:
        rec["d"], rec["du"], rec["dd"] = today, 0, 0
    rec["du"] = rec.get("du", 0) + du
    rec["dd"] = rec.get("dd", 0) + dd


def account_mgmt(u, rows, dt, now, today):
    """Usage + online state from OpenVPN's own per-session counters (exact, live)."""
    name = u["name"]
    rec = USAGE.get(name)
    if rec is None:
        rec = USAGE[name] = {"up": 0, "down": 0, "lu": 0, "ld": 0, "d": today, "du": 0, "dd": 0, "mt": now}
    c = CONN.setdefault(name, {})
    du = dd = 0
    r = rows[0] if rows else None
    if r:
        key = f"{r['cid']}:{r['since']}"
        if rec.get("sk") == key and r["rx"] >= rec.get("sr", 0) and r["tx"] >= rec.get("st", 0):
            du, dd = r["rx"] - rec["sr"], r["tx"] - rec["st"]
        elif rec.get("sk") != key and r["since"] >= rec.get("mt", now) - 5:
            du, dd = r["rx"], r["tx"]       # session began after our previous look: count all of it
        rec["sk"], rec["sr"], rec["st"] = key, r["rx"], r["tx"]
        if c.get("key") != key or c.get("rx") != r["rx"]:
            c["key"], c["rx"], c["rx_t"] = key, r["rx"], now   # client sent something -> still alive
        online = now - c.get("rx_t", now) < ACTIVE_WINDOW
        c.update(online=online, ip=r["ip"] or c.get("ip"), since=r["since"])
        if online:
            c["last"] = now
    else:
        rec["sk"] = None
        c.pop("key", None)
        c.pop("rx", None)
        c["online"] = False
        if not c.get("ip"):
            ip = lease_ip(name)
            if ip:
                c["ip"] = ip
    rec["mt"] = now
    _add_usage(rec, du, dd, today)
    RATES[name] = (du / dt, dd / dt) if r else (0.0, 0.0)


def account_legacy(u, counters, dt, now, today):
    """Fallback for instances without a management socket: iptables counters, counted only while the
    status file says somebody is connected (packets sent to nobody are not usage)."""
    name, sub = u["name"], u["subnet"]
    rows = parse_status(name)
    c = CONN.setdefault(name, {})
    if rows:
        c.update(online=True, last=now, ip=rows[0]["ip"] or c.get("ip"), since=rows[0]["since"] or c.get("since"))
    else:
        c["online"] = False
        if not c.get("ip"):
            ip = lease_ip(name)
            if ip:
                c["ip"] = ip
    cu = counters.get((f"{sub}.0/24", "0.0.0.0/0"))
    cd = counters.get(("0.0.0.0/0", f"{sub}.0/24"))
    if cu is None and cd is None:
        RATES[name] = (0.0, 0.0)
        return
    cu, cd = cu or 0, cd or 0
    rec = USAGE.get(name)
    if rec is None:
        USAGE[name] = {"up": cu, "down": cd, "lu": cu, "ld": cd, "d": today, "du": cu, "dd": cd}
        return
    du = cu - rec["lu"] if cu >= rec["lu"] else cu
    dd = cd - rec["ld"] if cd >= rec["ld"] else cd
    rec["lu"], rec["ld"] = cu, cd
    if not rows:
        du = dd = 0
    _add_usage(rec, du, dd, today)
    RATES[name] = (du / dt, dd / dt)


def users_tick(dt):
    users = load_state()["users"]
    now, today = time.time(), time.strftime("%Y-%m-%d")
    counters = None
    for u in users:
        rows = mgmt_clients(u["name"])
        if rows is not None:
            with SLOCK:
                account_mgmt(u, rows, dt, now, today)
        else:
            if counters is None:
                counters = read_user_counters()
            with SLOCK:
                account_legacy(u, counters, dt, now, today)


def migrate_management():
    """One-time: give instances created by older versions a management socket (needs one restart each)."""
    for u in load_state()["users"]:
        name = u["name"]
        path = f"{SERVER_DIR}/{name}.conf"
        try:
            with open(path) as f:
                conf = f.read()
        except OSError:
            continue
        if re.search(r"^management ", conf, re.M):
            continue
        with open(path, "w") as f:
            f.write(conf.rstrip("\n") + "\n" + mgmt_line(name) + "\n")
        if u.get("blocked") or not is_active(name):
            continue
        run(["systemctl", "restart", f"openvpn-server@{name}"])
        time.sleep(2)
        if not is_active(name):  # could not start with the new line: put the old config back
            with open(path, "w") as f:
                f.write(conf)
            run(["systemctl", "restart", f"openvpn-server@{name}"])
        time.sleep(1)


def daily_bytes(rec):
    if not rec or rec.get("d") != time.strftime("%Y-%m-%d"):
        return 0, 0
    return rec.get("du", 0), rec.get("dd", 0)


def limit_reason(u, rec):
    rec = rec or {}
    if u.get("limit_total") and rec.get("up", 0) + rec.get("down", 0) >= u["limit_total"] * GB:
        return "total"
    du, dd = daily_bytes(rec)
    if u.get("limit_daily") and du + dd >= u["limit_daily"] * GB:
        return "daily"
    return None


def enforce_blocks():
    """Stop users that reached a data limit; start them again when the limit is lifted / a new day starts."""
    with LOCK:
        st = load_state()
        changed = False
        for u in st["users"]:
            with SLOCK:
                rec = dict(USAGE.get(u["name"], {}))
            reason, cur = limit_reason(u, rec), u.get("blocked")
            if reason and cur != reason:
                u["blocked"], changed = reason, True
                run(["systemctl", "disable", "--now", f"openvpn-server@{u['name']}"])
            elif not reason and cur:
                u["blocked"], changed = None, True
                run(["systemctl", "enable", "--now", f"openvpn-server@{u['name']}"])
        if changed:
            save_state(st)


def reset_usage(u):
    c = read_user_counters()
    sub = u["subnet"]
    cu = c.get((f"{sub}.0/24", "0.0.0.0/0"), 0)
    cd = c.get(("0.0.0.0/0", f"{sub}.0/24"), 0)
    with SLOCK:
        old = USAGE.get(u["name"], {})
        USAGE[u["name"]] = {"up": 0, "down": 0, "lu": cu, "ld": cd, "d": time.strftime("%Y-%m-%d"), "du": 0, "dd": 0,
                            **{k: old[k] for k in ("sk", "sr", "st", "mt") if k in old}}


def parse_status(name):
    """Connected clients of one instance, from OpenVPN's status file (version 2, refreshed every ~60s)."""
    path = f"{STATUS_DIR}/status-{name}.log"
    try:
        if time.time() - os.stat(path).st_mtime > 180:
            return []
        rows = []
        with open(path) as f:
            for line in f:
                if line.startswith("CLIENT_LIST,"):
                    p = line.rstrip("\n").split(",")
                    rows.append({"ip": p[3], "since": int(p[8]) if len(p) > 8 and p[8].isdigit() else None})
        return rows
    except OSError:
        return []


def lease_ip(name):
    try:
        with open(f"{SERVER_DIR}/ipp-{name}.txt") as f:
            for line in f:
                p = line.strip().split(",")
                if len(p) > 1 and p[0] == name and p[1]:
                    return p[1]
    except OSError:
        pass
    return None


def sampler():
    tick, last_t, last_u, last_save = 0, time.time(), time.time(), time.time()
    while True:
        time.sleep(1)
        now = time.time()
        dt = max(now - last_t, 0.001)
        last_t = now
        try:
            if tick % 60 == 0:
                NET["iface"] = default_iface()
            if tick % 30 == 0:
                enforce_limits()
            net_tick(dt)
            sys_tick()
            if tick % 2 == 0:
                users_tick(max(now - last_u, 0.001))
                last_u = now
            if tick % 5 == 0:
                enforce_blocks()
            if now - last_save >= 30:
                save_usage()
                last_save = now
        except Exception as e:  # noqa
            print("sampler error:", e, flush=True)
        tick += 1


# ====================== upstream proxies (Advanced -> Proxy routing) ======================
# Users' TCP traffic is redirected (iptables REDIRECT) to a small built-in transparent relay, which forwards each
# connection through a SOCKS5 / SOCKS4 / HTTP-CONNECT proxy picked from the admin's list. UDP is not proxied.
import base64
import random
import select
import socketserver
import struct
from concurrent.futures import ThreadPoolExecutor

PROXY_FILE = f"{BASE}/proxy.json"
PROXY_LPORT = int(os.environ.get("PROXY_REDIR_PORT", "8091"))   # local port of the relay (never exposed: only VPN subnets may use it)
PROXY_MAX = 500
PLOCK = threading.RLock()
PCFG_DEFAULT = {"enabled": False, "list": [], "dtype": "socks5", "mode": "sticky", "rotate_min": 0,
                "fallback": False, "block_quic": True}
PSTATE = {}      # proxy key -> {"ok","ip","ms","checked","fails","fail_until"}
PSTICKY = {}     # user subnet -> (proxy key, picked at)
PSUBS = set()    # subnets of existing users (only these may use the relay)
PRR = [0]
PTEST = {"running": False, "done": 0, "total": 0}
PAPPLIED = {}    # (table, chain) -> rules last written
PLIST = []       # parsed proxies
PSRV = {"ok": False, "srv": None, "err": None}
PCLEAN = [False]   # leftovers of a previous run already removed
PCHAINS = (("nat", "GL_PROXY", "PREROUTING"), ("filter", "GL_PROXY_IN", "INPUT"), ("filter", "GL_PROXY_FW", "FORWARD"))


class _Refused(OSError):
    """The proxy answered but refused this destination (not a dead proxy)."""


_PROXY_RE = re.compile(r"^(?:(socks5h?|socks4a?|http)://)?(?:([^:@/\s]*):([^@/\s]*)@)?([A-Za-z0-9.\-]+):(\d{1,5})$", re.I)
_PTYPE = {"socks5": "socks5", "socks5h": "socks5", "socks4": "socks4", "socks4a": "socks4", "http": "http"}


def proxy_parse(line, dtype="socks5"):
    m = _PROXY_RE.match(line.strip())
    if not m:
        return None
    port = int(m.group(5))
    if not 1 <= port <= 65535:
        return None
    t = _PTYPE[m.group(1).lower()] if m.group(1) else dtype
    host = m.group(4)
    return {"type": t, "host": host, "port": port, "user": m.group(2) or "", "pw": m.group(3) or "",
            "key": f"{t}://{host}:{port}"}


def proxy_line(p):
    auth = f"{p['user']}:{p['pw']}@" if p["user"] else ""
    return f"{p['type']}://{auth}{p['host']}:{p['port']}"


def _pload():
    cfg = dict(PCFG_DEFAULT)
    try:
        with open(PROXY_FILE) as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def _psave():
    tmp = PROXY_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(PCFG, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, PROXY_FILE)


def _prebuild():
    global PLIST
    out, seen = [], set()
    for line in PCFG["list"]:
        p = proxy_parse(line, PCFG["dtype"])
        if p and p["key"] not in seen:
            seen.add(p["key"])
            out.append(p)
    PLIST = out
    _peff()


# ---- exit servers: your own SOCKS5 servers (user/password). While enabled they replace the free-proxy pool ----
import ipaddress

EXIT_FILE = f"{BASE}/exit_servers.json"
EX_DEFAULT = {"enabled": False, "mode": "sticky", "rotate_min": 0, "fallback": False, "block_quic": True,
              "next_id": 1, "servers": []}
EXTEST = {"running": False, "done": 0, "total": 0}
EXMAX = 50
PPOOL = []        # proxies the relay uses right now
PEFF = {"enabled": False, "mode": "sticky", "rotate_min": 0, "fallback": False, "block_quic": True}


def _exload():
    cfg = json.loads(json.dumps(EX_DEFAULT))
    try:
        with open(EXIT_FILE) as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def exit_proxy(s):
    return {"type": "socks5", "host": s["ip"], "port": int(s["port"]), "user": s["username"], "pw": s["password"],
            "key": f"exit:{s['id']}"}


def _exlist():
    return [exit_proxy(s) for s in EX["servers"] if s.get("is_active")]


def _peff():
    global PPOOL
    if EX["enabled"] and EXLIST:
        PEFF.update(enabled=True, mode=EX["mode"], rotate_min=EX["rotate_min"], fallback=EX["fallback"],
                    block_quic=EX["block_quic"])
        PPOOL = EXLIST
    else:
        PEFF.update(enabled=PCFG["enabled"], mode=PCFG["mode"], rotate_min=PCFG["rotate_min"],
                    fallback=PCFG["fallback"], block_quic=PCFG["block_quic"])
        PPOOL = PLIST


EX = _exload()
EXLIST = _exlist()


PCFG = _pload()
_prebuild()


def _recvn(s, n):
    buf = b""
    while len(buf) < n:
        c = s.recv(n - len(buf))
        if not c:
            raise OSError("closed")
        buf += c
    return buf


def proxy_handshake(s, p, dip, dport):
    ip4 = socket.inet_aton(dip)
    if p["type"] == "socks5":
        s.sendall(b"\x05\x02\x00\x02" if p["user"] else b"\x05\x01\x00")
        r = _recvn(s, 2)
        if r[0] != 5:
            raise OSError("bad socks5 reply")
        if r[1] == 2:
            u, w = p["user"].encode()[:255], p["pw"].encode()[:255]
            s.sendall(b"\x01" + bytes([len(u)]) + u + bytes([len(w)]) + w)
            if _recvn(s, 2)[1] != 0:
                raise OSError("socks5 auth failed")
        elif r[1] != 0:
            raise OSError("socks5 method rejected")
        s.sendall(b"\x05\x01\x00\x01" + ip4 + struct.pack("!H", dport))
        h = _recvn(s, 4)
        if h[1] != 0:
            raise _Refused("socks5 refused")
        n = {1: 4, 4: 16}.get(h[3])
        if n is None:
            n = _recvn(s, 1)[0]
        _recvn(s, n + 2)
    elif p["type"] == "socks4":
        s.sendall(b"\x04\x01" + struct.pack("!H", dport) + ip4 + p["user"].encode() + b"\x00")
        if _recvn(s, 8)[1] != 0x5A:
            raise _Refused("socks4 refused")
    else:
        req = f"CONNECT {dip}:{dport} HTTP/1.1\r\nHost: {dip}:{dport}\r\n"
        if p["user"]:
            req += "Proxy-Authorization: Basic " + base64.b64encode(f"{p['user']}:{p['pw']}".encode()).decode() + "\r\n"
        s.sendall((req + "\r\n").encode())
        buf = b""
        while not buf.endswith(b"\r\n\r\n"):      # byte by byte: never swallow data that follows the headers
            c = s.recv(1)
            if not c or len(buf) > 8192:
                raise OSError("bad http reply")
            buf += c
        if not re.match(rb"HTTP/1\.[01] 2\d\d", buf):
            raise _Refused("http connect refused")


def proxy_connect(p, dip, dport):
    s = socket.create_connection((p["host"], p["port"]), timeout=5)
    try:
        s.settimeout(7)
        proxy_handshake(s, p, dip, dport)
        s.settimeout(None)
        return s
    except Exception:
        s.close()
        raise


def proxy_mark(key, ok):
    with PLOCK:
        st = PSTATE.setdefault(key, {})
        if ok:
            st["fails"], st["fail_until"] = 0, 0
        else:
            st["fails"] = st.get("fails", 0) + 1
            st["fail_until"] = time.time() + min(60 * st["fails"], 600)


def proxy_pick(sub, exclude):
    now = time.time()
    with PLOCK:
        items = [p for p in PPOOL if p["key"] not in exclude]
        if not items:
            return None
        good = [p for p in items if PSTATE.get(p["key"], {}).get("fail_until", 0) <= now]
        pool = good or items
        mode = PEFF["mode"]
        if mode == "sticky":
            cur, rot = PSTICKY.get(sub), PEFF["rotate_min"] * 60
            if cur and (not rot or now - cur[1] < rot):
                for p in pool:
                    if p["key"] == cur[0]:
                        return p
            p = random.choice(pool)
            PSTICKY[sub] = (p["key"], now)
            return p
        if mode == "round":
            PRR[0] += 1
            return pool[PRR[0] % len(pool)]
        return random.choice(pool)


def _relay(a, b):
    live = {a: b, b: a}
    try:
        while live:
            r, _, _ = select.select(list(live), [], [], 300)
            if not r:
                break
            for s in r:
                try:
                    data = s.recv(65536)
                except OSError:
                    data = b""
                if data:
                    try:
                        live[s].sendall(data)
                    except OSError:
                        return
                else:                       # this side finished sending: pass the half-close on
                    dst = live.pop(s)
                    try:
                        dst.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
    except (OSError, ValueError):
        pass
    finally:
        a.close()
        b.close()


class _RedirHandler(socketserver.BaseRequestHandler):
    def handle(self):
        c = self.request
        sub = self.client_address[0].rsplit(".", 1)[0]
        if sub not in PSUBS:                # only VPN users' redirected connections are served
            return
        try:
            d = c.getsockopt(socket.SOL_IP, 80, 16)          # SO_ORIGINAL_DST
            dport, dip = struct.unpack("!H", d[2:4])[0], socket.inet_ntoa(d[4:8])
        except OSError:
            return
        up = None
        if PEFF["enabled"] and PPOOL:
            tried = set()
            for _ in range(4):
                p = proxy_pick(sub, tried)
                if not p:
                    break
                tried.add(p["key"])
                try:
                    up = proxy_connect(p, dip, dport)
                    proxy_mark(p["key"], True)
                    break
                except _Refused:
                    continue
                except OSError:
                    proxy_mark(p["key"], False)
        if up is None and (PEFF["fallback"] or not PEFF["enabled"]):
            try:
                up = socket.create_connection((dip, dport), timeout=8)
            except OSError:
                return
        if up is None:
            return
        _relay(c, up)


class _RedirServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 256


def proxy_test_one(p, dip):
    t0, ok, ip = time.time(), False, None
    try:
        if not dip:
            raise OSError("dns")
        s = proxy_connect(p, dip, 80)
        try:
            s.settimeout(8)
            s.sendall(b"GET / HTTP/1.0\r\nHost: api.ipify.org\r\nConnection: close\r\n\r\n")
            data = b""
            while len(data) < 4096:
                c = s.recv(1024)
                if not c:
                    break
                data += c
        finally:
            s.close()
        m = re.search(rb"\r\n\r\n\s*(\d{1,3}(?:\.\d{1,3}){3})\s*$", data)
        if m:
            ok, ip = True, m.group(1).decode()
    except Exception:  # noqa
        pass
    with PLOCK:
        st = PSTATE.setdefault(p["key"], {})
        st.update(ok=ok, ip=ip, ms=int((time.time() - t0) * 1000) if ok else None, checked=time.time())
        if ok:
            st["fails"], st["fail_until"] = 0, 0
        else:
            st["fails"] = st.get("fails", 0) + 1
            st["fail_until"] = time.time() + 300


def proxy_test_all():
    with PLOCK:
        if PTEST["running"]:
            return False
        items = list(PLIST)
        PTEST.update(running=True, done=0, total=len(items))

    def work():
        try:
            try:
                dip = socket.gethostbyname("api.ipify.org")
            except OSError:
                dip = None

            def one(p):
                try:
                    proxy_test_one(p, dip)
                finally:
                    with PLOCK:
                        PTEST["done"] += 1
            with ThreadPoolExecutor(max_workers=20) as ex:
                list(ex.map(one, items))
        finally:
            with PLOCK:
                PTEST["running"] = False
    threading.Thread(target=work, daemon=True).start()
    return True


def _ipt(table, *args):
    return run(["iptables", *(["-t", table] if table != "filter" else []), *args])


def _chain_apply(table, chain, parent, rules):
    """rules=None removes the chain; otherwise the chain holds exactly `rules` and is the first rule of `parent`."""
    if rules is None:
        if _ipt(table, "-S", chain).returncode == 0:
            while _ipt(table, "-D", parent, "-j", chain).returncode == 0:
                pass
            _ipt(table, "-F", chain)
            _ipt(table, "-X", chain)
        PAPPLIED.pop((table, chain), None)
        return
    if _ipt(table, "-S", chain).returncode != 0:
        _ipt(table, "-N", chain)
        PAPPLIED.pop((table, chain), None)
    if PAPPLIED.get((table, chain)) != rules:
        _ipt(table, "-F", chain)
        for r in rules:
            _ipt(table, "-A", chain, *r)
        PAPPLIED[(table, chain)] = rules
    first = [l for l in _ipt(table, "-S", parent).stdout.splitlines() if l.startswith("-A ")][:1]
    if first != [f"-A {parent} -j {chain}"]:     # must stay above rules the panel inserts later (e.g. FORWARD ACCEPT per user)
        while _ipt(table, "-D", parent, "-j", chain).returncode == 0:
            pass
        _ipt(table, "-I", parent, "1", "-j", chain)


def _relay_ensure(on):
    """The relay listens only while Proxy Routing / Exit servers is in use: nothing extra stays open otherwise."""
    with PLOCK:
        if on and PSRV["srv"] is None:
            try:
                srv = _RedirServer(("0.0.0.0", PROXY_LPORT), _RedirHandler)
            except OSError as e:
                PSRV["ok"] = False
                if PSRV["err"] != str(e):
                    print("proxy relay could not start:", e, flush=True)
                    PSRV["err"] = str(e)
                return
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            PSRV.update(srv=srv, ok=True, err=None)
        elif not on and PSRV["srv"] is not None:
            srv = PSRV["srv"]
            PSRV.update(srv=None, ok=False)
            threading.Thread(target=lambda: (srv.shutdown(), srv.server_close()), daemon=True).start()


def proxy_sync_rules():
    global PSUBS
    if not (PEFF["enabled"] and PPOOL):
        if not PCLEAN[0] or PAPPLIED:          # remove leftovers once, then stay completely idle
            for t, c, par in PCHAINS:
                _chain_apply(t, c, par, None)
            PCLEAN[0] = True
        _relay_ensure(False)
        return
    _relay_ensure(True)
    if not PSRV["ok"]:                         # relay could not start (port busy): never redirect users into nothing
        return
    PCLEAN[0] = False
    subs = sorted({u["subnet"] for u in load_state()["users"]})
    PSUBS = set(subs)
    lp = str(PROXY_LPORT)
    nat = [["-s", f"{s}.0/24", "!", "-d", f"{s}.0/24", "-p", "tcp", "-m", "addrtype", "!", "--dst-type", "LOCAL",
            "-j", "REDIRECT", "--to-ports", lp] for s in subs]
    inp = [["-s", f"{s}.0/24", "-p", "tcp", "--dport", lp, "-j", "ACCEPT"] for s in subs]
    fw = [["-s", f"{s}.0/24", "-p", "udp", "--dport", "443", "-j", "REJECT"] for s in subs] if PEFF["block_quic"] else []
    for (t, c, par), rules in zip(PCHAINS, (nat, inp, fw)):
        _chain_apply(t, c, par, rules)


def proxy_loop():
    while True:
        try:
            proxy_sync_rules()
        except Exception as e:  # noqa
            print("proxy sync error:", e, flush=True)
        time.sleep(15)


def proxy_start():
    threading.Thread(target=proxy_loop, daemon=True).start()
    threading.Thread(target=exit_health_loop, daemon=True).start()


def proxy_form():
    return {"enabled": PCFG["enabled"], "text": "\n".join(PCFG["list"]), "dtype": PCFG["dtype"], "mode": PCFG["mode"],
            "rotate_min": PCFG["rotate_min"], "fallback": PCFG["fallback"], "block_quic": PCFG["block_quic"],
            "relay_ok": PSRV["ok"] or not PEFF["enabled"], "relay_port": PROXY_LPORT}


def proxy_view():
    now = time.time()
    with PLOCK:
        rows = []
        for p in PLIST:
            st = PSTATE.get(p["key"], {})
            rows.append({"addr": f"{p['type']}://{p['host']}:{p['port']}" + (" (auth)" if p["user"] else ""),
                         "ok": st.get("ok"), "ip": st.get("ip"), "ms": st.get("ms"),
                         "cool": st.get("fail_until", 0) > now})
        return {"rows": rows, "test": dict(PTEST)}


# ====================== exit servers (Advanced -> Exit servers) ======================
_HOST_RE = re.compile(r"^[A-Za-z0-9.\-]{1,253}$")


def _exsave():
    tmp = EXIT_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(EX, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, EXIT_FILE)


def _exrebuild():
    global EXLIST
    EXLIST = _exlist()
    _peff()


def exit_validate(f, need_password=True):
    """-> (fields, error_key). Password may stay empty on edit (keeps the old one)."""
    ip = f.get("ip", "").strip()
    if not _HOST_RE.match(ip):
        return None, "f_ex_bad_ip"
    if re.fullmatch(r"[0-9.]+", ip):
        try:
            ipaddress.IPv4Address(ip)
        except ValueError:
            return None, "f_ex_bad_ip"
    try:
        port = int(f.get("port", "").strip())
    except ValueError:
        return None, "f_ex_bad_port"
    if not 1 <= port <= 65535:
        return None, "f_ex_bad_port"
    user, pw = f.get("username", "").strip(), f.get("password", "")
    if not user or len(user) > 255 or re.search(r"[\x00-\x1f]", user):
        return None, "f_ex_bad_user"
    if (need_password and not pw) or len(pw) > 255 or re.search(r"[\x00-\x1f]", pw):
        return None, "f_ex_bad_pw"
    return {"ip": ip, "port": port, "username": user, "password": pw}, None


def _exstate(s, now):
    st = PSTATE.get(f"exit:{s['id']}", {})
    if not s.get("is_active"):
        return "off"
    if st.get("ok") is False:
        return "bad"
    if st.get("fail_until", 0) > now:
        return "cool"
    return "ok" if st.get("ok") is True else "unk"


def exit_rows():
    now = time.time()
    with PLOCK:
        out = []
        for s in EX["servers"]:
            st = PSTATE.get(f"exit:{s['id']}", {})
            out.append({"id": s["id"], "ip": s["ip"], "port": s["port"], "username": s["username"],
                        "active": bool(s.get("is_active")), "state": _exstate(s, now),
                        "exit_ip": st.get("ip"), "ms": st.get("ms")})
        return out


def exit_form():
    return {"enabled": EX["enabled"], "mode": EX["mode"], "rotate_min": EX["rotate_min"], "fallback": EX["fallback"],
            "block_quic": EX["block_quic"], "servers": exit_rows()}


def exit_view():
    rows = [{"id": r["id"], "state": r["state"], "exit_ip": r["exit_ip"], "ms": r["ms"]} for r in exit_rows()]
    with PLOCK:
        return {"servers": rows, "test": dict(EXTEST)}


def exit_commit():
    """Save, rebuild the pool and re-apply firewall rules. Turns the feature off when no active server is left."""
    with PLOCK:
        auto = False
        if EX["enabled"] and not any(s.get("is_active") for s in EX["servers"]):
            EX["enabled"], auto = False, True
        _exsave()
        _exrebuild()
        keys = {f"exit:{s['id']}" for s in EX["servers"]}
        for k in [k for k in PSTATE if k.startswith("exit:") and k not in keys]:
            PSTATE.pop(k, None)
    proxy_sync_rules()
    return auto


def exit_test_all():
    with PLOCK:
        if EXTEST["running"]:
            return False
        items = [exit_proxy(s) for s in EX["servers"]]
        EXTEST.update(running=True, done=0, total=len(items))

    def work():
        try:
            try:
                dip = socket.gethostbyname("api.ipify.org")
            except OSError:
                dip = None

            def one(p):
                try:
                    proxy_test_one(p, dip)
                finally:
                    with PLOCK:
                        EXTEST["done"] += 1
            with ThreadPoolExecutor(max_workers=10) as ex:
                list(ex.map(one, items))
        finally:
            with PLOCK:
                EXTEST["running"] = False
    threading.Thread(target=work, daemon=True).start()
    return True


def exit_health_loop():
    """Every 30 s: check the active exit servers so a dead one is skipped quickly and a recovered one comes back."""
    while True:
        time.sleep(30)
        try:
            if EX["enabled"] and EXLIST and not EXTEST["running"]:
                dip = socket.gethostbyname("api.ipify.org")
                with ThreadPoolExecutor(max_workers=8) as ex:
                    list(ex.map(lambda p: proxy_test_one(p, dip), list(EXLIST)))
        except Exception:  # noqa
            pass


# ====================== address written into downloaded .ovpn files (Advanced) ======================
OVPN_ADDR_FILE = f"{BASE}/ovpn_addr.json"


def ovpn_addr():
    try:
        with open(OVPN_ADDR_FILE) as f:
            a = str(json.load(f).get("addr", "")).strip()
    except (OSError, ValueError, AttributeError):
        return ""
    return a if _HOST_RE.match(a) else ""


def ovpn_addr_valid(a):
    if not _HOST_RE.match(a):
        return False
    if re.fullmatch(r"[0-9.]+", a):
        try:
            ipaddress.IPv4Address(a)
        except ValueError:
            return False
    return True


def ovpn_bytes(path):
    """The .ovpn file as it is stored, with the host of its `remote` line replaced when an address is set
    (the port stays the user's own port). Done at download time, so existing users are covered and clearing
    the box brings the original address back."""
    with open(path, encoding="utf-8") as f:
        data = f.read()
    a = ovpn_addr()
    if a:
        data = re.sub(r"^remote (\S+) (\d+)", lambda m: f"remote {a} {m.group(2)}", data, count=1, flags=re.M)
    return data.encode("utf-8")


# ====================== auth ======================
DUMMY_HASH = generate_password_hash("not-a-real-password")


def save_auth(a):
    tmp = AUTH_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(a, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, AUTH_FILE)


def load_auth():
    if not os.path.exists(AUTH_FILE):  # first run: admin + PANEL_PASSWORD
        save_auth({"users": [{"username": "gasslight", "hash": generate_password_hash(PASSWORD)}]})
    with open(AUTH_FILE) as f:
        return json.load(f)


def current_admin():
    u = session.get("user")
    if session.get("ok") and u and any(a["username"] == u for a in load_auth()["users"]):
        return u
    return None


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not current_admin():
            if request.path.startswith("/api/"):
                return jsonify(error="auth"), 401
            return redirect(url_for("login"))
        return fn(*a, **kw)
    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login():
    ip = request.remote_addr
    cnt, until = FAILS.get(ip, (0, 0))
    if request.method == "POST":
        if until > time.time():
            flash(tr("err_many"), "err")
        else:
            uname = request.form.get("username", "").strip()
            adm = next((a for a in load_auth()["users"] if a["username"] == uname), None)
            good = check_password_hash(adm["hash"] if adm else DUMMY_HASH, request.form.get("password", ""))
            if adm and good:
                FAILS.pop(ip, None)
                session.clear()
                session["ok"], session["user"] = True, uname
                return redirect(url_for("index"))
            cnt += 1
            FAILS[ip] = (cnt, time.time() + 300 if cnt >= 5 else 0)
            flash(tr("err_login"), "err")
    return render_template_string(LOGIN_HTML, css=CSS, app=APP_NAME)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ====================== pages ======================
def page(body, **ctx):
    ctx.update(css=CSS, app=APP_NAME)
    return render_template_string(TOP + body + BOTTOM, **ctx)


@app.route("/")
@login_required
def index():
    with LOCK:
        sync_existing()
        st = load_state()
    users = st["users"]
    now = time.time()
    stats = {"total": len(users), "active": 0, "soon": 0, "unlimited": 0}
    for u in users:
        u["active"] = is_active(u["name"])
        stats["active"] += u["active"]
        rec = USAGE.get(u["name"], {})
        u["up"], u["down"] = rec.get("up", 0), rec.get("down", 0)
        exp = u.get("expires")
        if exp:
            left = exp - now
            total = max(exp - u.get("created", now), 1)
            u["left_txt"] = fmt_left(left)
            u["pct"] = max(0, min(100, int(left / total * 100)))
            u["level"] = "bad" if left < 86400 else ("warn" if left < 3 * 86400 else "ok")
            stats["soon"] += left < 3 * 86400
        else:
            u["left_txt"], u["pct"], u["level"] = None, 100, "ok"
            stats["unlimited"] += 1
    host = re.sub(r":\d+$", "", request.host)
    return page(DASH_HTML, active="dash", users=users, nid=free_id(st),
                portal_base=PORTAL_BASE_URL or f"http://{host}:{PORTAL_PORT}",
                port=next_port(st), stats=stats, ready=host_ready(), adopt=adoptable_clients(st))


@app.route("/network")
@login_required
def network():
    return page(NET_HTML, active="net")


@app.route("/usage")
@login_required
def usage():
    return page(USAGE_HTML, active="usage")


@app.route("/backup")
@login_required
def backup_page():
    with LOCK:
        sync_existing()
        n = len(load_state()["users"])
    return page(BACKUP_HTML, active="backup", count=n)


@app.route("/backup/download")
@login_required
def backup_download():
    data = build_backup_bytes()
    name = f"gasslight-backup-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    return send_file(io.BytesIO(data), mimetype="application/zip", as_attachment=True, download_name=name)


@app.route("/backup/import", methods=["POST"])
@login_required
def backup_import():
    f = request.files.get("file")
    if not f or not f.filename:
        flash(tr("f_b_nofile"), "err")
        return redirect(url_for("backup_page"))
    kind, payload = import_backup_zip(f.read(), request.form.get("restore_pki") == "1")
    if kind == "err":
        flash(payload, "err")
        return redirect(url_for("backup_page"))
    for i, m in enumerate(payload):
        flash(m, "ok" if i == 0 else "err")
    return redirect(url_for("index"))


@app.errorhandler(413)
def too_big(_e):
    flash(tr("f_b_big"), "err")
    return redirect(url_for("backup_page"))


@app.route("/create", methods=["POST"])
@login_required
def create():
    name = request.form.get("name", "").strip()
    manual = request.form.get("manual") == "1"
    proto = request.form.get("proto", "udp")
    try:
        days = int(request.form.get("days") or 0)
        speed = round(float(request.form.get("speed") or 0), 2)
        daily = round(float(request.form.get("daily") or 0), 3)
        total = round(float(request.form.get("total") or 0), 3)
        mport = int(request.form.get("port", "")) if manual else None
    except ValueError:
        flash(tr("f_num"), "err")
        return redirect(url_for("index"))

    with LOCK:
        st = load_state()
        port = mport if manual else next_port(st)
        err = None
        if not host_ready():
            err = tr("f_nohost")
        elif not NAME_RE.match(name):
            err = tr("f_name")
        elif proto not in ("udp", "tcp"):
            err = tr("f_proto")
        elif not 0 <= days <= 3650:
            err = tr("f_days")
        elif not 0 <= speed <= 10000:
            err = tr("f_speed")
        elif not (0 <= daily <= 1e6 and 0 <= total <= 1e6):
            err = tr("f_edit_lim")
        elif any(u["name"] == name for u in st["users"]):
            err = tr("f_exists")
        elif port is None:
            err = tr("f_noport")
        else:
            err = port_error(st, port, proto)
        if err:
            flash(err, "err")
            return redirect(url_for("index"))

        idx = free_id(st)
        sub = new_subnet(idx, st)
        ok, cerr = create_instance(name, port, sub, proto)
        if not ok:
            flash(tr("f_build", err=cerr), "err")
            return redirect(url_for("index"))
        now = time.time()
        user = {"name": name, "id": idx, "port": port, "subnet": sub, "created": now,
                "expires": now + days * 86400 if days else None, "proto": proto,
                "speed": speed or None, "limit_daily": daily or None,
                "limit_total": total or None, "blocked": None}
        st["users"].append(user)
        st["users"].sort(key=lambda u: u["id"])
        save_state(st)
        flash(tr("f_created", name=name, id=idx, port=port, proto=proto.upper())
              + (tr("f_created_exp", days=days) if days else ""), "ok")
        if speed and not apply_speed(user):
            flash(tr("f_tc_warn"), "err")
    return redirect(url_for("index"))


@app.route("/extend/<name>", methods=["POST"])
@login_required
def extend(name):
    try:
        days = int(request.form.get("days", ""))
    except ValueError:
        flash(tr("f_badday"), "err")
        return redirect(url_for("index"))
    with LOCK:
        st = load_state()
        u = next((u for u in st["users"] if u["name"] == name), None)
        if not u or not 1 <= days <= 3650:
            flash(tr("f_bad"), "err")
        else:
            u["expires"] = max(u.get("expires") or 0, time.time()) + days * 86400
            save_state(st)
            flash(tr("f_extended", days=days, name=name), "ok")
    return redirect(url_for("index"))


@app.route("/unlimit/<name>", methods=["POST"])
@login_required
def unlimit(name):
    with LOCK:
        st = load_state()
        for u in st["users"]:
            if u["name"] == name:
                u["expires"] = None
        save_state(st)
    flash(tr("f_unlimited", name=name), "ok")
    return redirect(url_for("index"))


@app.route("/speed/<name>", methods=["POST"])
@login_required
def set_speed(name):
    try:
        v = round(float(request.form.get("speed", "")), 2)
    except ValueError:
        v = -1
    if not 0 <= v <= 10000:
        flash(tr("f_speed"), "err")
        return redirect(url_for("index"))
    with LOCK:
        st = load_state()
        u = next((u for u in st["users"] if u["name"] == name), None)
        if not u:
            flash(tr("f_bad"), "err")
            return redirect(url_for("index"))
        u["speed"] = v or None
        save_state(st)
        applied = apply_speed(u)
    if v:
        flash(tr("f_speed_set", name=name, speed=f"{v:g}"), "ok")
        if not applied:
            flash(tr("f_tc_warn"), "err")
    else:
        flash(tr("f_speed_off", name=name), "ok")
    return redirect(url_for("index"))


@app.route("/edit/<name>", methods=["POST"])
@login_required
def edit_user(name):
    f = request.form
    proto = f.get("proto", "udp")
    try:
        new_id, new_port = int(f.get("id", "")), int(f.get("port", ""))
        speed = round(float(f.get("speed") or 0), 2)
        daily = round(float(f.get("daily") or 0), 3)
        total = round(float(f.get("total") or 0), 3)
    except ValueError:
        flash(tr("f_num"), "err")
        return redirect(url_for("index"))
    with LOCK:
        st = load_state()
        u = next((x for x in st["users"] if x["name"] == name), None)
        if not u:
            flash(tr("f_bad"), "err")
            return redirect(url_for("index"))
        others = {"users": [x for x in st["users"] if x["name"] != name]}
        old_proto = u.get("proto", "udp")
        err = None
        if proto not in ("udp", "tcp"):
            err = tr("f_proto")
        elif new_id < 1 or any(x["id"] == new_id for x in others["users"]):
            err = tr("f_edit_id")
        elif not 0 <= speed <= 10000:
            err = tr("f_speed")
        elif not (0 <= daily <= 1e6 and 0 <= total <= 1e6):
            err = tr("f_edit_lim")
        elif (new_port, proto) != (u["port"], old_proto):
            err = port_error(others, new_port, proto)
        if err:
            flash(err, "err")
            return redirect(url_for("index"))
        if (new_id, new_port, proto) != (u["id"], u["port"], old_proto):
            ok, e = edit_instance(u, new_id, new_port, proto)
            if not ok:
                flash(tr("f_edit_fail", err=e), "err")
                return redirect(url_for("index"))
            if u.get("blocked"):
                run(["systemctl", "disable", "--now", f"openvpn-server@{name}"])
        u["speed"], u["limit_daily"], u["limit_total"] = speed or None, daily or None, total or None
        st["users"].sort(key=lambda x: x["id"])
        save_state(st)
        apply_speed(u)
    enforce_blocks()
    flash(tr("f_edit_ok", name=name), "ok")
    return redirect(url_for("index"))


@app.route("/reset/<name>", methods=["POST"])
@login_required
def reset_user_usage(name):
    with LOCK:
        u = next((x for x in load_state()["users"] if x["name"] == name), None)
        if not u:
            flash(tr("f_bad"), "err")
            return redirect(url_for("index"))
        reset_usage(u)
    enforce_blocks()
    flash(tr("f_reset_ok", name=name), "ok")
    return redirect(url_for("index"))


ADV_SUBPAGES = {"account": "adv_pg_account", "proxy": "adv_pg_proxy", "exit": "adv_pg_exit"}


@app.route("/advanced")
@login_required
def advanced_page():
    # the POST handlers below still redirect here: send the person back to the sub page they were just on
    return redirect(url_for(ADV_SUBPAGES.get(session.get("adv_last"), "adv_pg_account")))


@app.route("/advanced/my-account")
@login_required
def adv_pg_account():
    session["adv_last"] = "account"
    return page(ADV_ACC_HTML, active="adv_acc", admins=[a["username"] for a in load_auth()["users"]])


@app.route("/advanced/proxy-routing")
@login_required
def adv_pg_proxy():
    session["adv_last"] = "proxy"
    return page(ADV_PX_HTML, active="adv_px", px=proxy_form())


@app.route("/advanced/exit-servers")
@login_required
def adv_pg_exit():
    session["adv_last"] = "exit"
    return page(ADV_EX_HTML, active="adv_ex", ex=exit_form(), oa={"addr": ovpn_addr()})


@app.route("/advanced/account", methods=["POST"])
@login_required
def adv_account():
    f = request.form
    new_user, pw, pw2 = f.get("username", "").strip(), f.get("password", ""), f.get("password2", "")
    with LOCK:
        a = load_auth()
        adm = next(x for x in a["users"] if x["username"] == session["user"])
        if not check_password_hash(adm["hash"], f.get("current", "")):
            flash(tr("f_acc_pw"), "err")
        elif not USER_RE.match(new_user):
            flash(tr("f_acc_user"), "err")
        elif new_user != adm["username"] and any(x["username"] == new_user for x in a["users"]):
            flash(tr("f_acc_exists"), "err")
        elif pw and len(pw) < 6:
            flash(tr("f_acc_short"), "err")
        elif pw and pw != pw2:
            flash(tr("f_acc_mismatch"), "err")
        else:
            adm["username"] = new_user
            if pw:
                adm["hash"] = generate_password_hash(pw)
            save_auth(a)
            session["user"] = new_user
            flash(tr("f_acc_saved"), "ok")
    return redirect(url_for("advanced_page"))


@app.route("/advanced/users/add", methods=["POST"])
@login_required
def adv_user_add():
    uname, pw = request.form.get("username", "").strip(), request.form.get("password", "")
    with LOCK:
        a = load_auth()
        if not USER_RE.match(uname):
            flash(tr("f_acc_user"), "err")
        elif any(x["username"] == uname for x in a["users"]):
            flash(tr("f_acc_exists"), "err")
        elif len(pw) < 6:
            flash(tr("f_acc_short"), "err")
        else:
            a["users"].append({"username": uname, "hash": generate_password_hash(pw)})
            save_auth(a)
            flash(tr("f_adm_added", name=uname), "ok")
    return redirect(url_for("advanced_page"))


@app.route("/advanced/users/delete/<uname>", methods=["POST"])
@login_required
def adv_user_delete(uname):
    with LOCK:
        a = load_auth()
        if uname == session["user"]:
            flash(tr("f_adm_self"), "err")
        elif len(a["users"]) <= 1 or not any(x["username"] == uname for x in a["users"]):
            flash(tr("f_adm_last"), "err")
        else:
            a["users"] = [x for x in a["users"] if x["username"] != uname]
            save_auth(a)
            flash(tr("f_adm_deleted", name=uname), "ok")
    return redirect(url_for("advanced_page"))


@app.route("/advanced/proxy/save", methods=["POST"])
@login_required
def adv_proxy_save():
    f = request.form
    dtype = f.get("dtype", "socks5")
    dtype = dtype if dtype in ("socks5", "socks4", "http") else "socks5"
    mode = f.get("mode", "sticky")
    mode = mode if mode in ("sticky", "random", "round") else "sticky"
    try:
        rot = max(0, min(int(f.get("rotate_min", "0") or 0), 1440))
    except ValueError:
        rot = 0
    lines, seen, bad = [], set(), 0
    for raw in f.get("list", "").splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        p = proxy_parse(raw, dtype)
        if not p:
            bad += 1
        elif p["key"] not in seen:
            seen.add(p["key"])
            lines.append(proxy_line(p))
    enabled = f.get("enabled") == "1"
    if len(lines) > PROXY_MAX:
        flash(tr("f_px_many", max=PROXY_MAX), "err")
    elif enabled and not lines:
        flash(tr("f_px_empty"), "err")
    else:
        with PLOCK:
            PCFG.update(enabled=enabled, list=lines, dtype=dtype, mode=mode, rotate_min=rot,
                        fallback=f.get("fallback") == "1", block_quic=f.get("block_quic") == "1")
            _psave()
            _prebuild()
            keys = {p["key"] for p in PLIST}
            for k in [k for k in PSTATE if k not in keys and not k.startswith("exit:")]:
                PSTATE.pop(k, None)
            for s in [s for s, v in PSTICKY.items() if v[0] not in keys and not v[0].startswith("exit:")]:
                PSTICKY.pop(s, None)
        proxy_sync_rules()
        flash(tr("f_px_saved", n=len(PLIST)) + (tr("f_px_bad", bad=bad) if bad else ""), "ok")
    return redirect(url_for("advanced_page"))


@app.route("/advanced/proxy/test", methods=["POST"])
@login_required
def adv_proxy_test():
    flash(tr("f_px_testing") if proxy_test_all() else tr("f_px_busy"), "ok")
    return redirect(url_for("advanced_page"))


@app.route("/advanced/proxy/prune", methods=["POST"])
@login_required
def adv_proxy_prune():
    with PLOCK:
        dead = {p["key"] for p in PLIST if PSTATE.get(p["key"], {}).get("ok") is False}
        if dead:
            PCFG["list"] = [l for l in PCFG["list"]
                            if (proxy_parse(l, PCFG["dtype"]) or {}).get("key") not in dead]
            _psave()
            _prebuild()
            for k in dead:
                PSTATE.pop(k, None)
            for s in [s for s, v in PSTICKY.items() if v[0] in dead]:
                PSTICKY.pop(s, None)
    if dead:
        proxy_sync_rules()
    flash(tr("f_px_pruned", n=len(dead)), "ok")
    return redirect(url_for("advanced_page"))


@app.route("/api/proxy")
@login_required
def api_proxy():
    return jsonify(proxy_view())


def _ex_find(sid):
    return next((s for s in EX["servers"] if s["id"] == sid), None)


@app.route("/advanced/exit/settings", methods=["POST"])
@login_required
def adv_exit_settings():
    f = request.form
    mode = f.get("mode", "sticky")
    mode = mode if mode in ("sticky", "random", "round") else "sticky"
    try:
        rot = max(0, min(int(f.get("rotate_min", "0") or 0), 1440))
    except ValueError:
        rot = 0
    enabled = f.get("enabled") == "1"
    if enabled and not any(s.get("is_active") for s in EX["servers"]):
        flash(tr("f_ex_noactive"), "err")
    else:
        with PLOCK:
            EX.update(enabled=enabled, mode=mode, rotate_min=rot, fallback=f.get("fallback") == "1",
                      block_quic=f.get("block_quic") == "1")
        exit_commit()
        flash(tr("f_ex_saved"), "ok")
    return redirect(url_for("advanced_page"))


@app.route("/advanced/exit/add", methods=["POST"])
@login_required
def adv_exit_add():
    fields, err = exit_validate(request.form)
    if err:
        flash(tr(err), "err")
    elif len(EX["servers"]) >= EXMAX:
        flash(tr("f_ex_many", max=EXMAX), "err")
    else:
        with PLOCK:
            EX["servers"].append({"id": EX["next_id"], **fields, "is_active": True})
            EX["next_id"] += 1
        exit_commit()
        flash(tr("f_ex_added"), "ok")
    return redirect(url_for("advanced_page"))


@app.route("/advanced/exit/<int:sid>/update", methods=["POST"])
@login_required
def adv_exit_update(sid):
    s = _ex_find(sid)
    if not s:
        flash(tr("f_ex_missing"), "err")
        return redirect(url_for("advanced_page"))
    fields, err = exit_validate(request.form, need_password=False)
    if err:
        flash(tr(err), "err")
    else:
        with PLOCK:
            if not fields["password"]:
                fields["password"] = s["password"]
            s.update(fields)
            PSTATE.pop(f"exit:{sid}", None)
        exit_commit()
        flash(tr("f_ex_updated"), "ok")
    return redirect(url_for("advanced_page"))


@app.route("/advanced/exit/<int:sid>/toggle", methods=["POST"])
@login_required
def adv_exit_toggle(sid):
    s = _ex_find(sid)
    if not s:
        flash(tr("f_ex_missing"), "err")
    else:
        with PLOCK:
            s["is_active"] = not s.get("is_active")
        auto = exit_commit()
        flash(tr("f_ex_autooff") if auto else tr("f_ex_toggled"), "err" if auto else "ok")
    return redirect(url_for("advanced_page"))


@app.route("/advanced/exit/<int:sid>/delete", methods=["POST"])
@login_required
def adv_exit_delete(sid):
    s = _ex_find(sid)
    if not s:
        flash(tr("f_ex_missing"), "err")
    else:
        with PLOCK:
            EX["servers"].remove(s)
        auto = exit_commit()
        flash(tr("f_ex_autooff") if auto else tr("f_ex_deleted"), "err" if auto else "ok")
    return redirect(url_for("advanced_page"))


@app.route("/advanced/exit/test", methods=["POST"])
@login_required
def adv_exit_test():
    flash(tr("f_px_testing") if exit_test_all() else tr("f_px_busy"), "ok")
    return redirect(url_for("advanced_page"))


@app.route("/api/exit")
@login_required
def api_exit():
    return jsonify(exit_view())


@app.route("/advanced/ovpn-addr", methods=["POST"])
@login_required
def adv_ovpn_addr():
    a = request.form.get("addr", "").strip()
    if a and not ovpn_addr_valid(a):
        flash(tr("f_oa_bad"), "err")
    else:
        tmp = OVPN_ADDR_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"addr": a}, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, OVPN_ADDR_FILE)
        flash(tr("f_oa_saved") if a else tr("f_oa_cleared"), "ok")
    return redirect(url_for("advanced_page"))


@app.route("/adopt/<name>", methods=["POST"])
@login_required
def adopt(name):
    proto = request.form.get("proto", "udp")
    with LOCK:
        st = load_state()
        if proto not in ("udp", "tcp") or not host_ready() or name not in adoptable_clients(st):
            flash(tr("f_adopt_bad"), "err")
            return redirect(url_for("index"))
        port = next_port(st)
        if port is None:
            flash(tr("f_noport"), "err")
            return redirect(url_for("index"))
        idx = free_id(st)
        sub = new_subnet(idx, st)
        ok, err = activate_instance(name, port, sub, proto, revoke_on_fail=False)
        if not ok:
            flash(tr("f_build", err=err), "err")
            return redirect(url_for("index"))
        st["users"].append({"name": name, "id": idx, "port": port, "subnet": sub, "proto": proto,
                            "created": time.time(), "expires": None, "speed": None,
                            "limit_daily": None, "limit_total": None, "blocked": None})
        st["users"].sort(key=lambda u: u["id"])
        save_state(st)
        flash(tr("f_adopt_ok", name=name, id=idx, port=port, proto=proto.upper()), "ok")
    return redirect(url_for("index"))


def _regen(name, field, gen, msg):
    with LOCK:
        st = load_state()
        u = next((x for x in st["users"] if x["name"] == name), None)
        if not u:
            flash(tr("f_bad"), "err")
        else:
            u[field] = gen()
            save_state(st)
            flash(tr(msg, name=name), "ok")
    return redirect(url_for("index"))


@app.route("/portal/regen_pw/<name>", methods=["POST"])
@login_required
def portal_regen_pw(name):
    return _regen(name, "dl_password", gen_dl_password, "f_pw_regen")


@app.route("/portal/regen_link/<name>", methods=["POST"])
@login_required
def portal_regen_link(name):
    return _regen(name, "token", new_token, "f_link_regen")


# ----- internal API for portal.py (localhost only + shared secret; anything else gets a plain 404) -----
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
PW_FAILS = {}   # ("t", token) / ("i", client ip) -> (failures, locked_until)


def portal_guard():
    if request.remote_addr not in ("127.0.0.1", "::1") or \
            not hmac.compare_digest(request.headers.get("X-Portal-Secret", ""), PORTAL_SECRET):
        abort(404)


def portal_user(token):
    if not TOKEN_RE.match(token):
        return None
    return next((u for u in load_state()["users"] if hmac.compare_digest(u["token"], token)), None)


def _pw_wait(key):
    c, until = PW_FAILS.get(key, (0, 0))
    return max(0, int(until - time.time()))


def _pw_fail(key, limit, lock):
    c, until = PW_FAILS.get(key, (0, 0))
    if until and until <= time.time():
        c = 0
    c += 1
    PW_FAILS[key] = (c, time.time() + lock if c >= limit else 0)
    if len(PW_FAILS) > 5000:
        for k in [k for k, v in PW_FAILS.items() if v[1] <= time.time()][:2500]:
            PW_FAILS.pop(k, None)
    return c


@app.route("/internal/portal/<token>")
def portal_view(token):
    portal_guard()
    u = portal_user(token)
    if not u:
        return jsonify(error="not_found"), 404
    with SLOCK:
        rec, cn = dict(USAGE.get(u["name"], {})), dict(CONN.get(u["name"], {}))
    du, dd = daily_bytes(rec)
    server = ""
    try:
        with open(f"{SERVER_DIR}/client-common.txt") as f:
            m = re.search(r"^remote (\S+) \d+", f.read(), re.M)
        server = m.group(1) if m else ""
    except OSError:
        pass
    server = ovpn_addr() or server
    return jsonify(name=u["name"], proto=u.get("proto", "udp"), port=u["port"], server=server,
                   speed=u.get("speed"), created=u["created"], expires=u.get("expires"), now=time.time(),
                   blocked=u.get("blocked"), online=bool(cn.get("online")), ip=cn.get("ip"),
                   last=cn.get("last"), since=cn.get("since"), up=rec.get("up", 0), down=rec.get("down", 0),
                   day_up=du, day_down=dd, limit_daily=u.get("limit_daily"), limit_total=u.get("limit_total"))


@app.route("/internal/portal/<token>/ovpn", methods=["POST"])
def portal_ovpn(token):
    portal_guard()
    u = portal_user(token)
    if not u:
        return jsonify(error="not_found"), 404
    tkey, ikey = ("t", token), ("i", request.headers.get("X-Client-IP", "-")[:64])
    wait = max(_pw_wait(tkey), _pw_wait(ikey))
    if wait:
        return jsonify(error="locked", wait=wait), 429
    if not hmac.compare_digest(request.form.get("password", "").encode(), u["dl_password"].encode()):
        c = _pw_fail(tkey, 5, 300)
        _pw_fail(ikey, 12, 600)
        return jsonify(error="bad_password", left=max(0, 5 - c)), 403
    PW_FAILS.pop(tkey, None)
    path = f"{OVPN_DIR}/{u['name']}.ovpn"
    if not os.path.exists(path):
        try:
            build_ovpn(u["name"], u["port"], path, u.get("proto", "udp"))
        except Exception:
            return jsonify(error="no_file"), 404
    resp = send_file(io.BytesIO(ovpn_bytes(path)), mimetype="application/x-openvpn-profile", as_attachment=True,
                     download_name=f"{u['name']}.ovpn")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/delete/<name>", methods=["POST"])
@login_required
def delete(name):
    with LOCK:
        st = load_state()
        u = next((u for u in st["users"] if u["name"] == name), None)
        if not u:
            abort(404)
        remove_instance(u)
        st["users"] = [x for x in st["users"] if x["name"] != name]
        save_state(st)
    flash(tr("f_deleted", name=name), "ok")
    return redirect(url_for("index"))


@app.route("/download/<name>")
@login_required
def download(name):
    u = next((u for u in load_state()["users"] if u["name"] == name), None)
    if not u:
        abort(404)
    path = f"{OVPN_DIR}/{name}.ovpn"
    if not os.path.exists(path):
        try:
            build_ovpn(name, u["port"], path, u.get("proto", "udp"))
        except Exception:
            abort(404)
    return send_file(io.BytesIO(ovpn_bytes(path)), as_attachment=True, download_name=f"{name}.ovpn")


# ====================== API ======================
@app.route("/api/port_check")
@login_required
def api_port_check():
    try:
        port = int(request.args.get("port", ""))
    except ValueError:
        return jsonify(ok=False, msg=tr("p_bad"))
    proto = request.args.get("proto", "udp")
    err = port_error(load_state(), port, proto if proto in ("udp", "tcp") else "udp")
    return jsonify(ok=err is None, msg=err or tr("p_free"))


@app.route("/api/net")
@login_required
def api_net():
    with SLOCK:
        day = time.strftime("%Y-%m-%d")
        month = time.strftime("%Y-%m")
        today = NET_DAYS.get(day, [0, 0])
        mon = [sum(v[0] for k, v in NET_DAYS.items() if k.startswith(month)),
               sum(v[1] for k, v in NET_DAYS.items() if k.startswith(month))]
        return jsonify(iface=NET["iface"] or "all", rx=NET["rate"][0], tx=NET["rate"][1],
                       total_rx=NET["cur"][0], total_tx=NET["cur"][1],
                       today=today, month=mon, hist=list(NET["hist"]),
                       sys={"cpu": SYS["cpu"], "cores": os.cpu_count() or 1, "load": list(os.getloadavg()),
                            "mem": SYS.get("mem") or {}, "hist": list(SYS["hist"])})


@app.route("/api/usage")
@login_required
def api_usage():
    users = load_state()["users"]
    out = []
    with SLOCK:
        for u in users:
            rec = USAGE.get(u["name"], {})
            cn = CONN.get(u["name"], {})
            ru, rd = RATES.get(u["name"], (0, 0))
            du, dd = daily_bytes(rec)
            out.append({"name": u["name"], "id": u["id"], "port": u["port"], "proto": u.get("proto", "udp"),
                        "created": u["created"], "expires": u.get("expires"),
                        "up": rec.get("up", 0), "down": rec.get("down", 0),
                        "day_up": du, "day_down": dd,
                        "limit_daily": u.get("limit_daily"), "limit_total": u.get("limit_total"),
                        "blocked": u.get("blocked"),
                        "ip": cn.get("ip"), "online": bool(cn.get("online")),
                        "last": cn.get("last"), "since": cn.get("since"),
                        "rate_up": ru, "rate_down": rd})
    return jsonify(users=out, now=time.time())


# ====================== UI ======================
CSS = """
<style>
:root{--bg:#0a0e1f;--card:rgba(255,255,255,.055);--bd:rgba(255,255,255,.1);--tx:#eaeefb;--mut:#8f9bbd;
--p1:#6d7cff;--p2:#a45cff;--ok:#34d399;--warn:#fbbf24;--bad:#fb7185;--inp:rgba(255,255,255,.06);
--nav:rgba(10,14,31,.8);--navs:#0e1330;--dn:#22d3ee;--upc:#a45cff;--side:252px}
@media (prefers-color-scheme:light){:root{--bg:#eef1fb;--card:rgba(255,255,255,.78);--bd:rgba(20,30,80,.1);
--tx:#1a2140;--mut:#64709a;--inp:rgba(20,30,80,.05);--nav:rgba(244,246,253,.85);--navs:#f4f6fd}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;min-height:100vh;color:var(--tx);background:var(--bg);overflow-x:hidden;
font-family:Inter,Vazirmatn,"Segoe UI",Tahoma,sans-serif;line-height:1.6;
background-image:radial-gradient(600px 400px at 85% -5%,rgba(109,124,255,.35),transparent),
radial-gradient(500px 400px at 5% 10%,rgba(164,92,255,.25),transparent);background-attachment:fixed}
a{color:inherit}
.ic{flex:none;vertical-align:-3px}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;direction:ltr;unicode-bidi:isolate}
.grow{flex:1;min-width:0}
/* ---------- brand / logo ---------- */
.brand{display:flex;align-items:center;gap:10px;font-size:19px;font-weight:800;color:var(--tx);text-decoration:none}
.brand em{font-style:normal;background:linear-gradient(135deg,var(--p1),var(--p2));-webkit-background-clip:text;
background-clip:text;color:transparent}
.logo{width:38px;height:38px;border-radius:12px;display:grid;place-items:center;flex:none;color:#fff;
background:linear-gradient(135deg,var(--p1),var(--p2));box-shadow:0 8px 24px rgba(109,124,255,.4)}
/* ---------- sidebar (desktop) / dropdown (mobile) ---------- */
.side{position:fixed;top:0;right:0;bottom:0;width:var(--side);display:flex;flex-direction:column;gap:6px;
padding:18px 14px;background:var(--nav);border-left:1px solid var(--bd);z-index:30;overflow-y:auto;
backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px)}
.side-brand{margin:2px 8px 18px}
.links{display:flex;flex-direction:column;gap:4px}
.links a{display:flex;align-items:center;gap:12px;padding:11px 14px;border-radius:12px;color:var(--mut);
text-decoration:none;font-size:14px;font-weight:500;transition:.2s}
.links a:hover{color:var(--tx);background:var(--inp)}
.links a.on{color:#fff;background:linear-gradient(135deg,var(--p1),var(--p2));box-shadow:0 6px 18px rgba(109,124,255,.35)}
/* ---------- Advanced sub-menu ---------- */
.grp{display:flex;flex-direction:column}
.gbtn{display:flex;align-items:center;gap:12px;width:100%;padding:11px 14px;border-radius:12px;border:0;background:none;
color:var(--mut);font:inherit;font-size:14px;font-weight:500;cursor:pointer;text-align:start;transition:.2s}
.gbtn:hover,.gbtn.on,.grp.open .gbtn{color:var(--tx);background:var(--inp)}
.gbtn .chev{margin-inline-start:auto;transition:transform .35s cubic-bezier(.4,0,.2,1)}
.grp.open .chev{transform:rotate(180deg)}
.smenu{display:grid;grid-template-rows:0fr;transition:grid-template-rows .38s cubic-bezier(.4,0,.2,1)}
.grp.open .smenu{grid-template-rows:1fr}
.smenu>div{min-height:0;overflow:hidden;display:flex;flex-direction:column;gap:4px;visibility:hidden;
margin-inline-start:22px;padding-inline-start:10px;border-inline-start:2px solid var(--bd);
transition:visibility 0s .38s}
.grp.open .smenu>div{visibility:visible;padding-top:6px;transition-delay:0s}
.links .smenu a{padding:9px 12px;font-size:13.5px;opacity:0;transform:translateY(-8px);
transition:opacity .3s,transform .3s,background .2s,color .2s}
.grp.open .smenu a{opacity:1;transform:none}
.grp.open .smenu a:nth-child(1){transition-delay:.06s,.06s,0s,0s}
.grp.open .smenu a:nth-child(2){transition-delay:.12s,.12s,0s,0s}
.grp.open .smenu a:nth-child(3){transition-delay:.18s,.18s,0s,0s}
.links .smenu a.on{color:#fff;background:linear-gradient(135deg,var(--p1),var(--p2));box-shadow:0 6px 18px rgba(109,124,255,.35)}
@media(prefers-reduced-motion:reduce){.smenu,.smenu a,.gbtn .chev{transition:none!important}}
.side-foot{margin-top:auto;display:flex;flex-direction:column;gap:8px;padding-top:14px;border-top:1px solid var(--bd)}
.me{display:flex;align-items:center;gap:8px;padding:4px 6px;color:var(--mut);font-size:13px}
.me b{color:var(--tx)}
.topbar{display:none}
.burger{width:42px;height:42px;border-radius:12px;border:1px solid var(--bd);background:var(--inp);cursor:pointer;
display:flex;flex-direction:column;align-items:center;justify-content:center;gap:5px;padding:0}
.burger span{width:18px;height:2px;background:var(--tx);border-radius:2px;transition:.25s}
.burger.open span:nth-child(1){transform:translateY(7px) rotate(45deg)}
.burger.open span:nth-child(2){opacity:0}
.burger.open span:nth-child(3){transform:translateY(-7px) rotate(-45deg)}
.wrap{margin-right:var(--side);padding:26px 28px 60px}
.main{max-width:1000px;margin:0 auto}
/* ---------- components ---------- */
.card{background:var(--card);border:1px solid var(--bd);border-radius:18px;padding:20px;margin-bottom:18px;
backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px)}
.card h3{margin:0 0 14px;font-size:16px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.card h3 small{color:var(--mut);font-weight:400;font-size:12px;margin-inline-start:auto}
.card h4{margin:20px 0 10px;font-size:14px;display:flex;align-items:center;gap:8px}
.muted{color:var(--mut);font-size:14px;margin:0 0 14px}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:18px}
.stat{background:var(--card);border:1px solid var(--bd);border-radius:16px;padding:16px;text-align:center}
.stat b{display:block;font-size:24px;background:linear-gradient(135deg,var(--p1),var(--p2));
-webkit-background-clip:text;background-clip:text;color:transparent}
.stat>span{color:var(--mut);font-size:13px}
.stat .two{font-size:13px;color:var(--tx);direction:ltr;margin-top:4px;line-height:1.8;font-variant-numeric:tabular-nums}
.stat .two b{display:inline;font-size:inherit;background:none;-webkit-text-fill-color:currentColor;color:inherit}
.dn{color:var(--dn)}.upc{color:var(--upc)}
label{display:block;font-size:12px;color:var(--mut);margin-bottom:4px}
input{width:100%;padding:11px 13px;border-radius:12px;border:1px solid var(--bd);background:var(--inp);
color:var(--tx);font:inherit;outline:none;transition:.2s}
input:focus{border-color:var(--p1);box-shadow:0 0 0 3px rgba(109,124,255,.25)}
input[readonly]{opacity:.75;cursor:not-allowed}
input[type=file]{padding:10px;cursor:pointer}
.stack{display:flex;flex-direction:column;gap:12px}
.row{display:flex;gap:10px;align-items:center}.row input{flex:1;min-width:0}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:7px;padding:11px 18px;border:0;
border-radius:12px;color:#fff;cursor:pointer;font:inherit;font-size:14px;text-decoration:none;
background:linear-gradient(135deg,var(--p1),var(--p2));transition:.2s;white-space:nowrap}
.btn:hover{transform:translateY(-1px);filter:brightness(1.1)}
.btn.sm{padding:8px 13px;font-size:13px;border-radius:10px}
.btn.ghost{background:var(--inp);color:var(--tx);border:1px solid var(--bd)}
.btn.red{background:rgba(251,113,133,.15);color:var(--bad);border:1px solid rgba(251,113,133,.35)}
.btn.block{width:100%}
.toast{padding:12px 16px;border-radius:12px;margin-bottom:12px;font-size:14px;white-space:pre-wrap;
border:1px solid var(--bd);animation:in .3s;word-break:break-word}
.toast.ok{background:rgba(52,211,153,.12);color:var(--ok);border-color:rgba(52,211,153,.35)}
.toast.err{background:rgba(251,113,133,.12);color:var(--bad);border-color:rgba(251,113,133,.35)}
@keyframes in{from{opacity:0;transform:translateY(-6px)}to{opacity:1}}
.warnbox{display:flex;gap:10px;align-items:flex-start;background:rgba(251,191,36,.12);border:1px solid rgba(251,191,36,.35);
color:var(--warn);padding:12px 16px;border-radius:12px;margin-bottom:16px;font-size:14px;word-break:break-word}
.tag{display:inline-flex;align-items:center;gap:4px;font-size:11px;padding:2px 9px;border-radius:99px;
background:var(--inp);color:var(--mut);border:1px solid var(--bd)}
.tag.bad{color:var(--bad);border-color:rgba(251,113,133,.4);background:rgba(251,113,133,.1)}
.tag .ic{vertical-align:-1px}
.tags{display:flex;gap:6px;flex-wrap:wrap;margin-top:3px}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block;margin-inline-end:7px}
.dot.on{background:var(--ok);box-shadow:0 0 8px var(--ok)}.dot.off{background:var(--bad)}
.pill{font-size:12px;padding:3px 11px;border-radius:99px;border:1px solid var(--bd);color:var(--mut);white-space:nowrap}
.pill.on{color:var(--ok);border-color:rgba(52,211,153,.4);background:rgba(52,211,153,.1)}
.bar{height:7px;border-radius:99px;background:var(--inp);overflow:hidden;margin-top:6px}
.bar i{display:block;height:100%;border-radius:99px;background:linear-gradient(90deg,var(--p1),var(--p2))}
.bar i.ok{background:linear-gradient(90deg,var(--ok),#22d3ee)}.bar i.warn{background:var(--warn)}.bar i.bad{background:var(--bad)}
.sub{display:block;font-size:11px;color:var(--mut);font-weight:400}
.av{width:44px;height:44px;border-radius:14px;display:grid;place-items:center;font-weight:700;font-size:18px;
background:linear-gradient(135deg,var(--p1),var(--p2));color:#fff;flex:none}
/* ---------- create form ---------- */
.form{display:grid;grid-template-columns:repeat(12,1fr);gap:10px;align-items:start}
.f-id{grid-column:span 2}.f-name{grid-column:span 4}.f-proto{grid-column:span 2}.f-port{grid-column:span 4}
.f-q{grid-column:span 3}.f-btn{grid-column:1/-1}.f-btn .btn{width:100%;padding:12px}
.chip{padding:11px 16px;border-radius:12px;background:var(--inp);border:1px dashed var(--bd);font-weight:700;text-align:center;color:var(--p1)}
.pwrap{display:flex;gap:6px}.pwrap input{flex:1;min-width:0}
#pmsg{display:block;font-size:12px;margin-top:4px;min-height:18px}
.seg{display:flex;background:var(--inp);border:1px solid var(--bd);border-radius:12px;padding:3px;gap:3px}
.seg label{flex:1;margin:0;cursor:pointer}.seg input{display:none}
.seg span{display:block;text-align:center;padding:8px 6px;border-radius:9px;font-size:13px;font-weight:700;color:var(--mut);transition:.2s}
.seg input:checked+span{background:linear-gradient(135deg,var(--p1),var(--p2));color:#fff;box-shadow:0 4px 12px rgba(109,124,255,.35)}
/* ---------- user cards ---------- */
.search{margin-bottom:14px}
.users,.ugrid{display:grid;grid-template-columns:repeat(2,1fr);gap:14px}
.user,.ucard{background:var(--card);border:1px solid var(--bd);border-radius:16px;padding:16px;transition:.2s;min-width:0}
.user:hover,.ucard:hover{border-color:rgba(109,124,255,.5);transform:translateY(-2px)}
.uh{display:flex;align-items:center;gap:12px;margin-bottom:12px}
.uh b{font-size:16px;display:block;word-break:break-all}
.exp{margin:10px 0 8px;font-size:13px;color:var(--mut)}
.exp .t-warn{color:var(--warn)}.exp .t-bad{color:var(--bad)}
.exp .ic,.uuse .ic{margin-inline-end:4px}
.uuse{font-size:12px;color:var(--mut);margin-bottom:12px;display:flex;gap:14px;flex-wrap:wrap}
.acts{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.acts form{display:flex;gap:6px;margin:0}.acts input{width:76px;padding:8px 10px;border-radius:10px;font-size:13px}
.empty{text-align:center;color:var(--mut);padding:30px}
/* ---------- portal link / download password ---------- */
.portal{display:flex;flex-direction:column;gap:8px;margin:0 0 12px;padding:10px 12px;border-radius:12px;background:var(--inp)}
.prow{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.plabel{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--mut);margin-inline-end:auto}
.pw{font-weight:700;letter-spacing:.5px;user-select:all;font-size:14px}
.portal form{margin:0;display:flex}
.btn.ico{padding:8px 10px}
.btn.done{background:rgba(52,211,153,.18)!important;color:var(--ok)!important;border-color:rgba(52,211,153,.4)!important}
/* ---------- usage cards ---------- */
.kv{display:flex;flex-direction:column;gap:8px;margin:4px 0 14px;padding:10px 12px;border-radius:12px;background:var(--inp)}
.kv div{display:flex;justify-content:space-between;align-items:center;gap:10px;font-size:13px}
.kv span.k{color:var(--mut);display:flex;align-items:center;gap:6px;white-space:nowrap}
.kv span.v{text-align:end;min-width:0}
.tgrid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:12px}
.tg{padding:8px 10px;border-radius:10px;background:var(--inp);font-size:12px;color:var(--mut);min-width:0}
.tg b{display:block;font-size:14px;color:var(--tx);font-variant-numeric:tabular-nums;white-space:nowrap}
.lim{font-size:12px;color:var(--mut);margin-bottom:10px}
.lim .l{display:flex;justify-content:space-between;gap:8px}
.live{font-size:12px;display:flex;gap:14px;margin-top:10px}
/* ---------- network ---------- */
.speeds{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:18px}
.speed{margin:0;text-align:center;direction:ltr}
.speed>span{color:var(--mut);font-size:13px;display:inline-flex;align-items:center;gap:6px}
.speed b{display:block;font-size:28px;margin:4px 0 0;font-variant-numeric:tabular-nums;white-space:nowrap}
.speed small{color:var(--mut)}
.speed.d b{color:var(--dn)}.speed.u b{color:var(--upc)}
.speed.c b,.speed.r b{color:var(--ok)}.speed.c b.warn,.speed.r b.warn{color:var(--warn)}.speed.c b.bad,.speed.r b.bad{color:var(--bad)}
.speed .bar{margin:10px 4px 0}.speed .bar i{transition:width .5s}
.speed .bar i.ok{background:linear-gradient(90deg,var(--ok),#22d3ee)}
canvas{width:100%;height:260px;display:block}
.legend{display:flex;gap:18px;justify-content:center;font-size:12px;color:var(--mut);margin-top:8px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:3px;margin-inline-end:6px}
/* ---------- backup / advanced ---------- */
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:18px;align-items:start}
.chk{display:flex;gap:10px;align-items:flex-start;margin:12px 0 4px;font-size:13px;color:var(--tx);cursor:pointer;line-height:1.5}
.chk input{width:18px;height:18px;margin-top:2px;flex:none;accent-color:var(--p1)}
.hint{color:var(--mut);font-size:12px;margin:0 0 14px;padding-inline-start:28px}
.alist{display:flex;flex-direction:column;gap:8px}
.arow{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:10px;padding:10px 12px;border-radius:12px;background:var(--inp)}
.arow>span{display:flex;align-items:center;gap:8px;min-width:0;word-break:break-all}
/* ---------- dialog ---------- */
.dlg{border:1px solid var(--bd);border-radius:20px;background:var(--navs);color:var(--tx);padding:0;
width:calc(100% - 28px);max-width:540px;box-shadow:0 30px 80px rgba(0,0,0,.5)}
.dlg::backdrop{background:rgba(5,8,20,.6);backdrop-filter:blur(4px)}
.dlg-in{padding:22px}
.dlg h3{margin:0 0 16px;display:flex;align-items:center;gap:8px;font-size:17px;flex-wrap:wrap}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px}.span2{grid-column:1/-1}
.dwarn{display:flex;gap:8px;align-items:flex-start;margin:14px 0;font-size:12px;color:var(--warn);line-height:1.6}
.dacts{display:flex;gap:8px;flex-wrap:wrap}.dacts .btn{flex:1 1 auto}
/* ---------- responsive ---------- */
@media(max-width:1180px){.speeds{grid-template-columns:1fr 1fr}}
@media(max-width:980px){
 .topbar{display:flex;position:sticky;top:0;z-index:40;height:62px;align-items:center;justify-content:space-between;
  padding:0 14px;background:var(--nav);border-bottom:1px solid var(--bd);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px)}
 .side{display:none;top:62px;left:0;right:0;bottom:auto;width:auto;max-height:calc(100vh - 62px);border-left:0;
  border-bottom:1px solid var(--bd);background:var(--navs);box-shadow:0 24px 40px rgba(0,0,0,.35);padding:12px 14px 16px}
 .side.open{display:flex;animation:in .2s}
 .side-brand{display:none}
 .side-foot{margin-top:8px}
 .links a{padding:13px 14px;font-size:15px;background:var(--inp)}
 .gbtn{padding:13px 14px;font-size:15px;background:var(--inp)}
 .links .smenu a{padding:12px 14px;font-size:14px}
 .side-foot .btn{padding:13px 14px;font-size:15px}
 .wrap{margin-right:0;padding:16px 12px 48px}
}
@media(max-width:760px){
 .card{padding:16px;border-radius:16px}
 .stats{grid-template-columns:repeat(2,1fr);gap:10px}
 .stat{padding:12px}.stat b{font-size:20px}.stat>span{font-size:12px}
 .two-col{grid-template-columns:1fr}
 .form{grid-template-columns:1fr 1fr}.form>div{grid-column:span 1}
 .form .f-name,.form .f-port,.form .f-proto,.form .f-btn{grid-column:1/-1}
 .btn.block,.f-btn .btn{width:100%;padding:13px}
 .pwrap .btn{padding:0 14px;flex:none}
 input{font-size:16px;padding:12px 13px}
 .seg span{padding:11px 6px}
 .users,.ugrid{grid-template-columns:1fr}
 .acts>a.btn{flex:1 1 100%;padding:12px}
 .acts>button.btn{flex:1 1 40%;padding:11px}
 .acts form.ext{flex:1 1 100%;min-width:0}
 .acts form.ext input{flex:1;min-width:0;width:auto;font-size:16px;padding:10px}
 .acts form.ext .btn{padding:10px 16px;flex:none}
 .acts form.mini{flex:1 1 40%}.acts form.mini .btn{flex:1;padding:11px}
 .speeds{gap:10px}.speed{padding:14px 10px}.speed b{font-size:21px}
 canvas{height:210px}
 .legend{gap:12px}
 .tgrid{grid-template-columns:repeat(2,1fr)}
 .row{flex-direction:column;align-items:stretch}
}
@media(max-width:480px){.grid2{grid-template-columns:1fr}}
</style>"""

LOGIN_HTML = """<!doctype html><html lang="{{ lang }}" dir="{{ dirn }}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><title>{{ t('login_title') }} | {{ app }}</title>
{{ css|safe }}</head><body>
<div style="max-width:400px;margin:0 auto;padding:10vh 16px 0">
<div style="text-align:end;margin-bottom:12px"><a class="btn ghost sm" href="{{ url_for('set_lang', code='fa' if lang=='en' else 'en') }}">{{ ic('globe',16) }} {{ t('lang_name') }}</a></div>
<div class="card" style="text-align:center;padding:32px 24px">
<div class="logo" style="margin:0 auto 14px;width:60px;height:60px;border-radius:19px">{{ ic('shield',30) }}</div>
<h2 style="margin:0 0 4px">Gass<span style="color:var(--p1)">Light</span></h2>
<p class="muted" style="margin:0 0 20px">{{ t('login_sub') }}</p>
{% for c, m in get_flashed_messages(with_categories=true) %}<div class="toast {{ c }}">{{ m }}</div>{% endfor %}
<form method="post" class="stack" style="text-align:start">
<div><label>{{ t('login_user') }}</label><input name="username" autocomplete="username" autocapitalize="none" autofocus required></div>
<div><label>{{ t('login_pw') }}</label><input type="password" name="password" autocomplete="current-password" required></div>
<button class="btn block">{{ ic('logout',16) }} {{ t('login_btn') }}</button></form></div></div></body></html>"""

TOP = """<!doctype html><html lang="{{ lang }}" dir="{{ dirn }}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><title>{{ app }}</title>
{{ css|safe }}</head><body>
<header class="topbar">
<a class="brand" href="{{ url_for('index') }}"><div class="logo">{{ ic('shield',20) }}</div><span>Gass<em>Light</em></span></a>
<button class="burger" id="bg" type="button" aria-label="{{ t('menu') }}" aria-expanded="false"><span></span><span></span><span></span></button>
</header>
<aside class="side" id="menu">
<a class="brand side-brand" href="{{ url_for('index') }}"><div class="logo">{{ ic('shield',20) }}</div><span>Gass<em>Light</em></span></a>
<nav class="links">
<a href="{{ url_for('index') }}" class="{{ 'on' if active=='dash' }}">{{ ic('home') }}<span>{{ t('nav_dash') }}</span></a>
<a href="{{ url_for('network') }}" class="{{ 'on' if active=='net' }}">{{ ic('wifi') }}<span>{{ t('nav_net') }}</span></a>
<a href="{{ url_for('usage') }}" class="{{ 'on' if active=='usage' }}">{{ ic('bars') }}<span>{{ t('nav_usage') }}</span></a>
<a href="{{ url_for('backup_page') }}" class="{{ 'on' if active=='backup' }}">{{ ic('drive') }}<span>{{ t('nav_backup') }}</span></a>
{% set advon = active in ('adv_acc','adv_px','adv_ex') %}
<div class="grp {{ 'open' if advon }}" id="advgrp">
<button type="button" class="gbtn {{ 'on' if advon }}" id="advbtn" aria-expanded="{{ 'true' if advon else 'false' }}" aria-controls="advsub">{{ ic('sliders') }}<span>{{ t('nav_adv') }}</span>{{ ic('chevron',16,'chev') }}</button>
<div class="smenu" id="advsub"><div>
<a href="{{ url_for('adv_pg_account') }}" class="{{ 'on' if active=='adv_acc' }}">{{ ic('key',16) }}<span>{{ t('nav_adv_acc') }}</span></a>
<a href="{{ url_for('adv_pg_proxy') }}" class="{{ 'on' if active=='adv_px' }}">{{ ic('globe',16) }}<span>{{ t('nav_adv_px') }}</span></a>
<a href="{{ url_for('adv_pg_exit') }}" class="{{ 'on' if active=='adv_ex' }}">{{ ic('server',16) }}<span>{{ t('nav_adv_ex') }}</span></a>
</div></div></div>
</nav>
<div class="side-foot">
<div class="me">{{ ic('user',16) }}<span>{{ t('signed_as') }} <b>{{ me }}</b></span></div>
<a class="btn ghost sm" href="{{ url_for('set_lang', code='fa' if lang=='en' else 'en') }}">{{ ic('globe',16) }} {{ t('lang_name') }}</a>
<a class="btn ghost sm" href="{{ url_for('logout') }}">{{ ic('logout',16) }} {{ t('logout') }}</a>
</div></aside>
<main class="wrap"><div class="main">
{% for c, m in get_flashed_messages(with_categories=true) %}<div class="toast {{ c }}">{{ m }}</div>{% endfor %}
"""

BOTTOM = """</div></main>
<script>
(function(){var b=document.getElementById('bg'),m=document.getElementById('menu');
function set(o){m.classList.toggle('open',o);b.classList.toggle('open',o);b.setAttribute('aria-expanded',o)}
b.addEventListener('click',function(e){e.stopPropagation();set(!m.classList.contains('open'))});
document.addEventListener('click',function(e){if(!m.contains(e.target))set(false)});
window.addEventListener('resize',function(){if(window.innerWidth>980)set(false)});
var g=document.getElementById('advgrp'),gb=document.getElementById('advbtn');
if(g&&gb)gb.addEventListener('click',function(){var o=!g.classList.contains('open');g.classList.toggle('open',o);gb.setAttribute('aria-expanded',o)});})();
</script></body></html>"""

DASH_HTML = """
{% if not ready %}<div class="warnbox">{{ ic('alert',18) }}<span>{{ t('warn_nohost') }}</span></div>{% endif %}

<div class="stats">
<div class="stat"><b>{{ stats.total }}</b><span>{{ t('st_total') }}</span></div>
<div class="stat"><b>{{ stats.active }}</b><span>{{ t('st_active') }}</span></div>
<div class="stat"><b>{{ stats.soon }}</b><span>{{ t('st_soon') }}</span></div>
<div class="stat"><b>{{ stats.unlimited }}</b><span>{{ t('st_unl') }}</span></div>
</div>

<div class="card"><h3>{{ ic('user-plus') }} {{ t('c_title') }}</h3>
<form method="post" action="{{ url_for('create') }}"><div class="form">
<div class="f-id"><label>{{ t('c_id') }}</label><div class="chip">#{{ nid }}</div></div>
<div class="f-name"><label>{{ t('c_name') }}</label><input name="name" placeholder="ali" pattern="[A-Za-z0-9_-]{1,32}" autocapitalize="none" autocomplete="off" required></div>
<div class="f-proto"><label>{{ t('c_proto') }}</label><div class="seg">
<label><input type="radio" name="proto" value="udp" checked><span>UDP</span></label>
<label><input type="radio" name="proto" value="tcp"><span>TCP</span></label></div></div>
<div class="f-port"><label>{{ t('c_port') }}</label>
<div class="pwrap"><input id="port" name="port" type="number" inputmode="numeric" min="1" max="65535" value="{{ port }}" readonly>
<button type="button" class="btn ghost sm" id="pbtn">{{ ic('edit',15) }} <span>{{ t('c_manual') }}</span></button></div>
<span id="pmsg"></span></div>
<div class="f-q"><label>{{ t('c_days') }}</label><input name="days" type="number" inputmode="numeric" min="0" max="3650" value="30"></div>
<div class="f-q"><label>{{ t('c_speed') }}</label><input name="speed" type="number" inputmode="decimal" min="0" max="10000" step="any" value="0"></div>
<div class="f-q"><label>{{ t('c_daily') }}</label><input name="daily" type="number" inputmode="decimal" min="0" max="1000000" step="any" value="0"></div>
<div class="f-q"><label>{{ t('c_total') }}</label><input name="total" type="number" inputmode="decimal" min="0" max="1000000" step="any" value="0"></div>
<div class="f-btn"><button class="btn">{{ ic('plus',16) }} {{ t('c_btn') }}</button></div>
</div><input type="hidden" name="manual" id="manual" value="0"></form></div>

{% if adopt %}<div class="card"><h3>{{ ic('server') }} {{ t('ad_title') }}</h3>
<p class="muted">{{ t('ad_desc') }}</p>
<div class="alist">{% for n in adopt %}<div class="arow"><span>{{ ic('user',16) }}<span class="mono">{{ n }}</span></span>
<form method="post" action="{{ url_for('adopt', name=n) }}" style="margin:0;display:flex;gap:6px;align-items:center">
<small class="muted" style="margin:0">{{ t('ad_add') }}</small>
<button class="btn ghost sm" name="proto" value="udp">UDP</button><button class="btn sm" name="proto" value="tcp">TCP</button></form></div>
{% endfor %}</div></div>{% endif %}

<div class="card"><h3>{{ ic('users') }} {{ t('u_title') }}</h3>
<input class="search" id="q" placeholder="{{ t('u_search') }}" oninput="f()">
<div class="users">
{% for u in users %}
<div class="user" data-s="{{ u.name|lower }} {{ u.port }}">
<div class="uh"><div class="av">{{ u.name[0]|upper }}</div>
<div class="grow"><b><span class="dot {{ 'on' if u.active else 'off' }}"></span>{{ u.name }}</b>
<div class="tags"><span class="tag">ID #{{ u.id }}</span><span class="tag">{{ t('port') }} {{ u.port }}</span>
<span class="tag">{{ u.proto|upper }}</span>
<span class="tag">{{ t('on') if u.active else t('off') }}</span>
<span class="tag">{{ ic('zap',11) }} {% if u.speed %}{{ '%g'|format(u.speed) }} Mbps{% else %}{{ t('nospeed') }}{% endif %}</span>
{% if u.limit_daily %}<span class="tag">{{ ic('calendar',11) }} {{ t('lim_daily') }} {{ '%g'|format(u.limit_daily) }} GB</span>{% endif %}
{% if u.limit_total %}<span class="tag">{{ ic('drive',11) }} {{ t('lim_total') }} {{ '%g'|format(u.limit_total) }} GB</span>{% endif %}
{% if u.blocked %}<span class="tag bad">{{ ic('slash',11) }} {{ t('blk_' ~ u.blocked) }}</span>{% endif %}
</div></div></div>
<div class="exp">
{% if u.left_txt %}<span class="t-{{ u.level }}">{{ ic('clock',14) }}{{ u.left_txt }}</span>
<div class="bar"><i class="{{ u.level }}" style="width:{{ u.pct }}%"></i></div>
{% else %}{{ ic('infinity',14) }}{{ t('nolimit') }}{% endif %}</div>
<div class="uuse"><span class="dn">{{ ic('down',13) }}{{ u.down|fmtb }}</span><span class="upc">{{ ic('up',13) }}{{ u.up|fmtb }}</span></div>
<div class="portal">
<div class="prow"><span class="plabel">{{ ic('key',14) }}{{ t('dl_pw') }}</span><span class="mono pw">{{ u.dl_password }}</span>
<button type="button" class="btn ghost sm ico" data-copy="{{ u.dl_password }}" title="{{ t('copy_pw') }}">{{ ic('copy',14) }}</button>
<form method="post" action="{{ url_for('portal_regen_pw', name=u.name) }}"
onsubmit='return confirm({{ t("confirm_regen_pw", name=u.name)|tojson }})'><button class="btn ghost sm ico" title="{{ t('regen_pw') }}">{{ ic('refresh',14) }}</button></form></div>
<div class="prow"><button type="button" class="btn ghost sm" data-copy="{{ portal_base }}/u/{{ u.token }}">{{ ic('link',14) }}<span>{{ t('copy_link') }}</span></button>
<a class="btn ghost sm ico" target="_blank" rel="noopener" href="{{ portal_base }}/u/{{ u.token }}" title="{{ t('open_portal') }}">{{ ic('external',14) }}</a>
<form method="post" action="{{ url_for('portal_regen_link', name=u.name) }}"
onsubmit='return confirm({{ t("confirm_regen_link", name=u.name)|tojson }})'><button class="btn ghost sm ico" title="{{ t('regen_link') }}">{{ ic('refresh',14) }}</button></form></div>
</div>
<div class="acts">
<a class="btn sm" href="{{ url_for('download', name=u.name) }}">{{ ic('download',15) }} {{ t('dl_ovpn') }}</a>
<button type="button" class="btn ghost sm" data-edit data-name="{{ u.name }}" data-id="{{ u.id }}" data-port="{{ u.port }}"
 data-proto="{{ u.proto }}" data-speed="{{ '%g'|format(u.speed) if u.speed else 0 }}"
 data-daily="{{ '%g'|format(u.limit_daily) if u.limit_daily else 0 }}" data-total="{{ '%g'|format(u.limit_total) if u.limit_total else 0 }}">{{ ic('edit',15) }} {{ t('edit_btn') }}</button>
<form class="ext" method="post" action="{{ url_for('extend', name=u.name) }}">
<input name="days" type="number" inputmode="numeric" min="1" placeholder="{{ t('days_ph') }}" required><button class="btn ghost sm">{{ ic('plus',15) }} {{ t('extend') }}</button></form>
{% if u.left_txt %}<form class="mini" method="post" action="{{ url_for('unlimit', name=u.name) }}">
<button class="btn ghost sm" title="{{ t('unl_title') }}">{{ ic('infinity',16) }}</button></form>{% endif %}
<form class="mini" method="post" action="{{ url_for('delete', name=u.name) }}"
onsubmit='return confirm({{ t("confirm_del", name=u.name)|tojson }})'><button class="btn red sm">{{ ic('trash',15) }} {{ t('delete') }}</button></form>
</div></div>
{% else %}<div class="empty" style="grid-column:1/-1">{{ t('u_none') }}</div>{% endfor %}
</div></div>

<dialog id="ed" class="dlg"><div class="dlg-in"><form method="post" id="edf">
<h3>{{ ic('edit',18) }} {{ t('edit_title') }} <span class="mono" id="edn"></span></h3>
<div class="grid2">
<div><label>{{ t('edit_id') }}</label><input name="id" type="number" min="1" required></div>
<div><label>{{ t('edit_port') }}</label><input name="port" type="number" min="1" max="65535" required></div>
<div class="span2"><label>{{ t('edit_proto') }}</label><div class="seg">
<label><input type="radio" name="proto" value="udp" id="e_udp"><span>UDP</span></label>
<label><input type="radio" name="proto" value="tcp" id="e_tcp"><span>TCP</span></label></div></div>
<div class="span2"><label>{{ t('edit_speed') }}</label><input name="speed" type="number" min="0" max="10000" step="any"></div>
<div><label>{{ t('edit_daily') }}</label><input name="daily" type="number" min="0" max="1000000" step="any"></div>
<div><label>{{ t('edit_total') }}</label><input name="total" type="number" min="0" max="1000000" step="any"></div>
</div>
<div class="dwarn">{{ ic('alert',16) }}<span>{{ t('edit_warn') }}</span></div>
<div class="dacts">
<button class="btn">{{ ic('check',16) }} {{ t('edit_save') }}</button>
<button type="button" class="btn ghost" id="edc">{{ t('edit_cancel') }}</button>
<button type="submit" class="btn red" id="edr" formnovalidate>{{ ic('refresh',16) }} {{ t('reset_usage') }}</button>
</div></form></div></dialog>

<script>
var I={{ jt('c_manual','c_auto','confirm_reset')|tojson }};
function f(){var q=document.getElementById('q').value.toLowerCase();
document.querySelectorAll('.user').forEach(function(e){e.style.display=e.dataset.s.indexOf(q)>-1?'':'none'})}
setTimeout(function(){document.querySelectorAll('.toast.ok').forEach(function(t){t.style.display='none'})},7000);
(function(){
var inp=document.getElementById('port'),btn=document.getElementById('pbtn'),lab=btn.querySelector('span'),man=document.getElementById('manual'),
msg=document.getElementById('pmsg'),auto=inp.value,t=null;
btn.onclick=function(){
 if(man.value==='0'){man.value='1';inp.readOnly=false;inp.focus();inp.select();lab.textContent=I.c_auto;check();}
 else{man.value='0';inp.readOnly=true;inp.value=auto;lab.textContent=I.c_manual;msg.textContent='';}
};
function check(){
 clearTimeout(t);
 t=setTimeout(function(){
  if(man.value!=='1'||!inp.value)return;
  fetch('/api/port_check?port='+encodeURIComponent(inp.value)+'&proto='+((document.querySelector('.f-proto input[name=proto]:checked')||{}).value||'udp')).then(function(r){return r.json()}).then(function(d){
   msg.textContent=d.msg;msg.style.color=d.ok?'var(--ok)':'var(--bad)';}).catch(function(){});
 },300);
}
inp.addEventListener('input',check);
document.querySelectorAll('.f-proto input[name=proto]').forEach(function(r){r.addEventListener('change',check)});
})();
(function(){
var dlg=document.getElementById('ed'),form=document.getElementById('edf'),els=form.elements,rst=document.getElementById('edr'),cur='';
document.querySelectorAll('[data-edit]').forEach(function(b){b.addEventListener('click',function(){
 var d=b.dataset;cur=d.name;form.action='/edit/'+d.name;document.getElementById('edn').textContent=d.name;
 els['id'].value=d.id;els['port'].value=d.port;els['speed'].value=d.speed;els['daily'].value=d.daily;els['total'].value=d.total;
 document.getElementById('e_'+d.proto).checked=true;rst.formAction='/reset/'+d.name;dlg.showModal();});});
rst.addEventListener('click',function(e){if(!confirm(I.confirm_reset.replace('{name}',cur)))e.preventDefault();});
document.getElementById('edc').addEventListener('click',function(){dlg.close()});
dlg.addEventListener('click',function(e){if(e.target===dlg)dlg.close()});
})();
</script>
<script>
(function(){
var DONE={{ jt('copied')|tojson }}.copied;
function copyText(txt){
 if(navigator.clipboard&&window.isSecureContext)return navigator.clipboard.writeText(txt);
 return new Promise(function(res,rej){var ta=document.createElement('textarea');ta.value=txt;ta.setAttribute('readonly','');
  ta.style.cssText='position:fixed;top:0;left:0;opacity:0';document.body.appendChild(ta);ta.select();ta.setSelectionRange(0,99999);
  var ok=false;try{ok=document.execCommand('copy')}catch(e){}document.body.removeChild(ta);ok?res():rej()})}
document.querySelectorAll('[data-copy]').forEach(function(b){b.addEventListener('click',function(){
 copyText(b.dataset.copy).then(function(){var s=b.querySelector('span'),old=s?s.textContent:null;b.classList.add('done');if(s)s.textContent=DONE;
  setTimeout(function(){b.classList.remove('done');if(s)s.textContent=old},1500)}).catch(function(){window.prompt('',b.dataset.copy)})})});
})();
</script>
"""

NET_HTML = """
<div class="speeds">
<div class="card speed d"><span>{{ ic('down',16) }} {{ t('n_down') }}</span><b id="dn">0 bps</b><small id="dn2">0 B/s</small></div>
<div class="card speed u"><span>{{ ic('up',16) }} {{ t('n_up') }}</span><b id="up">0 bps</b><small id="up2">0 B/s</small></div>
<div class="card speed c"><span>{{ ic('cpu',16) }} {{ t('n_cpu') }}</span><b id="cpu">0%</b><small id="cpu2">-</small>
<div class="bar"><i id="cpub" class="ok" style="width:0"></i></div></div>
<div class="card speed r"><span>{{ ic('memory',16) }} {{ t('n_ram') }}</span><b id="ram">0%</b><small id="ram2">-</small>
<div class="bar"><i id="ramb" class="ok" style="width:0"></i></div></div>
</div>
<div class="card"><h3>{{ ic('activity') }} {{ t('n_live') }} <small>{{ t('n_iface') }}: <span id="iface">-</span></small></h3>
<canvas id="ch"></canvas>
<div class="legend"><span><i style="background:var(--dn)"></i>{{ t('n_dn') }}</span><span><i style="background:var(--upc)"></i>{{ t('n_upl') }}</span></div></div>
<div class="card"><h3>{{ ic('cpu') }} {{ t('n_sys') }}</h3>
<canvas id="ch2"></canvas>
<div class="legend"><span><i style="background:#fbbf24"></i>{{ t('n_cpu') }}</span><span><i style="background:#34d399"></i>{{ t('n_ram') }}</span></div></div>
<div class="stats">
<div class="stat"><span>{{ t('n_today') }}</span><div class="two"><span class="dn">{{ ic('down',13) }} <b id="td">-</b></span><br><span class="upc">{{ ic('up',13) }} <b id="tu">-</b></span></div></div>
<div class="stat"><span>{{ t('n_month') }}</span><div class="two"><span class="dn">{{ ic('down',13) }} <b id="md">-</b></span><br><span class="upc">{{ ic('up',13) }} <b id="mu">-</b></span></div></div>
<div class="stat"><span>{{ t('n_boot') }}</span><div class="two"><span class="dn">{{ ic('down',13) }} <b id="bd">-</b></span><br><span class="upc">{{ ic('up',13) }} <b id="bu">-</b></span></div></div>
<div class="stat"><span>{{ t('n_peak') }}</span><div class="two"><span class="dn">{{ ic('down',13) }} <b id="pd">-</b></span><br><span class="upc">{{ ic('up',13) }} <b id="pu">-</b></span></div></div>
</div>
<script>
var I={{ jt('m3','m15','now','n_cores','n_load','n_swap')|tojson }};
var $=function(i){return document.getElementById(i)},N=180;
function fb(n){var u=['B','KB','MB','GB','TB'],i=0;while(n>=1024&&i<4){n/=1024;i++}return n.toFixed(i?2:0)+'\u00a0'+u[i]}
function fs(bytes){var b=bytes*8,u=['bps','Kbps','Mbps','Gbps'],i=0;while(b>=1000&&i<3){b/=1000;i++}return b.toFixed(i>1?2:(i?1:0))+' '+u[i]}
function fp(v){return Math.round(v)+'%'}
function lvl(v){return v>=90?'bad':(v>=70?'warn':'ok')}
function chart(id,h,series,fmt,fixedMax){
 var c=$(id),dpr=window.devicePixelRatio||1,w=c.clientWidth,H=c.clientHeight||260;
 c.width=w*dpr;c.height=H*dpr;var g=c.getContext('2d');g.scale(dpr,dpr);
 var sm=w<480,L=sm?46:64,B=22,T=10,pw=w-L-8,ph=H-B-T,mx=fixedMax||1000;
 if(!fixedMax){h.forEach(function(p){series.forEach(function(s){mx=Math.max(mx,p[s.k])})});mx*=1.15;}
 var mut=getComputedStyle(document.body).getPropertyValue('--mut')||'#888';
 g.font=(sm?'10':'11')+'px sans-serif';g.fillStyle=mut;g.strokeStyle='rgba(128,140,180,.18)';g.lineWidth=1;g.textAlign='right';
 for(var i=0;i<=4;i++){var y=T+ph*i/4;g.beginPath();g.moveTo(L,y);g.lineTo(L+pw,y);g.stroke();g.fillText(fmt(mx*(1-i/4)),L-6,y+4)}
 g.textAlign='center';g.fillText(I.m3,L+10,H-6);g.fillText(I.m15,L+pw/2,H-6);g.fillText(I.now,L+pw-10,H-6);
 series.forEach(function(s){
  if(h.length<2)return;
  g.beginPath();
  h.forEach(function(p,i){var x=L+pw-(h.length-1-i)*pw/(N-1),y=T+ph-(Math.min(p[s.k],mx)/mx)*ph;i?g.lineTo(x,y):g.moveTo(x,y)});
  g.strokeStyle=s.col;g.lineWidth=2;g.lineJoin='round';g.stroke();
  g.lineTo(L+pw,T+ph);g.lineTo(L+pw-(h.length-1)*pw/(N-1),T+ph);g.closePath();
  var gr=g.createLinearGradient(0,T,0,T+ph);gr.addColorStop(0,s.fill);gr.addColorStop(1,'rgba(0,0,0,0)');
  g.fillStyle=gr;g.fill();
 });
}
function gauge(idv,idt,idb,v,txt){var e=$(idv);e.textContent=fp(v);e.className=lvl(v);$(idt).textContent=txt;
 var b=$(idb);b.style.width=Math.min(100,v)+'%';b.className=lvl(v)}
function tick(){
 fetch('/api/net').then(function(r){if(r.status===401){location='/login';return null}return r.json()}).then(function(d){
  if(!d)return;
  $('dn').textContent=fs(d.rx);$('up').textContent=fs(d.tx);
  $('dn2').textContent=fb(d.rx)+'/s';$('up2').textContent=fb(d.tx)+'/s';
  $('iface').textContent=d.iface;
  $('td').textContent=fb(d.today[0]);$('tu').textContent=fb(d.today[1]);
  $('md').textContent=fb(d.month[0]);$('mu').textContent=fb(d.month[1]);
  $('bd').textContent=fb(d.total_rx);$('bu').textContent=fb(d.total_tx);
  var pr=0,pt=0;d.hist.forEach(function(p){pr=Math.max(pr,p[1]);pt=Math.max(pt,p[2])});
  $('pd').textContent=fs(pr);$('pu').textContent=fs(pt);
  chart('ch',d.hist,[{k:1,col:'#22d3ee',fill:'rgba(34,211,238,.28)'},{k:2,col:'#a45cff',fill:'rgba(164,92,255,.28)'}],fs);
  var s=d.sys,m=s.mem||{};
  gauge('cpu','cpu2','cpub',s.cpu,I.n_cores+': '+s.cores+' · '+I.n_load+' '+s.load.map(function(x){return x.toFixed(2)}).join(' '));
  gauge('ram','ram2','ramb',m.pct||0,fb(m.used||0)+' / '+fb(m.total||0)+' · '+I.n_swap+' '+fb(m.swap_used||0));
  chart('ch2',s.hist,[{k:1,col:'#fbbf24',fill:'rgba(251,191,36,.25)'},{k:2,col:'#34d399',fill:'rgba(52,211,153,.25)'}],fp,100);
 }).catch(function(){});
}
tick();setInterval(tick,1000);window.addEventListener('resize',tick);
</script>
"""

USAGE_HTML = """
<div class="stats">
<div class="stat"><b id="s_n">0</b><span>{{ t('us_n') }}</span></div>
<div class="stat"><b id="s_d">0</b><span>{{ t('us_d') }}</span></div>
<div class="stat"><b id="s_u">0</b><span>{{ t('us_u') }}</span></div>
<div class="stat"><b id="s_t">0</b><span>{{ t('us_t') }}</span></div>
</div>
<div class="card"><h3>{{ ic('bars') }} {{ t('us_title') }} <small><span id="upd">-</span> · {{ t('us_refresh') }}</small></h3>
<div class="ugrid" id="cards"><div class="empty" style="grid-column:1/-1">{{ t('us_load') }}</div></div>
<p class="muted" style="font-size:12px;margin:14px 0 0">{{ t('us_note') }}</p></div>
<script>
var I={{ jt('port','d_ago','u_none','th_created','th_down','th_up','th_total','th_share','th_live','u_local_ip','u_last',
 'u_online','u_offline','u_never','u_since','u_today','u_daily_lim','u_total_lim','u_nolimit','ago_now','ago_min','ago_h','ago_d',
 'blk_daily','blk_total')|tojson }};
var IC={{ icj('down','up','clock','server','calendar','activity','slash')|tojson }};
var GB=1073741824,$=function(i){return document.getElementById(i)};
function fb(n){var u=['B','KB','MB','GB','TB'],i=0;while(n>=1024&&i<4){n/=1024;i++}return n.toFixed(i?2:0)+' '+u[i]}
function fs(bytes){var b=bytes*8,u=['bps','Kbps','Mbps','Gbps'],i=0;while(b>=1000&&i<3){b/=1000;i++}return b.toFixed(i>1?2:(i?1:0))+' '+u[i]}
function p2(n){return ('0'+n).slice(-2)}
function ds(ts){var d=new Date(ts*1000);return d.getFullYear()+'-'+p2(d.getMonth()+1)+'-'+p2(d.getDate())}
function dt(ts){var d=new Date(ts*1000);return ds(ts)+' '+p2(d.getHours())+':'+p2(d.getMinutes())}
function ago(ts,now){var s=Math.max(0,now-ts);
 if(s<60)return I.ago_now;if(s<3600)return I.ago_min.replace('{n}',Math.floor(s/60));
 if(s<86400)return I.ago_h.replace('{n}',Math.floor(s/3600));return I.ago_d.replace('{n}',Math.floor(s/86400))}
function row(ic,k,v){return '<div><span class="k">'+ic+k+'</span><span class="v">'+v+'</span></div>'}
function lim(label,used,gb){
 if(!gb)return '<div class="lim"><div class="l"><span>'+label+'</span><span>'+I.u_nolimit+'</span></div></div>';
 var pct=Math.min(100,used/(gb*GB)*100),cls=pct>=100?'bad':(pct>=80?'warn':'ok');
 return '<div class="lim"><div class="l"><span>'+label+'</span><span>'+fb(used)+' / '+gb+' GB</span></div><div class="bar"><i class="'+cls+'" style="width:'+pct.toFixed(1)+'%"></i></div></div>'}
function load(){
 fetch('/api/usage').then(function(r){if(r.status===401){location='/login';return null}return r.json()}).then(function(d){
  if(!d)return;var us=d.users,td=0,tu=0;
  us.forEach(function(u){td+=u.down;tu+=u.up;u.tot=u.down+u.up});
  us.sort(function(a,b){return b.tot-a.tot});
  $('s_n').textContent=us.length;$('s_d').textContent=fb(td);$('s_u').textContent=fb(tu);$('s_t').textContent=fb(td+tu);
  var all=td+tu,h='';
  us.forEach(function(u){
   var pct=all?Math.round(u.tot/all*100):0,days=Math.max(0,Math.floor((d.now-u.created)/86400)),
   last=u.online?('<b>'+I.u_online+'</b> · '+I.u_since+' '+dt(u.since||u.last)):(u.last?dt(u.last)+'<span class="sub">'+ago(u.last,d.now)+'</span>':I.u_never);
   h+='<div class="ucard"><div class="uh"><div class="av">'+u.name[0].toUpperCase()+'</div><div class="grow"><b><span class="dot '+(u.online?'on':'off')+'"></span>'+u.name+'</b>'+
   '<div class="tags"><span class="tag">ID #'+u.id+'</span><span class="tag">'+I.port+' '+u.port+'</span><span class="tag">'+u.proto.toUpperCase()+'</span>'+
   (u.blocked?'<span class="tag bad">'+IC.slash+I['blk_'+u.blocked]+'</span>':'')+'</div></div>'+
   '<span class="pill '+(u.online?'on':'')+'">'+(u.online?I.u_online:I.u_offline)+'</span></div>'+
   '<div class="kv">'+row(IC.server,I.u_local_ip,u.ip?'<span class="mono">'+u.ip+'</span>':'—')+row(IC.clock,I.u_last,last)+
   row(IC.calendar,I.th_created,ds(u.created)+'<span class="sub">'+I.d_ago.replace('{n}',days)+'</span>')+'</div>'+
   '<div class="tgrid"><div class="tg">'+I.th_down+'<b class="dn">'+fb(u.down)+'</b></div><div class="tg">'+I.th_up+'<b class="upc">'+fb(u.up)+'</b></div>'+
   '<div class="tg">'+I.th_total+'<b>'+fb(u.tot)+'</b></div><div class="tg">'+I.u_today+'<b>'+fb(u.day_up+u.day_down)+'</b></div></div>'+
   lim(I.u_daily_lim,u.day_up+u.day_down,u.limit_daily)+lim(I.u_total_lim,u.tot,u.limit_total)+
   '<div class="lim"><div class="l"><span>'+I.th_share+'</span><span>'+pct+'%</span></div><div class="bar"><i style="width:'+pct+'%"></i></div></div>'+
   '<div class="live"><span class="dn">'+IC.down+' '+fs(u.rate_down)+'</span><span class="upc">'+IC.up+' '+fs(u.rate_up)+'</span></div></div>';
  });
  $('cards').innerHTML=h||'<div class="empty" style="grid-column:1/-1">'+I.u_none+'</div>';
  var t=new Date(d.now*1000);$('upd').textContent=p2(t.getHours())+':'+p2(t.getMinutes())+':'+p2(t.getSeconds());
 }).catch(function(){$('upd').textContent='—'});
}
load();setInterval(load,2000);
</script>
"""

BACKUP_HTML = """
<div class="two-col">
<div class="card"><h3>{{ ic('download') }} {{ t('b_create_title') }}</h3>
<p class="muted">{{ t('b_create_desc') }}</p>
<div class="stat" style="margin-bottom:14px"><b>{{ count }}</b><span>{{ t('b_users') }}</span></div>
<div class="warnbox">{{ ic('alert',18) }}<span>{{ t('b_warn') }}</span></div>
<a class="btn block" href="{{ url_for('backup_download') }}">{{ ic('download',16) }} {{ t('b_download') }}</a></div>

<div class="card"><h3>{{ ic('upload') }} {{ t('b_import_title') }}</h3>
<p class="muted">{{ t('b_import_desc') }}</p>
<form method="post" action="{{ url_for('backup_import') }}" enctype="multipart/form-data" id="impf">
<label>{{ t('b_file') }}</label>
<input type="file" name="file" accept=".zip,application/zip" required>
<label class="chk"><input type="checkbox" name="restore_pki" value="1" id="rp"><span>{{ t('b_restore_pki') }}</span></label>
<p class="hint">{{ t('b_restore_hint') }}</p>
<button class="btn block">{{ ic('upload',16) }} {{ t('b_import_btn') }}</button></form></div>
</div>
<script>
var I={{ jt('b_confirm')|tojson }};
document.getElementById('impf').addEventListener('submit',function(e){
 if(document.getElementById('rp').checked&&!confirm(I.b_confirm))e.preventDefault();});
</script>
"""

ADV_ACC_HTML = """
<div class="two-col">
<div class="card"><h3>{{ ic('key') }} {{ t('adv_title_acc') }}</h3>
<p class="muted">{{ t('adv_acc_desc') }}</p>
<form method="post" action="{{ url_for('adv_account') }}" class="stack">
<div><label>{{ t('adv_username') }}</label><input name="username" value="{{ me }}" pattern="[A-Za-z0-9_.-]{3,32}" autocomplete="username" autocapitalize="none" required></div>
<div><label>{{ t('adv_cur_pw') }}</label><input type="password" name="current" autocomplete="current-password" required></div>
<div><label>{{ t('adv_new_pw') }}</label><input type="password" name="password" minlength="6" autocomplete="new-password"></div>
<div><label>{{ t('adv_rep_pw') }}</label><input type="password" name="password2" minlength="6" autocomplete="new-password"></div>
<button class="btn">{{ ic('check',16) }} {{ t('adv_save') }}</button></form></div>

<div class="card"><h3>{{ ic('users') }} {{ t('adv_title_users') }}</h3>
<p class="muted">{{ t('adv_users_desc') }}</p>
<div class="alist">
{% for a in admins %}<div class="arow"><span>{{ ic('user',16) }}<span class="mono">{{ a }}</span>{% if a == me %}<span class="tag">{{ t('adv_you') }}</span>{% endif %}</span>
{% if a != me %}<form method="post" action="{{ url_for('adv_user_delete', uname=a) }}" style="margin:0"
onsubmit='return confirm({{ t("adv_del_confirm", name=a)|tojson }})'><button class="btn red sm">{{ ic('trash',14) }}</button></form>{% endif %}</div>
{% endfor %}</div>
<h4>{{ ic('user-plus',16) }} {{ t('adv_add_title') }}</h4>
<form method="post" action="{{ url_for('adv_user_add') }}" class="stack">
<div><label>{{ t('adv_username') }}</label><input name="username" pattern="[A-Za-z0-9_.-]{3,32}" autocomplete="off" autocapitalize="none" required></div>
<div><label>{{ t('adv_password') }}</label><input type="password" name="password" minlength="6" autocomplete="new-password" required></div>
<button class="btn">{{ ic('plus',16) }} {{ t('adv_add_btn') }}</button></form></div>
</div>
"""

ADV_PX_HTML = """
<div class="card pxx"><h3>{{ ic('globe') }} {{ t('px_title') }}</h3>
<style>.pxx select,.pxx textarea{width:100%;padding:11px 13px;border-radius:12px;border:1px solid var(--bd);background:var(--inp);color:var(--tx);font:inherit;outline:none}
.pxx textarea{resize:vertical;min-height:130px;font-size:13px;direction:ltr}.pxx option{background:var(--bg);color:var(--tx)}
.pxx .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}.pxx .alist{max-height:340px;overflow:auto}</style>
<p class="muted">{{ t('px_desc') }}</p>
{% if not px.relay_ok %}<p class="muted" style="color:var(--bad)">{{ t('px_relay_bad', port=px.relay_port) }}</p>{% endif %}
<form method="post" action="{{ url_for('adv_proxy_save') }}" class="stack">
<label class="chk"><input type="checkbox" name="enabled" value="1" {% if px.enabled %}checked{% endif %}><span>{{ t('px_enable') }}</span></label>
<div><label>{{ t('px_list') }}</label><textarea name="list" class="mono" spellcheck="false" autocomplete="off" placeholder="socks5://1.2.3.4:1080">{{ px.text }}</textarea>
<p class="hint" style="padding-inline-start:0;margin:6px 0 0">{{ t('px_hint') }}</p></div>
<div><label>{{ t('px_dtype') }}</label><select name="dtype">
<option value="socks5" {% if px.dtype=='socks5' %}selected{% endif %}>SOCKS5</option>
<option value="socks4" {% if px.dtype=='socks4' %}selected{% endif %}>SOCKS4</option>
<option value="http" {% if px.dtype=='http' %}selected{% endif %}>HTTP</option></select></div>
<div><label>{{ t('px_mode') }}</label><select name="mode">
<option value="sticky" {% if px.mode=='sticky' %}selected{% endif %}>{{ t('px_m_sticky') }}</option>
<option value="random" {% if px.mode=='random' %}selected{% endif %}>{{ t('px_m_random') }}</option>
<option value="round" {% if px.mode=='round' %}selected{% endif %}>{{ t('px_m_round') }}</option></select></div>
<div><label>{{ t('px_rot') }}</label><input type="number" name="rotate_min" min="0" max="1440" value="{{ px.rotate_min }}"></div>
<label class="chk"><input type="checkbox" name="block_quic" value="1" {% if px.block_quic %}checked{% endif %}><span>{{ t('px_quic') }}</span></label>
<label class="chk"><input type="checkbox" name="fallback" value="1" {% if px.fallback %}checked{% endif %}><span>{{ t('px_fb') }}</span></label>
<p class="hint" style="padding-inline-start:0">{{ ic('alert',14) }} {{ t('px_note') }}</p>
<div><button class="btn">{{ ic('check',16) }} {{ t('px_save') }}</button></div></form>

<h4>{{ ic('server',16) }} {{ t('px_status') }} <span class="muted" id="pxprog" style="margin:0"></span></h4>
<div class="row" style="margin-bottom:10px">
<form method="post" action="{{ url_for('adv_proxy_test') }}" style="margin:0"><button class="btn ghost sm">{{ ic('refresh',14) }} {{ t('px_test') }}</button></form>
<form method="post" action="{{ url_for('adv_proxy_prune') }}" style="margin:0"><button class="btn red sm">{{ ic('trash',14) }} {{ t('px_prune') }}</button></form></div>
<div class="alist" id="pxrows"></div>
<script>
(function(){
var I={{ jt('px_st_ok','px_st_bad','px_st_unk','px_st_cool','px_none','px_testing_n')|tojson }};
var box=document.getElementById('pxrows'),prog=document.getElementById('pxprog');
function el(tag,cls,txt){var e=document.createElement(tag);if(cls)e.className=cls;if(txt!=null)e.textContent=txt;return e;}
function render(d){
 box.textContent='';
 if(!d.rows.length){box.appendChild(el('div','empty',I.px_none));}
 d.rows.forEach(function(x){
  var r=el('div','arow'),a=el('span','mono',x.addr),b=el('span','');
  b.style.cssText='display:flex;gap:10px;align-items:center;flex-wrap:wrap';
  if(x.ip)b.appendChild(el('span','mono',x.ip));
  if(x.ms)b.appendChild(el('span','muted',x.ms+' ms'));
  b.lastChild&&(b.lastChild.style.margin='0');
  if(x.ok===true)b.appendChild(el('span','pill on',I.px_st_ok));
  else if(x.ok===false)b.appendChild(el('span','tag bad',I.px_st_bad));
  else if(x.cool)b.appendChild(el('span','tag bad',I.px_st_cool));
  else b.appendChild(el('span','pill',I.px_st_unk));
  r.appendChild(a);r.appendChild(b);box.appendChild(r);});
 prog.textContent=d.test.running?I.px_testing_n.replace('{done}',d.test.done).replace('{total}',d.test.total):'';
}
function poll(){fetch('/api/proxy',{credentials:'same-origin'}).then(function(r){return r.json()}).then(function(d){
 render(d);if(d.test.running)setTimeout(poll,1500);}).catch(function(){});}
poll();
})();
</script>
</div>
"""

ADV_EX_HTML = """
<div class="card pxx">
<style>.pxx select,.pxx textarea{width:100%;padding:11px 13px;border-radius:12px;border:1px solid var(--bd);background:var(--inp);color:var(--tx);font:inherit;outline:none}
.pxx textarea{resize:vertical;min-height:130px;font-size:13px;direction:ltr}.pxx option{background:var(--bg);color:var(--tx)}
.pxx .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}.pxx .alist{max-height:340px;overflow:auto}</style><h3>{{ ic('server') }} {{ t('ex_title') }}</h3>
<p class="muted">{{ t('ex_desc') }}</p>
<form method="post" action="{{ url_for('adv_exit_settings') }}" class="stack">
<label class="chk"><input type="checkbox" name="enabled" value="1" {% if ex.enabled %}checked{% endif %}><span>{{ t('ex_enable') }}</span></label>
<div><label>{{ t('px_mode') }}</label><select name="mode">
<option value="sticky" {% if ex.mode=='sticky' %}selected{% endif %}>{{ t('ex_m_sticky') }}</option>
<option value="random" {% if ex.mode=='random' %}selected{% endif %}>{{ t('ex_m_random') }}</option>
<option value="round" {% if ex.mode=='round' %}selected{% endif %}>{{ t('ex_m_round') }}</option></select></div>
<div><label>{{ t('ex_rot') }}</label><input type="number" name="rotate_min" min="0" max="1440" value="{{ ex.rotate_min }}"></div>
<label class="chk"><input type="checkbox" name="block_quic" value="1" {% if ex.block_quic %}checked{% endif %}><span>{{ t('px_quic') }}</span></label>
<label class="chk"><input type="checkbox" name="fallback" value="1" {% if ex.fallback %}checked{% endif %}><span>{{ t('ex_fb') }}</span></label>
<p class="hint" style="padding-inline-start:0">{{ ic('alert',14) }} {{ t('ex_note') }}</p>
<div><button class="btn">{{ ic('check',16) }} {{ t('px_save') }}</button></div></form>

<h4>{{ ic('server',16) }} {{ t('ex_servers') }} <span class="muted" id="exprog" style="margin:0"></span></h4>
<div class="row" style="margin-bottom:10px">
<form method="post" action="{{ url_for('adv_exit_test') }}" style="margin:0"><button class="btn ghost sm">{{ ic('refresh',14) }} {{ t('px_test') }}</button></form></div>
<div class="alist">
{% for s in ex.servers %}
<div class="arow" style="flex-wrap:wrap;gap:10px">
<span class="mono">{{ s.ip }}:{{ s.port }} <span class="muted" style="margin:0">{{ s.username }}</span></span>
<span style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
<span class="mono muted" id="exd-{{ s.id }}" style="margin:0">{{ s.exit_ip or '' }}{% if s.ms %} · {{ s.ms }} ms{% endif %}</span>
<span id="exs-{{ s.id }}" class="{% if s.state=='ok' %}pill on{% elif s.state in ('bad','cool') %}tag bad{% else %}pill{% endif %}">{{ t('ex_st_'+s.state) }}</span>
<form method="post" action="{{ url_for('adv_exit_toggle', sid=s.id) }}" style="margin:0"><button class="btn ghost sm">{{ t('ex_off') if s.active else t('ex_on') }}</button></form>
<form method="post" action="{{ url_for('adv_exit_delete', sid=s.id) }}" style="margin:0" onsubmit='return confirm({{ t("ex_confirm_del", ip=s.ip)|tojson }})'><button class="btn red sm" title="{{ t('ex_del') }}">{{ ic('trash',14) }}</button></form>
</span>
<details style="flex-basis:100%"><summary class="muted" style="cursor:pointer">{{ ic('edit',14) }} {{ t('ex_edit') }}</summary>
<form method="post" action="{{ url_for('adv_exit_update', sid=s.id) }}" class="stack" style="margin-top:10px">
<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px">
<input name="ip" value="{{ s.ip }}" placeholder="{{ t('ex_ip') }}" required autocomplete="off">
<input name="port" type="number" min="1" max="65535" value="{{ s.port }}" placeholder="{{ t('ex_port') }}" required>
<input name="username" value="{{ s.username }}" placeholder="{{ t('ex_user') }}" required autocomplete="off">
<input name="password" type="password" placeholder="{{ t('ex_pw_keep') }}" autocomplete="new-password"></div>
<div><button class="btn sm">{{ ic('check',14) }} {{ t('px_save') }}</button></div></form></details>
</div>
{% else %}<div class="empty">{{ t('ex_none') }}</div>{% endfor %}
</div>

<h4>{{ ic('plus',16) }} {{ t('ex_add') }}</h4>
<form method="post" action="{{ url_for('adv_exit_add') }}" class="stack">
<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px">
<input name="ip" placeholder="{{ t('ex_ip') }}" required autocomplete="off">
<input name="port" type="number" min="1" max="65535" value="1080" placeholder="{{ t('ex_port') }}" required>
<input name="username" placeholder="{{ t('ex_user') }}" required autocomplete="off">
<input name="password" type="password" placeholder="{{ t('ex_pw') }}" required autocomplete="new-password"></div>
<div><button class="btn">{{ ic('plus',16) }} {{ t('ex_add_btn') }}</button></div></form>
<script>
(function(){
var I={{ jt('ex_st_ok','ex_st_bad','ex_st_unk','ex_st_cool','ex_st_off','ex_testing_n')|tojson }};
var prog=document.getElementById('exprog');
var CL={ok:'pill on',bad:'tag bad',cool:'tag bad',off:'pill',unk:'pill'};
var TX={ok:I.ex_st_ok,bad:I.ex_st_bad,cool:I.ex_st_cool,off:I.ex_st_off,unk:I.ex_st_unk};
function paint(x){
 var e=document.getElementById('exs-'+x.id);if(!e)return;
 e.className=CL[x.state];e.textContent=TX[x.state];
 var d=document.getElementById('exd-'+x.id);
 if(d)d.textContent=(x.exit_ip||'')+(x.ms?' \u00b7 '+x.ms+' ms':'');
}
function poll(){fetch('/api/exit',{credentials:'same-origin'}).then(function(r){return r.json()}).then(function(d){
 d.servers.forEach(paint);
 prog.textContent=d.test.running?I.ex_testing_n.replace('{done}',d.test.done).replace('{total}',d.test.total):'';
 setTimeout(poll,d.test.running?1500:10000);}).catch(function(){setTimeout(poll,15000);});}
setTimeout(poll,1500);
})();
</script>
</div>

<div class="card" style="margin-top:18px"><h3>{{ ic('link') }} {{ t('oa_title') }}</h3>
<p class="muted">{{ t('oa_desc') }}</p>
<form method="post" action="{{ url_for('adv_ovpn_addr') }}" class="stack">
<div><label>{{ t('oa_label') }}</label><input name="addr" value="{{ oa.addr }}" placeholder="{{ t('oa_ph') }}" autocomplete="off" spellcheck="false" style="direction:ltr"></div>
<p class="hint" style="padding-inline-start:0">{{ t('oa_hint') }}</p>
<div><button class="btn">{{ ic('check',16) }} {{ t('px_save') }}</button></div></form>
</div>
"""


if __name__ == "__main__":
    load_auth()      # creates the first "admin" account right at startup (needs PANEL_PASSWORD only once)
    load_usage()
    restore_rules()
    drop_legacy_exit_ip()
    threading.Thread(target=migrate_management, daemon=True).start()
    threading.Thread(target=reaper, daemon=True).start()
    threading.Thread(target=sampler, daemon=True).start()
    proxy_start()
    app.run(host=HOST, port=PORT, threaded=True)
