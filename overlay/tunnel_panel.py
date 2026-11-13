#!/usr/bin/env python3
import ipaddress
import json
import os
import random
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from urllib.parse import parse_qs, unquote, urlparse, urlsplit

from markupsafe import Markup
from werkzeug.security import check_password_hash, generate_password_hash

from flask import (Flask, abort, flash, jsonify, redirect, render_template_string,
                   request, session, url_for)

APP_NAME = "GassLight Tunnel"
BASE = os.environ.get("TUNNEL_DATA", "/opt/gl-tunnel")
AUTH_FILE = f"{BASE}/auth.json"
SECRET_FILE = f"{BASE}/secret.key"
CFG_FILE = f"{BASE}/tunnel.json"
RELAYS_FILE = f"{BASE}/relays.json"
STATE_FILE = f"{BASE}/state.json"
TRAFFIC_FILE = f"{BASE}/traffic.json"
XRAY_DIR = f"{BASE}/xray"
XRAY_CONF = f"{XRAY_DIR}/config.json"
XRAY_LOG = f"{XRAY_DIR}/xray.log"
XRAY_BIN = os.environ.get("XRAY_BIN", "/usr/local/bin/xray")
XRAY_MODE = os.environ.get("XRAY_MODE", "systemd")
XRAY_SERVICE = os.environ.get("XRAY_SERVICE", "gl-xray")
PASSWORD = os.environ.get("PANEL_PASSWORD", "")
HOST = os.environ.get("TUNNEL_HOST", "0.0.0.0")
PORT = int(os.environ.get("TUNNEL_PORT", "8090"))
PROBE_LOCAL = int(os.environ.get("TUNNEL_PROBE_LOCAL", "18999"))
ROT_UNIT = int(os.environ.get("TUNNEL_ROT_UNIT", "60"))
HEALTH_EVERY = int(os.environ.get("TUNNEL_HEALTH_EVERY", "30"))
USER_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
HOST_RE = re.compile(r"^[A-Za-z0-9.\-]{1,253}$")
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
PBK_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
SID_RE = re.compile(r"^(?:[0-9a-fA-F]{2}){0,8}$")
FINGERPRINTS = ("chrome", "firefox", "safari", "ios", "android", "edge", "360", "qq", "random", "randomized")
FLOWS = ("xtls-rprx-vision", "")
MAX_PORTS = 500
MAX_RELAYS = 50
ROT_MAX_MIN = 19
LOCK = threading.RLock()
SWLOCK = threading.RLock()
SLOCK = threading.Lock()
FAILS = {}
RNG = random.SystemRandom()

os.makedirs(XRAY_DIR, mode=0o700, exist_ok=True)
if not PASSWORD and not os.path.exists(AUTH_FILE):
    sys.exit("PANEL_PASSWORD is not set (needed once, to create the first admin account)")
if not os.path.exists(SECRET_FILE):
    with open(SECRET_FILE, "w") as f:
        f.write(secrets.token_hex(32))
    os.chmod(SECRET_FILE, 0o600)

app = Flask(__name__)
app.secret_key = open(SECRET_FILE).read().strip()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict")


def fmtb(n):
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.2f} {u}"
        n /= 1024


app.add_template_filter(fmtb)

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
 "shuffle": '<polyline points="16 3 21 3 21 8"/><line x1="4" y1="20" x2="21" y2="3"/><polyline points="21 16 21 21 16 21"/><line x1="15" y1="15" x2="21" y2="21"/><line x1="4" y1="4" x2="9" y2="9"/>',
 "arrow-right": '<line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/>',
 "pause": '<rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/>',
 "play": '<polygon points="5 3 19 12 5 21 5 3"/>',
 "terminal": '<polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/>',
 "power": '<path d="M18.36 6.64a9 9 0 1 1-12.73 0"/><line x1="12" y1="2" x2="12" y2="12"/>',
}


def icon(name, size=18, cls=""):
    return Markup(f'<svg class="ic {cls}" width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" '
                  f'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" '
                  f'aria-hidden="true">{ICONS[name]}</svg>')

TR = {
 "en": {
  "lang_name": "فارسی",
  "logout": "Logout",
  "menu": "Menu",
  "login_title": "Sign in",
  "login_pw": "Password",
  "login_btn": "Sign in",
  "err_many": "Too many attempts, wait a few minutes",
  "err_login": "Wrong username or password",
  "login_user": "Username",
  "signed_as": "Signed in as",
  "adv_title_acc": "My account",
  "adv_acc_desc": "Change your username and password. Leave the new password empty to keep the current one.",
  "adv_cur_pw": "Current password",
  "adv_username": "Username",
  "adv_new_pw": "New password",
  "adv_rep_pw": "Repeat new password",
  "adv_save": "Save changes",
  "adv_title_users": "Panel users",
  "adv_users_desc": "Everyone listed here can sign in to this panel.",
  "adv_you": "you",
  "adv_add_title": "Add panel user",
  "adv_password": "Password",
  "adv_add_btn": "Add user",
  "adv_del_confirm": "Delete panel user {name}?",
  "f_acc_pw": "Current password is wrong",
  "f_acc_user": "Username must be 3-32 characters: letters, digits, . _ -",
  "f_acc_exists": "This username already exists",
  "f_acc_short": "Password must be at least 6 characters",
  "f_acc_mismatch": "Passwords do not match",
  "f_acc_saved": "Account updated",
  "f_adm_added": "Panel user {name} added",
  "f_adm_self": "You can't delete your own account here",
  "f_adm_last": "At least one panel user is required",
  "f_adm_deleted": "Panel user {name} deleted",
  "n_cpu": "CPU",
  "n_ram": "RAM",
  "n_sys": "System load",
  "n_cores": "cores",
  "n_load": "load",
  "n_swap": "swap",
  "n_down": "Download (RX)",
  "n_up": "Upload (TX)",
  "n_live": "Live traffic",
  "n_iface": "interface",
  "n_dn": "Download",
  "n_upl": "Upload",
  "n_today": "Today",
  "n_month": "This month",
  "n_boot": "Since server boot",
  "n_peak": "Peak (last 3 min)",
  "m3": "-3m",
  "m15": "-1.5m",
  "now": "now",
  "nav_dash": "Dashboard",
  "nav_relays": "Relays",
  "nav_net": "Traffic",
  "nav_log": "Log",
  "nav_set": "Settings",
  "login_sub": "Tunnel control panel",
  "st_tunnel": "Tunnel",
  "st_relay": "Active relay",
  "st_next": "Next switch",
  "st_probe": "Path check",
  "v_running": "Running",
  "v_stopped": "Stopped",
  "v_paused": "Paused",
  "v_ok": "OK",
  "v_fail": "Failed",
  "v_off": "Off",
  "rt_title": "Current route",
  "rt_users": "Users",
  "rt_here": "This server",
  "rt_ports": "ports {ports}",
  "rt_main": "Main server",
  "rt_none": "No relay in use",
  "rt_conns": "Open tunnels",
  "t_now": "now",
  "a_rotate": "Switch route now",
  "a_pause": "Pause rotation",
  "a_resume": "Resume rotation",
  "a_stop": "Stop tunnel",
  "a_start": "Start tunnel",
  "a_use": "Use now",
  "d_relays": "Relays",
  "d_hist": "Switch history",
  "s_ok": "Reachable",
  "s_down": "Unreachable",
  "s_bad": "Skipped (failed)",
  "s_unk": "Not checked",
  "s_off": "Disabled",
  "s_cur": "In use",
  "r_start": "Start",
  "r_scheduled": "Scheduled",
  "r_failover": "Failover",
  "r_manual": "Manual",
  "h_fail": "failed",
  "w_noxray": "Xray is not installed at {path}. Run overlay-panel-setup.sh (or set XRAY_BIN).",
  "w_nomain": "Set the main server address in Settings.",
  "w_norelay": "Add at least one relay.",
  "w_stopped": "The tunnel is stopped: users cannot connect through this server.",
  "w_skipped": "These ports are used by another program on this server and are skipped: {ports}",
  "w_problem": "Last problem: {msg}",
  "need_xray": "Xray",
  "need_main": "main server address",
  "need_relay": "an enabled relay",
  "rl_title": "Add relay",
  "rl_link_desc": "Paste the vless:// link printed by the relay setup script (REALITY over TCP).",
  "rl_link": "Link",
  "rl_import": "Import",
  "rl_manual": "Add manually",
  "rl_name": "Name",
  "rl_host": "Address",
  "rl_port": "Port",
  "rl_sni": "Server name (SNI)",
  "rl_uuid": "UUID",
  "rl_pbk": "Public key",
  "rl_sid": "Short ID",
  "rl_fp": "Fingerprint",
  "rl_flow": "Flow",
  "rl_add": "Add",
  "rl_list": "Relays",
  "rl_test": "Check all now",
  "rl_none": "No relays yet",
  "rl_edit": "Edit",
  "rl_on": "Enable",
  "rl_off": "Disable",
  "rl_del": "Delete",
  "rl_confirm_del": "Delete relay {name}?",
  "lg_title": "Xray log",
  "lg_refresh": "Refresh",
  "lg_empty": "No log lines yet",
  "se_title": "Tunnel settings",
  "se_desc": "Users connect to this server on their own port; the panel carries every connection through the active relay to the main server.",
  "se_main": "Main server address (the OpenVPN / GassLight server)",
  "se_main_hint": "The relays hand traffic to this address, so it must be reachable from them.",
  "se_ports": "Carried ports",
  "se_ports_hint": "UDP and TCP of every port listed are carried. Ranges and lists are fine (1195-1300 or 1195-1300,2000). Use the same ports the main panel gives to users.",
  "se_listen": "Listen address",
  "se_listen_hint": "Leave 0.0.0.0 to accept users on every address of this server, or enter one IP of this server.",
  "f_se_listen": "Invalid listen address (use an IPv4 address)",
  "se_probe": "Path-check port on the main server (0 = off)",
  "se_probe_hint": "After every switch the panel opens a test connection to this port on the main server, through the tunnel. The main GassLight panel (8088) is a good choice. If the check fails the panel leaves that relay at once.",
  "se_rot": "Rotate the route automatically",
  "se_rot_min": "Switch every (minutes): at least",
  "se_rot_max": "at most (max 19)",
  "se_save": "Save",
  "se_fw": "Open these ports (UDP and TCP) in your firewall: {ports}",
  "se_xver": "Version",
  "se_xnone": "not installed",
  "se_xbin": "Binary",
  "se_xmode": "Run mode",
  "se_apply": "Apply now (restart Xray)",
  "f_se_need": "To start the tunnel you still need: {what}",
  "f_tn_started": "Tunnel starting",
  "f_tn_stopped": "Tunnel stopped",
  "f_tn_notrunning": "The tunnel is not running",
  "f_tn_norelay": "There is no other healthy relay to switch to",
  "f_tn_rotated": "Switched to {name}",
  "f_tn_fail": "Switch failed: {msg}",
  "f_tn_rot_on": "Rotation resumed",
  "f_tn_rot_off": "Rotation paused",
  "f_rl_bad_link": "Not a valid link: it must be vless:// with security=reality and type=tcp",
  "f_rl_bad_host": "Invalid relay address",
  "f_rl_bad_port": "Invalid port (1-65535)",
  "f_rl_bad_uuid": "Invalid UUID",
  "f_rl_bad_key": "Invalid public key (43 characters, base64url)",
  "f_rl_bad_sid": "Invalid short ID (up to 16 hex characters, even length)",
  "f_rl_bad_sni": "Invalid server name (SNI)",
  "f_rl_bad_fp": "Unknown fingerprint",
  "f_rl_bad_flow": "Unknown flow",
  "f_rl_many": "Too many relays (max {max})",
  "f_rl_added": "Relay {name} added",
  "f_rl_updated": "Relay updated",
  "f_rl_deleted": "Relay deleted",
  "f_rl_toggled": "Relay updated",
  "f_rl_missing": "Relay not found",
  "f_rl_tested": "Check finished",
  "f_se_main": "Invalid main server address",
  "f_se_ports": "Invalid ports: {why}",
  "f_se_rot": "Rotation needs whole minutes from 1 to 19 (min at most max), and a valid check port",
  "f_se_saved": "Settings saved",
  "f_se_applied": "Applied",
  "f_se_failed": "Could not apply: {msg}",
 },
 "fa": {
  "lang_name": "English",
  "logout": "خروج",
  "menu": "منو",
  "login_title": "ورود",
  "login_pw": "رمز عبور",
  "login_btn": "ورود",
  "err_many": "تعداد تلاش زیاد بود، چند دقیقه صبر کن",
  "err_login": "نام کاربری یا رمز اشتباه است",
  "login_user": "نام کاربری",
  "signed_as": "واردشده با",
  "adv_title_acc": "حساب من",
  "adv_acc_desc": "نام کاربری و رمزت رو عوض کن. اگه رمز جدید رو خالی بذاری رمز فعلی می‌مونه.",
  "adv_cur_pw": "رمز فعلی",
  "adv_username": "نام کاربری",
  "adv_new_pw": "رمز جدید",
  "adv_rep_pw": "تکرار رمز جدید",
  "adv_save": "ذخیره‌ی تغییرات",
  "adv_title_users": "کاربران پنل",
  "adv_users_desc": "همه‌ی کسایی که اینجا هستن می‌تونن وارد پنل بشن.",
  "adv_you": "شما",
  "adv_add_title": "افزودن کاربر پنل",
  "adv_password": "رمز عبور",
  "adv_add_btn": "افزودن",
  "adv_del_confirm": "کاربر پنل {name} حذف بشه؟",
  "f_acc_pw": "رمز فعلی اشتباهه",
  "f_acc_user": "نام کاربری باید ۳ تا ۳۲ کاراکتر باشه: حروف انگلیسی، عدد، . _ -",
  "f_acc_exists": "این نام کاربری قبلاً هست",
  "f_acc_short": "رمز باید حداقل ۶ کاراکتر باشه",
  "f_acc_mismatch": "رمزها یکی نیستن",
  "f_acc_saved": "حساب به‌روز شد",
  "f_adm_added": "کاربر پنل {name} اضافه شد",
  "f_adm_self": "حساب خودت رو از اینجا نمی‌تونی حذف کنی",
  "f_adm_last": "حداقل یه کاربر پنل لازمه",
  "f_adm_deleted": "کاربر پنل {name} حذف شد",
  "n_cpu": "پردازنده (CPU)",
  "n_ram": "رم (RAM)",
  "n_sys": "منابع سیستم",
  "n_cores": "هسته",
  "n_load": "لود",
  "n_swap": "swap",
  "n_down": "دانلود (RX)",
  "n_up": "آپلود (TX)",
  "n_live": "ترافیک زنده",
  "n_iface": "کارت شبکه",
  "n_dn": "دانلود",
  "n_upl": "آپلود",
  "n_today": "امروز",
  "n_month": "این ماه",
  "n_boot": "از زمان بوت سرور",
  "n_peak": "پیک ۳ دقیقه اخیر",
  "m3": "-3m",
  "m15": "-1.5m",
  "now": "now",
  "nav_dash": "داشبورد",
  "nav_relays": "ریله‌ها",
  "nav_net": "ترافیک",
  "nav_log": "لاگ",
  "nav_set": "تنظیمات",
  "login_sub": "پنل کنترل تانل",
  "st_tunnel": "تانل",
  "st_relay": "ریله‌ی فعال",
  "st_next": "تغییر مسیر بعدی",
  "st_probe": "تست مسیر",
  "v_running": "روشن",
  "v_stopped": "خاموش",
  "v_paused": "متوقف",
  "v_ok": "سالم",
  "v_fail": "خراب",
  "v_off": "غیرفعال",
  "rt_title": "مسیر فعلی",
  "rt_users": "کاربرها",
  "rt_here": "این سرور",
  "rt_ports": "پورت‌های {ports}",
  "rt_main": "سرور اصلی",
  "rt_none": "ریله‌ای در حال استفاده نیست",
  "rt_conns": "تانل‌های باز",
  "t_now": "now",
  "a_rotate": "تغییر مسیر همین حالا",
  "a_pause": "توقف چرخش",
  "a_resume": "ادامه‌ی چرخش",
  "a_stop": "خاموش کردن تانل",
  "a_start": "روشن کردن تانل",
  "a_use": "استفاده از این",
  "d_relays": "ریله‌ها",
  "d_hist": "تاریخچه‌ی تغییر مسیر",
  "s_ok": "در دسترس",
  "s_down": "در دسترس نیست",
  "s_bad": "کنار گذاشته شد (خراب)",
  "s_unk": "چک نشده",
  "s_off": "غیرفعال",
  "s_cur": "در حال استفاده",
  "r_start": "شروع",
  "r_scheduled": "زمان‌بندی‌شده",
  "r_failover": "جایگزینی اضطراری",
  "r_manual": "دستی",
  "h_fail": "ناموفق",
  "w_noxray": "Xray توی {path} نصب نیست. overlay-panel-setup.sh رو اجرا کن (یا XRAY_BIN رو تنظیم کن).",
  "w_nomain": "آدرس سرور اصلی رو توی تنظیمات وارد کن.",
  "w_norelay": "حداقل یک ریله اضافه کن.",
  "w_stopped": "تانل خاموشه: کاربرها از این سرور نمی‌تونن وصل بشن.",
  "w_skipped": "این پورت‌ها توسط برنامه‌ی دیگه‌ای روی این سرور اشغالن و رد شدن: {ports}",
  "w_problem": "آخرین مشکل: {msg}",
  "need_xray": "Xray",
  "need_main": "آدرس سرور اصلی",
  "need_relay": "یک ریله‌ی فعال",
  "rl_title": "افزودن ریله",
  "rl_link_desc": "لینک vless:// که اسکریپت نصب ریله چاپ کرده رو اینجا بچسبون (REALITY روی TCP).",
  "rl_link": "لینک",
  "rl_import": "افزودن از لینک",
  "rl_manual": "افزودن دستی",
  "rl_name": "نام",
  "rl_host": "آدرس",
  "rl_port": "پورت",
  "rl_sni": "نام سرور (SNI)",
  "rl_uuid": "UUID",
  "rl_pbk": "کلید عمومی",
  "rl_sid": "Short ID",
  "rl_fp": "اثرانگشت",
  "rl_flow": "Flow",
  "rl_add": "افزودن",
  "rl_list": "ریله‌ها",
  "rl_test": "چک همه‌ی ریله‌ها",
  "rl_none": "هنوز ریله‌ای نیست",
  "rl_edit": "ویرایش",
  "rl_on": "فعال",
  "rl_off": "غیرفعال",
  "rl_del": "حذف",
  "rl_confirm_del": "ریله‌ی {name} حذف بشه؟",
  "lg_title": "لاگ Xray",
  "lg_refresh": "تازه‌سازی",
  "lg_empty": "هنوز لاگی نیست",
  "se_title": "تنظیمات تانل",
  "se_desc": "کاربرها روی پورت خودشون به این سرور وصل می‌شن و پنل هر اتصال رو از ریله‌ی فعال رد می‌کنه و به سرور اصلی می‌رسونه.",
  "se_main": "آدرس سرور اصلی (سرور OpenVPN / GassLight)",
  "se_main_hint": "ریله‌ها ترافیک رو به این آدرس تحویل می‌دن، پس باید از اونا در دسترس باشه.",
  "se_ports": "پورت‌های عبوری",
  "se_ports_hint": "هم UDP و هم TCP هر پورتی که اینجا بنویسی عبور می‌کنه. بازه و لیست مجازه (1195-1300 یا 1195-1300,2000). همون پورت‌هایی رو بذار که پنل اصلی به کاربرها می‌ده.",
  "se_listen": "آدرس listen",
  "se_listen_hint": "اگه 0.0.0.0 بمونه کاربرها روی همه‌ی آدرس‌های این سرور می‌تونن وصل بشن، یا یکی از IPهای همین سرور رو بنویس.",
  "f_se_listen": "آدرس listen نامعتبره (آدرس IPv4 بنویس)",
  "se_probe": "پورت تست مسیر روی سرور اصلی (۰ = خاموش)",
  "se_probe_hint": "بعد از هر تغییر مسیر، پنل از داخل تانل یه اتصال آزمایشی به این پورت روی سرور اصلی می‌زنه. پنل اصلی GassLight (پورت 8088) انتخاب خوبیه. اگه تست خراب بشه پنل فوراً از اون ریله میاد بیرون.",
  "se_rot": "چرخش خودکار مسیر",
  "se_rot_min": "تغییر مسیر هر چند دقیقه: حداقل",
  "se_rot_max": "حداکثر (بیشینه ۱۹)",
  "se_save": "ذخیره",
  "se_fw": "این پورت‌ها (UDP و TCP) رو توی فایروال باز کن: {ports}",
  "se_xver": "نسخه",
  "se_xnone": "نصب نیست",
  "se_xbin": "فایل اجرایی",
  "se_xmode": "نحوه‌ی اجرا",
  "se_apply": "اعمال همین حالا (ری‌استارت Xray)",
  "f_se_need": "برای روشن کردن تانل هنوز لازمه: {what}",
  "f_tn_started": "تانل در حال روشن شدنه",
  "f_tn_stopped": "تانل خاموش شد",
  "f_tn_notrunning": "تانل روشن نیست",
  "f_tn_norelay": "ریله‌ی سالم دیگه‌ای برای تغییر مسیر نیست",
  "f_tn_rotated": "مسیر به {name} عوض شد",
  "f_tn_fail": "تغییر مسیر ناموفق بود: {msg}",
  "f_tn_rot_on": "چرخش ادامه پیدا کرد",
  "f_tn_rot_off": "چرخش متوقف شد",
  "f_rl_bad_link": "لینک معتبر نیست: باید vless:// با security=reality و type=tcp باشه",
  "f_rl_bad_host": "آدرس ریله نامعتبره",
  "f_rl_bad_port": "پورت نامعتبره (۱ تا ۶۵۵۳۵)",
  "f_rl_bad_uuid": "UUID نامعتبره",
  "f_rl_bad_key": "کلید عمومی نامعتبره (۴۳ کاراکتر، base64url)",
  "f_rl_bad_sid": "Short ID نامعتبره (حداکثر ۱۶ کاراکتر هگز، تعداد زوج)",
  "f_rl_bad_sni": "نام سرور (SNI) نامعتبره",
  "f_rl_bad_fp": "اثرانگشت ناشناخته‌ست",
  "f_rl_bad_flow": "Flow ناشناخته‌ست",
  "f_rl_many": "تعداد ریله‌ها زیاده (حداکثر {max})",
  "f_rl_added": "ریله‌ی {name} اضافه شد",
  "f_rl_updated": "ریله ویرایش شد",
  "f_rl_deleted": "ریله حذف شد",
  "f_rl_toggled": "ریله به‌روز شد",
  "f_rl_missing": "ریله پیدا نشد",
  "f_rl_tested": "چک تموم شد",
  "f_se_main": "آدرس سرور اصلی نامعتبره",
  "f_se_ports": "پورت‌ها نامعتبرن: {why}",
  "f_se_rot": "چرخش باید دقیقه‌ی صحیح از ۱ تا ۱۹ باشه (حداقل نباید از حداکثر بیشتر باشه) و پورت تست هم معتبر",
  "f_se_saved": "تنظیمات ذخیره شد",
  "f_se_applied": "اعمال شد",
  "f_se_failed": "اعمال نشد: {msg}",
 },
}


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
                ic=icon, me=session.get("user"))


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


def jload(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def jsave(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


CFG_DEFAULT = {"enabled": False, "main_host": "", "ports": "1195-1300", "listen": "0.0.0.0", "probe_port": 8088,
               "rot_enabled": True, "rot_min": 8, "rot_max": 15}
CFG = dict(CFG_DEFAULT)
CFG.update(jload(CFG_FILE, {}))
RELAYS = jload(RELAYS_FILE, {"next_id": 1, "relays": []})
STATE = {"current": None, "since": 0, "next_switch": 0, "history": []}
STATE.update(jload(STATE_FILE, {}))
RSTATE = {}
RT = {"probe": {"ok": None, "ms": None, "ts": 0}, "probe_fails": 0, "skipped": [], "problem": None,
      "last_health": 0, "conns": (0, 0), "stopped_seen": False, "retry_at": 0}
WAKE = threading.Event()


def save_cfg():
    jsave(CFG_FILE, CFG)


def save_relays():
    jsave(RELAYS_FILE, RELAYS)


def save_state():
    jsave(STATE_FILE, STATE)


def get_relay(rid):
    return next((r for r in RELAYS["relays"] if r["id"] == rid), None)


def valid_host(h):
    if not HOST_RE.match(h or ""):
        return False
    if re.fullmatch(r"[0-9.]+", h):
        try:
            ipaddress.IPv4Address(h)
        except ValueError:
            return False
    return True


def reserved_ports():
    return {22, PORT, PROBE_LOCAL, PROBE_LOCAL + 1}


def parse_ports(text):
    out = set()
    for part in re.split(r"[,\s]+", (text or "").strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d{1,5})(?:-(\d{1,5}))?", part)
        if not m:
            raise ValueError(part)
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        if not 1 <= a <= b <= 65535:
            raise ValueError(part)
        if len(out) + (b - a + 1) > MAX_PORTS:
            raise ValueError(f"max {MAX_PORTS}")
        out.update(range(a, b + 1))
    if not out:
        raise ValueError("empty")
    bad = sorted(out & reserved_ports())
    if bad:
        raise ValueError("reserved " + ",".join(map(str, bad)))
    return sorted(out)


def ranges(ports):
    out, start, prev = [], None, None
    for p in sorted(ports):
        if start is None:
            start = prev = p
        elif p == prev + 1:
            prev = p
        else:
            out.append((start, prev))
            start = prev = p
    if start is not None:
        out.append((start, prev))
    return out


def ranges_text(ports):
    return ",".join(str(a) if a == b else f"{a}-{b}" for a, b in ranges(ports))


def relay_clean(d):
    host = (d.get("host") or "").strip()
    if not valid_host(host):
        return None, "f_rl_bad_host"
    try:
        port = int(str(d.get("port") or "443").strip())
    except ValueError:
        return None, "f_rl_bad_port"
    if not 1 <= port <= 65535:
        return None, "f_rl_bad_port"
    uuid = (d.get("uuid") or "").strip()
    if not UUID_RE.match(uuid):
        return None, "f_rl_bad_uuid"
    pbk = (d.get("public_key") or "").strip()
    if not PBK_RE.match(pbk):
        return None, "f_rl_bad_key"
    sid = (d.get("short_id") or "").strip()
    if not SID_RE.match(sid):
        return None, "f_rl_bad_sid"
    sni = (d.get("server_name") or "").strip()
    if not HOST_RE.match(sni) or "." not in sni:
        return None, "f_rl_bad_sni"
    fp = (d.get("fingerprint") or "chrome").strip()
    if fp not in FINGERPRINTS:
        return None, "f_rl_bad_fp"
    flow = (d.get("flow") if d.get("flow") is not None else "xtls-rprx-vision").strip()
    if flow not in FLOWS:
        return None, "f_rl_bad_flow"
    name = re.sub(r"[\x00-\x1f<>]", "", (d.get("name") or "").strip())[:40] or host
    return {"name": name, "host": host, "port": port, "uuid": uuid.lower(), "public_key": pbk, "short_id": sid.lower(),
            "server_name": sni, "fingerprint": fp, "flow": flow}, None


def parse_vless(link):
    try:
        u = urlsplit((link or "").strip())
        port = u.port or 443
    except ValueError:
        raise ValueError("link")
    if u.scheme != "vless" or not u.hostname or not u.username:
        raise ValueError("link")
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    if q.get("security") != "reality" or q.get("type", "tcp") != "tcp":
        raise ValueError("link")
    return {"name": unquote(u.fragment), "host": u.hostname, "port": port, "uuid": unquote(u.username),
            "public_key": q.get("pbk", ""), "short_id": q.get("sid", ""), "server_name": q.get("sni", ""),
            "fingerprint": q.get("fp", "chrome"), "flow": q.get("flow", "xtls-rprx-vision")}


def build_config(relay, ports):
    main = CFG["main_host"]
    inbounds = [{"tag": f"in-{p}", "listen": CFG["listen"], "port": p, "protocol": "dokodemo-door",
                 "settings": {"address": main, "port": p, "network": "tcp,udp"}} for p in ports]
    if CFG["probe_port"]:
        inbounds.append({"tag": "probe", "listen": "127.0.0.1", "port": PROBE_LOCAL, "protocol": "dokodemo-door",
                         "settings": {"address": main, "port": int(CFG["probe_port"]), "network": "tcp"}})
    user = {"id": relay["uuid"], "encryption": "none"}
    if relay["flow"]:
        user["flow"] = relay["flow"]
    return {
        "log": {"loglevel": "warning"},
        "inbounds": inbounds,
        "outbounds": [
            {"tag": "relay", "protocol": "vless",
             "settings": {"vnext": [{"address": relay["host"], "port": relay["port"], "users": [user]}]},
             "streamSettings": {"network": "tcp", "security": "reality",
                                "realitySettings": {"serverName": relay["server_name"], "fingerprint": relay["fingerprint"],
                                                    "publicKey": relay["public_key"], "shortId": relay["short_id"],
                                                    "spiderX": "/"}}},
            {"tag": "block", "protocol": "blackhole"},
        ],
    }


def sh(cmd, timeout=15):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return subprocess.CompletedProcess(cmd, 127, "", str(e))


class XrayCtl:
    proc = None

    def start_or_restart(self):
        if XRAY_MODE == "systemd":
            sh(["systemctl", "enable", XRAY_SERVICE])
            return sh(["systemctl", "restart", XRAY_SERVICE]).returncode == 0
        self.stop()
        try:
            out = open(XRAY_LOG, "ab")
            self.proc = subprocess.Popen([XRAY_BIN, "run", "-config", XRAY_CONF], stdout=out, stderr=out,
                                         start_new_session=True)
        except OSError:
            return False
        return True

    def stop(self):
        if XRAY_MODE == "systemd":
            sh(["systemctl", "disable", "--now", XRAY_SERVICE])
            return
        p, self.proc = self.proc, None
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(3)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(3)

    def active(self):
        if XRAY_MODE == "systemd":
            return sh(["systemctl", "is-active", "--quiet", XRAY_SERVICE]).returncode == 0
        return bool(self.proc and self.proc.poll() is None)

    def logs(self, n=150):
        if XRAY_MODE == "systemd":
            return sh(["journalctl", "-u", XRAY_SERVICE, "-n", str(n), "--no-pager", "-o", "short-iso"]).stdout
        try:
            with open(XRAY_LOG, "rb") as f:
                return b"".join(f.readlines()[-n:]).decode("utf-8", "replace")
        except OSError:
            return ""

    def version(self):
        r = sh([XRAY_BIN, "version"], 5)
        return (r.stdout.splitlines() or [""])[0].strip() if r.returncode == 0 else ""


XCTL = XrayCtl()
FW_DONE = set()


def ports_busy_by_others(ports, listen="0.0.0.0"):
    want, busy = set(ports), set()
    for line in sh(["ss", "-H", "-lntup"], 8).stdout.splitlines():
        p = line.split()
        if len(p) < 5 or '"xray"' in line:
            continue
        addr, _, port = p[4].rpartition(":")
        if not port.isdigit() or int(port) not in want:
            continue
        addr = addr.strip("[]")
        if listen == "0.0.0.0" or addr in ("*", "", "0.0.0.0", "::", listen):
            busy.add(int(port))
    return busy


def fw_open(ports):
    new = set(ports) - FW_DONE
    if not new or "Status: active" not in sh(["ufw", "status"], 8).stdout:
        FW_DONE.update(new)
        return
    for a, b in ranges(new):
        spec = f"{a}" if a == b else f"{a}:{b}"
        for proto in ("tcp", "udp"):
            sh(["ufw", "allow", f"{spec}/{proto}"], 20)
    FW_DONE.update(new)


def apply_xray(relay):
    with SWLOCK:
        try:
            ports = parse_ports(CFG["ports"])
        except ValueError as e:
            return False, f"ports: {e}"
        busy = ports_busy_by_others(ports, CFG["listen"])
        eff = [p for p in ports if p not in busy]
        RT["skipped"] = sorted(busy)
        if not eff:
            return False, "no usable port"
        if not os.path.exists(XRAY_BIN):
            return False, f"xray not found at {XRAY_BIN}"
        tmp = f"{XRAY_DIR}/config.new.json"
        with open(tmp, "w") as f:
            json.dump(build_config(relay, eff), f, indent=1)
        os.chmod(tmp, 0o600)
        t = sh([XRAY_BIN, "run", "-test", "-config", tmp], 25)
        if t.returncode != 0:
            os.remove(tmp)
            return False, "invalid config: " + (t.stdout + t.stderr).strip().splitlines()[-1][:160]
        prev = XRAY_CONF + ".prev"
        had = os.path.exists(XRAY_CONF)
        if had:
            shutil.copy2(XRAY_CONF, prev)
        os.replace(tmp, XRAY_CONF)
        ok = XCTL.start_or_restart()
        time.sleep(0.8)
        if not ok or not XCTL.active():
            if had:
                shutil.copy2(prev, XRAY_CONF)
                XCTL.start_or_restart()
            return False, "xray did not start"
        fw_open(eff)
        return True, ""


def tcp_ms(host, port, timeout=4):
    t0 = time.time()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return int((time.time() - t0) * 1000)
    except OSError:
        return None


def health_all():
    relays = list(RELAYS["relays"])
    if not relays:
        return
    with ThreadPoolExecutor(max_workers=min(8, len(relays))) as ex:
        res = list(ex.map(lambda r: tcp_ms(r["host"], r["port"]), relays))
    now = time.time()
    with LOCK:
        for r, ms in zip(relays, res):
            st = RSTATE.setdefault(r["id"], {"ok": None, "ms": None, "checked": 0, "fails": 0, "bad_until": 0})
            st.update(ok=ms is not None, ms=ms, checked=now)
            st["fails"] = 0 if ms is not None else st["fails"] + 1


def probe_enabled():
    return bool(CFG["probe_port"]) and bool(CFG["main_host"])


def probe_once(port=None, wait=3.5):
    t0 = time.time()
    try:
        s = socket.create_connection(("127.0.0.1", port or PROBE_LOCAL), timeout=3)
    except OSError:
        return False, None
    try:
        s.settimeout(wait)
        s.sendall(b"GET /login HTTP/1.0\r\nHost: probe\r\nConnection: close\r\n\r\n")
        try:
            d = s.recv(256)
        except socket.timeout:
            return True, None
        return (True, int((time.time() - t0) * 1000)) if d else (False, None)
    except OSError:
        return False, None
    finally:
        s.close()


def probe_record(wait=3.5):
    ok, ms = probe_once(None, wait)
    RT["probe"] = {"ok": ok, "ms": ms, "ts": time.time()}
    RT["probe_fails"] = 0 if ok else RT["probe_fails"] + 1
    return ok


def verify_path():
    for i in range(3):
        if probe_record(2.5):
            return True
        if i < 2:
            time.sleep(1.0)
    return False


def preflight(relay):
    if not (probe_enabled() and os.path.exists(XRAY_BIN)):
        return True
    with socket.socket() as tmp:
        tmp.bind(("127.0.0.1", 0))
        port = tmp.getsockname()[1]
    path = f"{XRAY_DIR}/preflight.{port}.json"
    conf = {"log": {"loglevel": "none"},
            "inbounds": [{"tag": "pf", "listen": "127.0.0.1", "port": port, "protocol": "dokodemo-door",
                          "settings": {"address": CFG["main_host"], "port": int(CFG["probe_port"]), "network": "tcp"}}],
            "outbounds": build_config(relay, [])["outbounds"][:1]}
    with open(path, "w") as f:
        json.dump(conf, f)
    os.chmod(path, 0o600)
    try:
        p = subprocess.Popen([XRAY_BIN, "run", "-config", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
    except OSError:
        return False
    deadline = time.time() + 12
    try:
        while time.time() < deadline:
            time.sleep(0.5)
            if p.poll() is not None:
                return False
            if probe_once(port, 2.5)[0]:
                return True
        return False
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(3)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(3)


def bad_now(rid):
    return RSTATE.get(rid, {}).get("bad_until", 0) > time.time()


def mark_bad(rid, secs):
    st = RSTATE.setdefault(rid, {"ok": None, "ms": None, "checked": 0, "fails": 0, "bad_until": 0})
    st["bad_until"] = time.time() + secs


def usable(r):
    st = RSTATE.get(r["id"], {})
    return r.get("active") and not bad_now(r["id"]) and st.get("fails", 0) < 2


def pick(exclude=()):
    cand = [r["id"] for r in RELAYS["relays"] if r["id"] not in exclude and usable(r)]
    return RNG.choice(cand) if cand else None


def schedule_next():
    lo, hi = int(CFG["rot_min"]), int(CFG["rot_max"])
    STATE["next_switch"] = time.time() + RNG.uniform(lo, hi) * ROT_UNIT


def history_add(entry):
    STATE["history"].append(entry)
    del STATE["history"][:-100]


def switch_to(rid, reason):
    with SWLOCK:
        relay = get_relay(rid)
        if not relay:
            return False
        prev = STATE["current"]
        if rid != prev and not preflight(relay):
            mark_bad(rid, 600)
            with LOCK:
                history_add({"ts": time.time(), "from": prev, "to": rid, "reason": reason, "ok": False,
                             "msg": "preflight failed"})
                save_state()
            RT["problem"] = "preflight failed: " + relay["name"]
            return False
        ok, msg = apply_xray(relay)
        entry = {"ts": time.time(), "from": prev, "to": rid, "reason": reason, "ok": ok, "msg": msg}
        if ok:
            with LOCK:
                STATE.update(current=rid, since=time.time())
                schedule_next()
            RT["problem"] = None
            if probe_enabled() and not verify_path():
                mark_bad(rid, 600)
                entry.update(ok=False, msg="path check failed")
            else:
                mark_ok = RSTATE.setdefault(rid, {"ok": None, "ms": None, "checked": 0, "fails": 0, "bad_until": 0})
                mark_ok["bad_until"] = 0
        else:
            mark_bad(rid, 300)
            RT["problem"] = msg
        if not ok:
            RT["retry_at"] = time.time() + 8
        with LOCK:
            history_add(entry)
            save_state()
        return entry["ok"]


def prerequisites():
    miss = []
    if not os.path.exists(XRAY_BIN):
        miss.append("xray")
    if not CFG["main_host"]:
        miss.append("main")
    if not any(r.get("active") for r in RELAYS["relays"]):
        miss.append("relay")
    return miss


def engine_tick():
    if not CFG["enabled"]:
        if not RT["stopped_seen"]:
            RT["stopped_seen"] = True
            XCTL.stop()
            with LOCK:
                STATE["current"] = None
                save_state()
        return
    RT["stopped_seen"] = False
    if prerequisites():
        return
    now = time.time()
    if now - RT["last_health"] >= HEALTH_EVERY:
        RT["last_health"] = now
        health_all()
        if STATE["current"] and probe_enabled() and XCTL.active():
            probe_record()
        c = STATE["current"]
        if c and (RSTATE.get(c, {}).get("fails", 0) > 0 or RT["probe_fails"] > 0):
            RT["last_health"] = now - HEALTH_EVERY + min(5, HEALTH_EVERY)
    if now < RT["retry_at"]:
        return
    cur = STATE["current"]
    r = get_relay(cur) if cur else None
    if not r or not r.get("active") or not XCTL.active():
        rid = cur if (r and r.get("active") and usable(r)) else pick()
        if rid:
            switch_to(rid, "start")
        return
    st = RSTATE.get(cur, {})
    if st.get("fails", 0) >= 2 or bad_now(cur) or RT["probe_fails"] >= 3:
        if RT["probe_fails"] >= 3:
            mark_bad(cur, 600)
        nxt = pick(exclude={cur})
        if nxt:
            RT["probe_fails"] = 0
            switch_to(nxt, "failover")
        return
    if CFG["rot_enabled"] and now >= STATE["next_switch"]:
        nxt = pick(exclude={cur})
        if nxt:
            switch_to(nxt, "scheduled")
        else:
            with LOCK:
                schedule_next()


def engine():
    if STATE["next_switch"] < time.time():
        schedule_next()
        STATE["next_switch"] = time.time() + RNG.uniform(30, 120)
    while True:
        WAKE.wait(1.5)
        WAKE.clear()
        try:
            engine_tick()
        except Exception as e:
            print("engine error:", e, flush=True)


def poke():
    WAKE.set()


def count_conns():
    r = get_relay(STATE["current"]) if STATE["current"] else None
    if not r:
        return 0
    now = time.time()
    if now - RT["conns"][0] < 4:
        return RT["conns"][1]
    n = 0
    for line in sh(["ss", "-H", "-tn", "state", "established"], 6).stdout.splitlines():
        p = line.split()
        if len(p) >= 4 and p[-1].endswith(f":{r['port']}") and p[-1].rsplit(":", 1)[0] == r["host"]:
            n += 1
    RT["conns"] = (now, n)
    return n


def status_json():
    now = time.time()
    with LOCK:
        cur = STATE["current"]
        names = {r["id"]: r["name"] for r in RELAYS["relays"]}
        relays = []
        for r in RELAYS["relays"]:
            st = RSTATE.get(r["id"], {})
            relays.append({"id": r["id"], "name": r["name"], "host": r["host"], "port": r["port"], "sni": r["server_name"],
                           "active": bool(r.get("active")), "ok": st.get("ok"), "ms": st.get("ms"),
                           "down": st.get("fails", 0) >= 2, "bad": bad_now(r["id"]), "current": r["id"] == cur})
        hist = [{"ts": h["ts"], "from": names.get(h["from"], "-") if h["from"] else "-", "to": names.get(h["to"], "?"),
                 "reason": h["reason"], "ok": h["ok"], "msg": h.get("msg", "")} for h in STATE["history"][-15:][::-1]]
        try:
            eff = ranges_text([p for p in parse_ports(CFG["ports"]) if p not in RT["skipped"]])
        except ValueError:
            eff = ""
        return {"now": now, "enabled": CFG["enabled"], "running": XCTL.active(), "current": cur,
                "current_name": names.get(cur) if cur else None, "since": STATE["since"],
                "next_switch": STATE["next_switch"] if (CFG["rot_enabled"] and cur) else None,
                "rot_enabled": CFG["rot_enabled"], "probe_on": probe_enabled(), "probe": RT["probe"],
                "relays": relays, "history": hist, "problem": RT["problem"], "skipped": RT["skipped"],
                "ports": eff, "main": CFG["main_host"], "conns": count_conns() if cur else 0}


NET = {"iface": None, "last": None, "cur": (0, 0), "rate": (0.0, 0.0), "hist": deque(maxlen=180)}
NET_DAYS = {}


def load_traffic():
    d = jload(TRAFFIC_FILE, {})
    NET_DAYS.update(d.get("net_days", {}))
    NET["last"] = d.get("net_last")


def save_traffic():
    with SLOCK:
        jsave(TRAFFIC_FILE, {"net_days": NET_DAYS, "net_last": NET["last"]})


def default_iface():
    try:
        with open("/proc/net/route") as f:
            for line in f.readlines()[1:]:
                p = line.split()
                if len(p) > 1 and p[1] == "00000000":
                    return p[0]
    except OSError:
        pass
    return None


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
        return sum(p), p[3] + p[4]
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


def sampler():
    tick, last_t, last_save = 0, time.time(), time.time()
    while True:
        time.sleep(1)
        now = time.time()
        dt = max(now - last_t, 0.001)
        last_t = now
        try:
            if tick % 60 == 0:
                NET["iface"] = default_iface()
            net_tick(dt)
            sys_tick()
            if now - last_save >= 30:
                save_traffic()
                last_save = now
        except Exception as e:
            print("sampler error:", e, flush=True)
        tick += 1


DUMMY_HASH = generate_password_hash("not-a-real-password")


def save_auth(a):
    jsave(AUTH_FILE, a)


def load_auth():
    if not os.path.exists(AUTH_FILE):
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


def page(body, **ctx):
    ctx.update(css=CSS, app=APP_NAME)
    return render_template_string(TOP + body + BOTTOM, **ctx)


def warnings():
    w = []
    miss = prerequisites()
    if "xray" in miss:
        w.append(tr("w_noxray", path=XRAY_BIN))
    if "main" in miss:
        w.append(tr("w_nomain"))
    if "relay" in miss:
        w.append(tr("w_norelay"))
    if CFG["enabled"] is False:
        w.append(tr("w_stopped"))
    if RT["skipped"]:
        w.append(tr("w_skipped", ports=",".join(map(str, RT["skipped"]))))
    if RT["problem"]:
        w.append(tr("w_problem", msg=RT["problem"]))
    return w


@app.route("/")
@login_required
def index():
    return page(DASH_HTML, active="dash", warns=warnings(), ports=CFG["ports"], main=CFG["main_host"])


@app.route("/api/status")
@login_required
def api_status():
    return jsonify(status_json())


@app.route("/relays")
@login_required
def relays_page():
    return page(RELAYS_HTML, active="relays", relays=RELAYS["relays"], fps=FINGERPRINTS, maxr=MAX_RELAYS)


@app.route("/network")
@login_required
def network():
    return page(NET_HTML, active="net")


@app.route("/log")
@login_required
def log_page():
    return page(LOG_HTML, active="log", text=XCTL.logs(150))


@app.route("/settings")
@login_required
def settings_page():
    try:
        ports = parse_ports(CFG["ports"])
        fw = ranges_text(ports)
    except ValueError:
        fw = CFG["ports"]
    return page(SET_HTML, active="set", cfg=CFG, fw=fw, xver=XCTL.version(), xbin=XRAY_BIN, xmode=XRAY_MODE,
                admins=[a["username"] for a in load_auth()["users"]], panel_port=PORT)


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


def back(to="index"):
    return redirect(url_for(to))


@app.route("/tunnel/toggle", methods=["POST"])
@login_required
def tunnel_toggle():
    want = request.form.get("enabled") == "1"
    if want:
        miss = prerequisites()
        if miss:
            flash(tr("f_se_need", what=", ".join(tr("need_" + m) for m in miss)), "err")
            return back()
    with LOCK:
        CFG["enabled"] = want
        save_cfg()
    RT["stopped_seen"] = False if want else RT["stopped_seen"]
    poke()
    flash(tr("f_tn_started") if want else tr("f_tn_stopped"), "ok")
    return back()


@app.route("/tunnel/rotate", methods=["POST"])
@login_required
def tunnel_rotate():
    cur = STATE["current"]
    nxt = pick(exclude={cur} if cur else ())
    if not CFG["enabled"] or prerequisites():
        flash(tr("f_tn_notrunning"), "err")
    elif not nxt:
        flash(tr("f_tn_norelay"), "err")
    elif switch_to(nxt, "manual"):
        flash(tr("f_tn_rotated", name=get_relay(nxt)["name"]), "ok")
    else:
        flash(tr("f_tn_fail", msg=RT["problem"] or "path check failed"), "err")
    return back()


@app.route("/tunnel/rotation", methods=["POST"])
@login_required
def tunnel_rotation():
    with LOCK:
        CFG["rot_enabled"] = request.form.get("enabled") == "1"
        save_cfg()
        if CFG["rot_enabled"]:
            schedule_next()
    flash(tr("f_tn_rot_on") if CFG["rot_enabled"] else tr("f_tn_rot_off"), "ok")
    return back()


def relay_add(clean):
    with LOCK:
        if len(RELAYS["relays"]) >= MAX_RELAYS:
            return False
        RELAYS["relays"].append({"id": RELAYS["next_id"], **clean, "active": True})
        RELAYS["next_id"] += 1
        save_relays()
    return True


@app.route("/relays/add-link", methods=["POST"])
@login_required
def relay_add_link():
    try:
        d = parse_vless(request.form.get("link", ""))
    except ValueError:
        flash(tr("f_rl_bad_link"), "err")
        return back("relays_page")
    clean, err = relay_clean(d)
    if err:
        flash(tr(err), "err")
    elif not relay_add(clean):
        flash(tr("f_rl_many", max=MAX_RELAYS), "err")
    else:
        flash(tr("f_rl_added", name=clean["name"]), "ok")
        poke()
    return back("relays_page")


@app.route("/relays/add", methods=["POST"])
@login_required
def relay_add_manual():
    clean, err = relay_clean(request.form)
    if err:
        flash(tr(err), "err")
    elif not relay_add(clean):
        flash(tr("f_rl_many", max=MAX_RELAYS), "err")
    else:
        flash(tr("f_rl_added", name=clean["name"]), "ok")
        poke()
    return back("relays_page")


def relay_changed(rid):
    if STATE["current"] == rid and CFG["enabled"]:
        r = get_relay(rid)
        if r and r.get("active"):
            switch_to(rid, "manual")
        else:
            STATE["current"] = None
            poke()


@app.route("/relays/<int:rid>/update", methods=["POST"])
@login_required
def relay_update(rid):
    r = get_relay(rid)
    if not r:
        flash(tr("f_rl_missing"), "err")
        return back("relays_page")
    clean, err = relay_clean(request.form)
    if err:
        flash(tr(err), "err")
        return back("relays_page")
    with LOCK:
        r.update(clean)
        save_relays()
        RSTATE.pop(rid, None)
    relay_changed(rid)
    flash(tr("f_rl_updated"), "ok")
    return back("relays_page")


@app.route("/relays/<int:rid>/toggle", methods=["POST"])
@login_required
def relay_toggle(rid):
    r = get_relay(rid)
    if not r:
        flash(tr("f_rl_missing"), "err")
        return back("relays_page")
    with LOCK:
        r["active"] = not r.get("active")
        save_relays()
    relay_changed(rid)
    flash(tr("f_rl_toggled"), "ok")
    return back("relays_page")


@app.route("/relays/<int:rid>/delete", methods=["POST"])
@login_required
def relay_delete(rid):
    r = get_relay(rid)
    if not r:
        flash(tr("f_rl_missing"), "err")
        return back("relays_page")
    with LOCK:
        RELAYS["relays"].remove(r)
        save_relays()
        RSTATE.pop(rid, None)
    relay_changed(rid)
    flash(tr("f_rl_deleted"), "ok")
    return back("relays_page")


@app.route("/relays/<int:rid>/use", methods=["POST"])
@login_required
def relay_use(rid):
    r = get_relay(rid)
    if not r or not r.get("active"):
        flash(tr("f_rl_missing"), "err")
    elif not CFG["enabled"] or prerequisites():
        flash(tr("f_tn_notrunning"), "err")
    elif switch_to(rid, "manual"):
        flash(tr("f_tn_rotated", name=r["name"]), "ok")
    else:
        flash(tr("f_tn_fail", msg=RT["problem"] or "path check failed"), "err")
    return back(request.form.get("next") if request.form.get("next") in ("index", "relays_page") else "index")


@app.route("/relays/test", methods=["POST"])
@login_required
def relays_test():
    health_all()
    flash(tr("f_rl_tested"), "ok")
    return back("relays_page")


@app.route("/settings/save", methods=["POST"])
@login_required
def settings_save():
    f = request.form
    main = f.get("main_host", "").strip()
    if main and not valid_host(main):
        flash(tr("f_se_main"), "err")
        return back("settings_page")
    listen = f.get("listen", "0.0.0.0").strip() or "0.0.0.0"
    try:
        ipaddress.IPv4Address(listen)
    except ValueError:
        flash(tr("f_se_listen"), "err")
        return back("settings_page")
    try:
        parse_ports(f.get("ports", ""))
    except ValueError as e:
        flash(tr("f_se_ports", why=str(e)), "err")
        return back("settings_page")
    try:
        lo, hi = int(f.get("rot_min", "")), int(f.get("rot_max", ""))
        probe = int(f.get("probe_port", "0") or 0)
    except ValueError:
        flash(tr("f_se_rot"), "err")
        return back("settings_page")
    if not (1 <= lo <= hi <= ROT_MAX_MIN) or not 0 <= probe <= 65535:
        flash(tr("f_se_rot"), "err")
        return back("settings_page")
    with LOCK:
        CFG.update(main_host=main, ports=re.sub(r"\s+", "", f.get("ports", "")), listen=listen, probe_port=probe,
                   rot_min=lo, rot_max=hi, rot_enabled=f.get("rot_enabled") == "1")
        save_cfg()
        schedule_next()
    flash(tr("f_se_saved"), "ok")
    if CFG["enabled"] and not prerequisites() and STATE["current"]:
        if not switch_to(STATE["current"], "manual"):
            flash(tr("f_se_failed", msg=RT["problem"] or "path check failed"), "err")
    poke()
    return back("settings_page")


@app.route("/settings/apply", methods=["POST"])
@login_required
def settings_apply():
    cur = STATE["current"]
    if not CFG["enabled"] or prerequisites() or not cur:
        flash(tr("f_tn_notrunning"), "err")
    elif switch_to(cur, "manual"):
        flash(tr("f_se_applied"), "ok")
    else:
        flash(tr("f_se_failed", msg=RT["problem"] or "path check failed"), "err")
    return back("settings_page")


@app.route("/settings/account", methods=["POST"])
@login_required
def set_account():
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
    return back("settings_page")


@app.route("/settings/users/add", methods=["POST"])
@login_required
def set_user_add():
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
    return back("settings_page")


@app.route("/settings/users/delete/<uname>", methods=["POST"])
@login_required
def set_user_delete(uname):
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
    return back("settings_page")


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
.brand{display:flex;align-items:center;gap:10px;font-size:19px;font-weight:800;color:var(--tx);text-decoration:none}
.brand em{font-style:normal;background:linear-gradient(135deg,var(--p1),var(--p2));-webkit-background-clip:text;
background-clip:text;color:transparent}
.logo{width:38px;height:38px;border-radius:12px;display:grid;place-items:center;flex:none;color:#fff;
background:linear-gradient(135deg,var(--p1),var(--p2));box-shadow:0 8px 24px rgba(109,124,255,.4)}
.side{position:fixed;top:0;right:0;bottom:0;width:var(--side);display:flex;flex-direction:column;gap:6px;
padding:18px 14px;background:var(--nav);border-left:1px solid var(--bd);z-index:30;overflow-y:auto;
backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px)}
.side-brand{margin:2px 8px 18px}
.links{display:flex;flex-direction:column;gap:4px}
.links a{display:flex;align-items:center;gap:12px;padding:11px 14px;border-radius:12px;color:var(--mut);
text-decoration:none;font-size:14px;font-weight:500;transition:.2s}
.links a:hover{color:var(--tx);background:var(--inp)}
.links a.on{color:#fff;background:linear-gradient(135deg,var(--p1),var(--p2));box-shadow:0 6px 18px rgba(109,124,255,.35)}
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
.portal{display:flex;flex-direction:column;gap:8px;margin:0 0 12px;padding:10px 12px;border-radius:12px;background:var(--inp)}
.prow{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.plabel{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--mut);margin-inline-end:auto}
.pw{font-weight:700;letter-spacing:.5px;user-select:all;font-size:14px}
.portal form{margin:0;display:flex}
.btn.ico{padding:8px 10px}
.btn.done{background:rgba(52,211,153,.18)!important;color:var(--ok)!important;border-color:rgba(52,211,153,.4)!important}
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
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:18px;align-items:start}
.chk{display:flex;gap:10px;align-items:flex-start;margin:12px 0 4px;font-size:13px;color:var(--tx);cursor:pointer;line-height:1.5}
.chk input{width:18px;height:18px;margin-top:2px;flex:none;accent-color:var(--p1)}
.hint{color:var(--mut);font-size:12px;margin:0 0 14px;padding-inline-start:28px}
.alist{display:flex;flex-direction:column;gap:8px}
.arow{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:10px;padding:10px 12px;border-radius:12px;background:var(--inp)}
.arow>span{display:flex;align-items:center;gap:8px;min-width:0;word-break:break-all}
.dlg{border:1px solid var(--bd);border-radius:20px;background:var(--navs);color:var(--tx);padding:0;
width:calc(100% - 28px);max-width:540px;box-shadow:0 30px 80px rgba(0,0,0,.5)}
.dlg::backdrop{background:rgba(5,8,20,.6);backdrop-filter:blur(4px)}
.dlg-in{padding:22px}
.dlg h3{margin:0 0 16px;display:flex;align-items:center;gap:8px;font-size:17px;flex-wrap:wrap}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px}.span2{grid-column:1/-1}
.dwarn{display:flex;gap:8px;align-items:flex-start;margin:14px 0;font-size:12px;color:var(--warn);line-height:1.6}
.dacts{display:flex;gap:8px;flex-wrap:wrap}.dacts .btn{flex:1 1 auto}
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

select,textarea{width:100%;padding:11px 13px;border-radius:12px;border:1px solid var(--bd);background:var(--inp);color:var(--tx);font:inherit;outline:none;transition:.2s}
select:focus,textarea:focus{border-color:var(--p1);box-shadow:0 0 0 3px rgba(109,124,255,.25)}
textarea{resize:vertical;min-height:90px;font-size:13px;direction:ltr}
option{background:var(--bg);color:var(--tx)}
.brand .tag{margin-inline-start:2px}
.route{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin:6px 0 16px}
.node{display:inline-flex;flex-direction:column;gap:2px;padding:10px 14px;border-radius:14px;background:var(--inp);border:1px solid var(--bd);font-size:13px;min-width:0;max-width:100%}
.node b{font-size:14px;word-break:break-all}.node small{color:var(--mut);font-size:11px}
.node.on{border-color:rgba(52,211,153,.5);background:rgba(52,211,153,.08)}.node.bad{border-color:rgba(251,113,133,.5);background:rgba(251,113,133,.08)}
.arr{color:var(--mut);display:inline-flex}[dir=rtl] .arr svg{transform:scaleX(-1)}
details>summary{list-style:none}details>summary::-webkit-details-marker{display:none}
.hist .arow{font-size:13px}
@media(max-width:760px){.node{flex:1 1 100%}.arr{display:none}}
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
<a class="brand" href="{{ url_for('index') }}"><div class="logo">{{ ic('shield',20) }}</div><span>Gass<em>Light</em></span><span class="tag">Tunnel</span></a>
<button class="burger" id="bg" type="button" aria-label="{{ t('menu') }}" aria-expanded="false"><span></span><span></span><span></span></button>
</header>
<aside class="side" id="menu">
<a class="brand side-brand" href="{{ url_for('index') }}"><div class="logo">{{ ic('shield',20) }}</div><span>Gass<em>Light</em></span><span class="tag">Tunnel</span></a>
<nav class="links">
<a href="{{ url_for('index') }}" class="{{ 'on' if active=='dash' }}">{{ ic('home') }}<span>{{ t('nav_dash') }}</span></a>
<a href="{{ url_for('relays_page') }}" class="{{ 'on' if active=='relays' }}">{{ ic('server') }}<span>{{ t('nav_relays') }}</span></a>
<a href="{{ url_for('network') }}" class="{{ 'on' if active=='net' }}">{{ ic('wifi') }}<span>{{ t('nav_net') }}</span></a>
<a href="{{ url_for('log_page') }}" class="{{ 'on' if active=='log' }}">{{ ic('terminal') }}<span>{{ t('nav_log') }}</span></a>
<a href="{{ url_for('settings_page') }}" class="{{ 'on' if active=='set' }}">{{ ic('sliders') }}<span>{{ t('nav_set') }}</span></a>
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
window.addEventListener('resize',function(){if(window.innerWidth>980)set(false)});})();
</script></body></html>"""

DASH_HTML = """
{% for w in warns %}<div class="warnbox">{{ ic('alert',18) }}<span>{{ w }}</span></div>{% endfor %}

<div class="stats">
<div class="stat"><b id="s-state">-</b><span>{{ t('st_tunnel') }}</span></div>
<div class="stat"><b id="s-relay" style="font-size:17px;word-break:break-all">-</b><span>{{ t('st_relay') }}</span></div>
<div class="stat"><b id="s-next">-</b><span>{{ t('st_next') }}</span></div>
<div class="stat"><b id="s-lat">-</b><span>{{ t('st_probe') }}</span></div>
</div>

<div class="card"><h3>{{ ic('shuffle') }} {{ t('rt_title') }} <small id="s-conns"></small></h3>
<div class="route">
<span class="node"><b>{{ t('rt_users') }}</b><small>OpenVPN UDP / TCP</small></span><span class="arr">{{ ic('arrow-right',18) }}</span>
<span class="node"><b>{{ t('rt_here') }}</b><small class="mono">{{ t('rt_ports', ports=ports) }}</small></span><span class="arr">{{ ic('arrow-right',18) }}</span>
<span class="node" id="n-relay"><b id="n-relay-name">-</b><small class="mono" id="n-relay-host">&nbsp;</small></span><span class="arr">{{ ic('arrow-right',18) }}</span>
<span class="node"><b>{{ t('rt_main') }}</b><small class="mono">{{ main or '-' }}</small></span>
</div>
<div class="acts">
<form method="post" action="{{ url_for('tunnel_rotate') }}"><button class="btn">{{ ic('shuffle',16) }} {{ t('a_rotate') }}</button></form>
<form method="post" action="{{ url_for('tunnel_rotation') }}" id="f-rot"><input type="hidden" name="enabled" id="f-rot-v" value="0"><button class="btn ghost" id="b-rot">{{ ic('pause',16) }} <span>{{ t('a_pause') }}</span></button></form>
<form method="post" action="{{ url_for('tunnel_toggle') }}" id="f-run"><input type="hidden" name="enabled" id="f-run-v" value="0"><button class="btn red" id="b-run">{{ ic('power',16) }} <span>{{ t('a_stop') }}</span></button></form>
</div></div>

<div class="card"><h3>{{ ic('server') }} {{ t('d_relays') }}</h3>
<div class="alist" id="rl"></div></div>

<div class="card"><h3>{{ ic('clock') }} {{ t('d_hist') }}</h3>
<div class="alist hist" id="hs"></div></div>

<script>
var I={{ jt('v_running','v_stopped','v_paused','v_ok','v_fail','v_off','s_ok','s_down','s_bad','s_unk','s_off','s_cur','a_use','a_pause','a_resume','a_stop','a_start','r_start','r_scheduled','r_failover','r_manual','h_fail','rt_none','rl_none','rt_conns','t_now')|tojson }};
var $=function(i){return document.getElementById(i)},skew=0,last=null;
function el(tag,cls,txt){var e=document.createElement(tag);if(cls)e.className=cls;if(txt!=null)e.textContent=txt;return e}
function mmss(s){s=Math.max(0,Math.round(s));var m=Math.floor(s/60),r=s%60;return m+':'+(r<10?'0':'')+r}
function when(ts){var d=new Date(ts*1000);return d.toLocaleTimeString()}
function useForm(id){var f=el('form');f.method='post';f.action='/relays/'+id+'/use';f.style.margin='0';
 var n=el('input');n.type='hidden';n.name='next';n.value='index';f.appendChild(n);
 var b=el('button','btn ghost sm',I.a_use);f.appendChild(b);return f}
function render(d){
 last=d;skew=Date.now()/1000-d.now;
 $('s-state').textContent=d.enabled?(d.running?I.v_running:I.v_stopped):I.v_stopped;
 $('s-relay').textContent=d.current_name||'-';
 $('s-lat').textContent=!d.probe_on?I.v_off:(d.probe.ok===null?'-':(d.probe.ok?(d.probe.ms!=null?d.probe.ms+' ms':I.v_ok):I.v_fail));
 $('n-relay-name').textContent=d.current_name||I.rt_none;
 var cur=d.relays.filter(function(r){return r.current})[0];
 $('n-relay-host').textContent=cur?cur.host+':'+cur.port:'\u00a0';
 $('n-relay').className='node'+(d.current&&d.running?(d.probe.ok===false?' bad':' on'):'');
 $('s-conns').textContent=d.current?I.rt_conns+': '+d.conns:'';
 $('f-rot-v').value=d.rot_enabled?'0':'1';
 $('b-rot').lastElementChild.textContent=d.rot_enabled?I.a_pause:I.a_resume;
 $('f-run-v').value=d.enabled?'0':'1';
 $('b-run').lastElementChild.textContent=d.enabled?I.a_stop:I.a_start;
 var rl=$('rl');rl.textContent='';
 if(!d.relays.length)rl.appendChild(el('div','empty',I.rl_none));
 d.relays.forEach(function(r){
  var row=el('div','arow'),l=el('span'),a=el('span','mono',r.host+':'+r.port),nm=el('b',null,r.name);
  var left=el('span');left.style.cssText='display:flex;flex-direction:column;align-items:flex-start;gap:2px';
  left.appendChild(nm);left.appendChild(a);
  var right=el('span');right.style.cssText='display:flex;gap:8px;align-items:center;flex-wrap:wrap';
  if(r.ms!=null)right.appendChild(el('span','muted',r.ms+' ms')).style.margin='0';
  var st=!r.active?['pill',I.s_off]:(r.current?['pill on',I.s_cur]:(r.down?['tag bad',I.s_down]:(r.bad?['tag bad',I.s_bad]:(r.ok===true?['pill on',I.s_ok]:['pill',I.s_unk]))));
  right.appendChild(el('span',st[0],st[1]));
  if(r.active&&!r.current&&d.enabled)right.appendChild(useForm(r.id));
  row.appendChild(left);row.appendChild(right);rl.appendChild(row)});
 var hs=$('hs');hs.textContent='';
 if(!d.history.length)hs.appendChild(el('div','empty','-'));
 d.history.forEach(function(h){
  var row=el('div','arow'),left=el('span');left.style.cssText='display:flex;gap:8px;align-items:center;flex-wrap:wrap';
  left.appendChild(el('span','muted',when(h.ts))).style.margin='0';
  left.appendChild(el('span','mono',h.from+' \u2192 '+h.to));
  var right=el('span');right.style.cssText='display:flex;gap:8px;align-items:center';
  right.appendChild(el('span','tag',I['r_'+h.reason]||h.reason));
  if(!h.ok)right.appendChild(el('span','tag bad',I.h_fail+(h.msg?': '+h.msg:'')));
  row.appendChild(left);row.appendChild(right);hs.appendChild(row)});
}
function tickClock(){
 if(!last)return;var now=Date.now()/1000-skew;
 $('s-next').textContent=last.next_switch?mmss(last.next_switch-now):(last.rot_enabled?'-':I.v_paused);
}
function poll(){fetch('/api/status').then(function(r){if(r.status===401){location='/login';return null}return r.json()})
 .then(function(d){if(d)render(d);tickClock()}).catch(function(){})}
poll();setInterval(poll,3000);setInterval(tickClock,1000);
</script>
"""

RELAYS_HTML = """
<div class="card"><h3>{{ ic('link') }} {{ t('rl_title') }}</h3>
<p class="muted">{{ t('rl_link_desc') }}</p>
<form method="post" action="{{ url_for('relay_add_link') }}" class="stack">
<div><label>{{ t('rl_link') }}</label><textarea name="link" class="mono" spellcheck="false" autocomplete="off" placeholder="vless://UUID@1.2.3.4:443?security=reality&amp;sni=...&amp;pbk=...&amp;sid=...&amp;fp=chrome&amp;flow=xtls-rprx-vision#name" required></textarea></div>
<div><button class="btn">{{ ic('plus',16) }} {{ t('rl_import') }}</button></div></form>
<details style="margin-top:16px"><summary class="muted" style="cursor:pointer">{{ ic('edit',14) }} {{ t('rl_manual') }}</summary>
<form method="post" action="{{ url_for('relay_add_manual') }}" class="stack" style="margin-top:12px">
<div class="grid2">
<div><label>{{ t('rl_name') }}</label><input name="name" autocomplete="off" placeholder="de-1"></div>
<div><label>{{ t('rl_host') }}</label><input name="host" autocomplete="off" placeholder="203.0.113.10" required></div>
<div><label>{{ t('rl_port') }}</label><input name="port" type="number" min="1" max="65535" value="443" required></div>
<div><label>{{ t('rl_sni') }}</label><input name="server_name" autocomplete="off" placeholder="www.example.com" required></div>
<div class="span2"><label>{{ t('rl_uuid') }}</label><input name="uuid" class="mono" autocomplete="off" required></div>
<div class="span2"><label>{{ t('rl_pbk') }}</label><input name="public_key" class="mono" autocomplete="off" required></div>
<div><label>{{ t('rl_sid') }}</label><input name="short_id" class="mono" autocomplete="off"></div>
<div><label>{{ t('rl_fp') }}</label><select name="fingerprint">{% for f in fps %}<option value="{{ f }}">{{ f }}</option>{% endfor %}</select></div>
<div class="span2"><label>{{ t('rl_flow') }}</label><select name="flow"><option value="xtls-rprx-vision">xtls-rprx-vision</option><option value="">-</option></select></div>
</div>
<div><button class="btn">{{ ic('plus',16) }} {{ t('rl_add') }}</button></div></form></details></div>

<div class="card"><h3>{{ ic('server') }} {{ t('rl_list') }} <small>{{ relays|length }} / {{ maxr }}</small></h3>
<div class="row" style="margin-bottom:12px"><form method="post" action="{{ url_for('relays_test') }}" style="margin:0"><button class="btn ghost sm">{{ ic('refresh',14) }} {{ t('rl_test') }}</button></form></div>
<div class="alist">
{% for r in relays %}
<div class="arow" style="flex-wrap:wrap;gap:10px">
<span style="flex-direction:column;align-items:flex-start;gap:2px"><b>{{ r.name }}</b><span class="mono muted" style="margin:0">{{ r.host }}:{{ r.port }} &middot; {{ r.server_name }}</span></span>
<span style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
<span class="rs pill" data-id="{{ r.id }}">-</span>
<form method="post" action="{{ url_for('relay_use', rid=r.id) }}" style="margin:0"><input type="hidden" name="next" value="relays_page"><button class="btn ghost sm">{{ t('a_use') }}</button></form>
<form method="post" action="{{ url_for('relay_toggle', rid=r.id) }}" style="margin:0"><button class="btn ghost sm">{{ t('rl_off') if r.active else t('rl_on') }}</button></form>
<form method="post" action="{{ url_for('relay_delete', rid=r.id) }}" style="margin:0" onsubmit='return confirm({{ t("rl_confirm_del", name=r.name)|tojson }})'><button class="btn red sm" title="{{ t('rl_del') }}">{{ ic('trash',14) }}</button></form>
</span>
<details style="flex-basis:100%"><summary class="muted" style="cursor:pointer">{{ ic('edit',14) }} {{ t('rl_edit') }}</summary>
<form method="post" action="{{ url_for('relay_update', rid=r.id) }}" class="stack" style="margin-top:10px">
<div class="grid2">
<div><label>{{ t('rl_name') }}</label><input name="name" value="{{ r.name }}" autocomplete="off"></div>
<div><label>{{ t('rl_host') }}</label><input name="host" value="{{ r.host }}" autocomplete="off" required></div>
<div><label>{{ t('rl_port') }}</label><input name="port" type="number" min="1" max="65535" value="{{ r.port }}" required></div>
<div><label>{{ t('rl_sni') }}</label><input name="server_name" value="{{ r.server_name }}" autocomplete="off" required></div>
<div class="span2"><label>{{ t('rl_uuid') }}</label><input name="uuid" class="mono" value="{{ r.uuid }}" autocomplete="off" required></div>
<div class="span2"><label>{{ t('rl_pbk') }}</label><input name="public_key" class="mono" value="{{ r.public_key }}" autocomplete="off" required></div>
<div><label>{{ t('rl_sid') }}</label><input name="short_id" class="mono" value="{{ r.short_id }}" autocomplete="off"></div>
<div><label>{{ t('rl_fp') }}</label><select name="fingerprint">{% for f in fps %}<option value="{{ f }}" {% if f==r.fingerprint %}selected{% endif %}>{{ f }}</option>{% endfor %}</select></div>
<div class="span2"><label>{{ t('rl_flow') }}</label><select name="flow"><option value="xtls-rprx-vision" {% if r.flow %}selected{% endif %}>xtls-rprx-vision</option><option value="" {% if not r.flow %}selected{% endif %}>-</option></select></div>
</div>
<div><button class="btn sm">{{ ic('check',14) }} {{ t('se_save') }}</button></div></form></details>
</div>
{% else %}<div class="empty">{{ t('rl_none') }}</div>{% endfor %}
</div></div>
<script>
var I={{ jt('s_ok','s_down','s_bad','s_unk','s_off','s_cur')|tojson }};
function poll(){fetch('/api/status').then(function(r){if(r.status===401){location='/login';return null}return r.json()}).then(function(d){
 if(!d)return;d.relays.forEach(function(r){
  var e=document.querySelector('.rs[data-id="'+r.id+'"]');if(!e)return;
  var st=!r.active?['pill',I.s_off]:(r.current?['pill on',I.s_cur]:(r.down?['tag bad',I.s_down]:(r.bad?['tag bad',I.s_bad]:(r.ok===true?['pill on',I.s_ok]:['pill',I.s_unk]))));
  e.className='rs '+st[0];e.textContent=st[1]+(r.ms!=null?' \u00b7 '+r.ms+' ms':'')})}).catch(function(){})}
poll();setInterval(poll,5000);
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

LOG_HTML = """
<div class="card"><h3>{{ ic('terminal') }} {{ t('lg_title') }}
<small><a class="btn ghost sm" href="{{ url_for('log_page') }}">{{ ic('refresh',14) }} {{ t('lg_refresh') }}</a></small></h3>
<pre class="mono" style="margin:0;white-space:pre-wrap;word-break:break-all;font-size:12px;max-height:560px;overflow:auto;direction:ltr;text-align:left">{{ text if text.strip() else t('lg_empty') }}</pre>
</div>
"""

SET_HTML = """
<div class="card"><h3>{{ ic('sliders') }} {{ t('se_title') }}</h3>
<p class="muted">{{ t('se_desc') }}</p>
<form method="post" action="{{ url_for('settings_save') }}" class="stack">
<div><label>{{ t('se_main') }}</label><input name="main_host" class="mono" value="{{ cfg.main_host }}" autocomplete="off" placeholder="203.0.113.5">
<p class="hint" style="padding-inline-start:0;margin:6px 0 0">{{ t('se_main_hint') }}</p></div>
<div><label>{{ t('se_ports') }}</label><input name="ports" class="mono" value="{{ cfg.ports }}" autocomplete="off" required>
<p class="hint" style="padding-inline-start:0;margin:6px 0 0">{{ t('se_ports_hint') }}</p></div>
<div><label>{{ t('se_listen') }}</label><input name="listen" class="mono" value="{{ cfg.listen }}" autocomplete="off">
<p class="hint" style="padding-inline-start:0;margin:6px 0 0">{{ t('se_listen_hint') }}</p></div>
<div><label>{{ t('se_probe') }}</label><input name="probe_port" type="number" min="0" max="65535" value="{{ cfg.probe_port }}">
<p class="hint" style="padding-inline-start:0;margin:6px 0 0">{{ t('se_probe_hint') }}</p></div>
<label class="chk"><input type="checkbox" name="rot_enabled" value="1" {% if cfg.rot_enabled %}checked{% endif %}><span>{{ t('se_rot') }}</span></label>
<div class="grid2">
<div><label>{{ t('se_rot_min') }}</label><input name="rot_min" type="number" min="1" max="19" value="{{ cfg.rot_min }}" required></div>
<div><label>{{ t('se_rot_max') }}</label><input name="rot_max" type="number" min="1" max="19" value="{{ cfg.rot_max }}" required></div>
</div>
<p class="hint" style="padding-inline-start:0">{{ ic('alert',14) }} {{ t('se_fw', ports=fw) }}</p>
<div><button class="btn">{{ ic('check',16) }} {{ t('se_save') }}</button></div></form></div>

<div class="card"><h3>{{ ic('cpu') }} Xray</h3>
<div class="kv">
<div><span class="k">{{ t('se_xver') }}</span><span class="v mono">{{ xver or t('se_xnone') }}</span></div>
<div><span class="k">{{ t('se_xbin') }}</span><span class="v mono">{{ xbin }}</span></div>
<div><span class="k">{{ t('se_xmode') }}</span><span class="v mono">{{ xmode }}</span></div></div>
<form method="post" action="{{ url_for('settings_apply') }}"><button class="btn ghost">{{ ic('refresh',16) }} {{ t('se_apply') }}</button></form></div>

<div class="two-col">
<div class="card"><h3>{{ ic('key') }} {{ t('adv_title_acc') }}</h3>
<p class="muted">{{ t('adv_acc_desc') }}</p>
<form method="post" action="{{ url_for('set_account') }}" class="stack">
<div><label>{{ t('adv_username') }}</label><input name="username" value="{{ me }}" pattern="[A-Za-z0-9_.-]{3,32}" autocomplete="username" autocapitalize="none" required></div>
<div><label>{{ t('adv_cur_pw') }}</label><input type="password" name="current" autocomplete="current-password" required></div>
<div><label>{{ t('adv_new_pw') }}</label><input type="password" name="password" minlength="6" autocomplete="new-password"></div>
<div><label>{{ t('adv_rep_pw') }}</label><input type="password" name="password2" minlength="6" autocomplete="new-password"></div>
<button class="btn">{{ ic('check',16) }} {{ t('adv_save') }}</button></form></div>

<div class="card"><h3>{{ ic('users') }} {{ t('adv_title_users') }}</h3>
<p class="muted">{{ t('adv_users_desc') }}</p>
<div class="alist">
{% for a in admins %}<div class="arow"><span>{{ ic('user',16) }}<span class="mono">{{ a }}</span>{% if a == me %}<span class="tag">{{ t('adv_you') }}</span>{% endif %}</span>
{% if a != me %}<form method="post" action="{{ url_for('set_user_delete', uname=a) }}" style="margin:0"
onsubmit='return confirm({{ t("adv_del_confirm", name=a)|tojson }})'><button class="btn red sm">{{ ic('trash',14) }}</button></form>{% endif %}</div>
{% endfor %}</div>
<h4>{{ ic('user-plus',16) }} {{ t('adv_add_title') }}</h4>
<form method="post" action="{{ url_for('set_user_add') }}" class="stack">
<div><label>{{ t('adv_username') }}</label><input name="username" pattern="[A-Za-z0-9_.-]{3,32}" autocomplete="off" autocapitalize="none" required></div>
<div><label>{{ t('adv_password') }}</label><input type="password" name="password" minlength="6" autocomplete="new-password" required></div>
<button class="btn">{{ ic('plus',16) }} {{ t('adv_add_btn') }}</button></form></div>
</div>
"""


if __name__ == "__main__":
    load_auth()
    load_traffic()
    threading.Thread(target=sampler, daemon=True).start()
    threading.Thread(target=engine, daemon=True).start()
    app.run(host=HOST, port=PORT, threaded=True)
