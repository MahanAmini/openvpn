<div align="center">
<h1>OpenVPN</h1>
<h4>⚡GassLight</h4>
</div>
<p align="center">
  <a href="README.fa.md"><img alt="Language: Persian" src="https://img.shields.io/badge/Language-Persian-2ea44f?style=for-the-badge"></a>
  <a href="README.md"><img alt="Language: English" src="https://img.shields.io/badge/Language-English-6d7cff?style=for-the-badge"></a>
</p>

<p align="center">English | <a href="README.fa.md">فارسی</a></p>

---

> **One command installs an OpenVPN server, a web admin panel and a user portal. Every VPN user gets their own port.**

GassLight turns a fresh Ubuntu/Debian server into a small VPN service you can manage from a browser: create users, give each one a dedicated port and protocol, limit speed, data and time, watch live traffic and CPU/RAM, back everything up, and hand each user a private page where they can see their usage and download their config file.

---

## 📌 Quick Links

- [Install](#install)
- [Features](#features)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [After the install](#after-the-install)
- [The admin panel](#the-admin-panel)
- [The user portal](#the-user-portal)
- [Command line: gasslight](#command-line-gasslight)
- [Ports and firewall](#ports-and-firewall)
- [Security notes and HTTPS](#security-notes-and-https)
- [Updating and uninstalling](#updating-and-uninstalling)
- [Troubleshooting](#troubleshooting)
- [Project layout](#project-layout)
- [Copyright and license](#copyright-and-license)

---

## Features

### 👑 For you (the admin)

- **One-command installation** of everything: OpenVPN core, panel, portal and a management command.
- **One user = one OpenVPN instance**: its own port, protocol (TCP or UDP), certificate, tunnel device and private subnet.
- Per-user **speed limit**, **daily and total data limits**, and **expiry in days** (expired users are removed and their certificate is revoked automatically).
- Automatic **ID and port** selection, with gaps left by deleted users filled first. Ports can also be typed manually and are checked live.
- **Live monitoring**: server download/upload speed, CPU, RAM and swap, plus per-user online state, local VPN IP, last connection and usage.
- **Backup and import** of all users, usage and certificates in a single zip file.
- **Panel users**: several admins, each with their own username and password.
- English interface by default with a one-click **Persian (فارسی)** switch, dark/light theme, desktop sidebar and mobile menu.

### 👤 For your users

- A private link to a personal page: status, time left, usage, limits, connection details.
- The `.ovpn` file stays **locked behind an 8-character password** that you generate and send to them yourself.

---

## How it works

```text
                     ┌──────────────────────────── your server ────────────────────────────┐
  user A  ──UDP 1195──►  openvpn-server@alice   tun · 10.101.1.0/24 · own certificate      │
  user B  ──TCP 1196──►  openvpn-server@bob     tun · 10.101.2.0/24 · own certificate      │
  old clients ─UDP 1194─►  main OpenVPN (shared core)                                      │
                     │                                                                     │
  admin   ──TCP 8088──►  GassLight panel (Flask, root)  ◄── 127.0.0.1 internal API ──┐     │
  users   ──TCP 8089──►  user portal (separate process, user "nobody") ───────────────┘     │
                     └─────────────────────────────────────────────────────────────────────┘
```

**One port per user.** OpenVPN serves one port per process, so GassLight creates a dedicated OpenVPN service for each user instead of putting everyone into one shared configuration. For every user the panel:

1. Issues a **personal certificate** (Easy-RSA) and a personal `.ovpn` file.
2. Writes a server configuration with a **unique port, protocol and /24 subnet** and starts it as the systemd service `openvpn-server@<name>`.
3. Restricts that instance to the user's own certificate (`ccd-exclusive`), so a certificate from one user cannot connect through another user's port.
4. Adds firewall rules for the subnet (NAT, forwarding) and opens the port in `ufw` if it is active.
5. Opens a private OpenVPN **management socket** so the panel can read exact, live per-session byte counters and the real online state.
6. Applies the speed limit with `tc` on the instance's tunnel device (download shaping and upload policing).

Because users are separated like this, creating, editing or deleting one user never restarts or disturbs another, and each user's traffic is counted separately.

**What is shared:** the server's public IP, bandwidth, CPU and RAM. A user's config works on one device at a time (a second connection with the same certificate replaces the first).

**Online state and usage** come from OpenVPN's own per-session counters, polled every 2 seconds. A user counts as *online* only while their device keeps sending data to the server (45-second window), so a vanished device does not stay "online".

---

## Requirements

| Requirement | Specification |
|---|---|
| **OS** | Ubuntu 22.04+ or Debian 11+ (with systemd) |
| **Access** | root (`sudo`) |
| **Network** | a public IP address, the TUN device enabled (`/dev/net/tun`) |
| **Resources** | 512 MB RAM is enough to start; every user instance uses roughly 10–20 MB |
| **Cloud firewall** | you must open the ports listed in [Ports and firewall](#ports-and-firewall) |

---

## Install

Run these two commands on the server (as a user with `sudo`):

```bash
wget https://raw.githubusercontent.com/USERNAME/openvpn/main/install.sh -O install.sh
sudo bash install.sh
```

> ⚠️ **Note:** Download first, then run. Do **not** use `curl ... | bash`: the OpenVPN core setup asks a few questions (server IP, protocol, port, DNS, first client name) and needs a real terminal. Press Enter to accept the defaults.

The installer:

1. checks the system and installs only the missing packages,
2. installs the OpenVPN core if it is not installed yet (you answer its questions),
3. installs the panel and the portal and creates their systemd services,
4. generates a **random admin password**,
5. opens the panel and portal ports in `ufw` if it is active,
6. installs the `gasslight` management command and prints your login details.

### Installer options

| Option | Meaning | Default |
|---|---|---|
| `--panel-port N` | port of the admin panel | `8088` |
| `--portal-port N` | port of the user portal | `8089` |
| `--local-only` | panel listens on `127.0.0.1` only (use an SSH tunnel) | off |
| `--repo user/repo` | GitHub repository to download from | the project repo |
| `--ref NAME` | branch or tag to install | `main` |
| `-h`, `--help` | show help | |

**Example:** `sudo bash install.sh --panel-port 9443 --portal-port 9444`

Environment variables with the same meaning also work: `PANEL_PORT`, `PORTAL_PORT`, `PANEL_HOST`, `GASSLIGHT_REPO`, `GASSLIGHT_REF`.

---

## After the install

At the end the installer prints something like this:

```text
 Panel     :  http://YOUR_SERVER_IP:8088
 Username  :  gasslight
 Password  :  <random 16 characters>
 Portal    :  http://YOUR_SERVER_IP:8089
```

| Detail | Information |
|---|---|
| **Default username** | `gasslight` |
| **Password** | random, generated during the install and **shown only once** — save it |
| **Forgot it?** | `sudo gasslight reset-password` prints a new one |
| **Change it** | Panel → **Advanced → My account** (you can change both the username and the password) |

### Quick start:

1. Open `http://YOUR_SERVER_IP:8088` and sign in.
2. Open the cloud-firewall ports (see [Ports and firewall](#ports-and-firewall)).
3. **Dashboard → Create new user**, pick a name, protocol and limits, press *Create*.
4. On the new user's card press **Copy link** and send it to the user, together with their **download password** (shown on the same card).

The first VPN client created by the OpenVPN core setup is saved as `/root/<name>.ovpn`. You can move it to its own port from the dashboard (see *Adopt existing clients* below).

---

## The admin panel

### Dashboard

- **Summary cards**: total users, active users, users expiring within 3 days, users without expiry.
- **Create new user**

  | Field | Details |
  |---|---|
  | ID | assigned automatically (smallest free number) |
  | Name | letters, digits, `-` and `_`, up to 32 characters |
  | Protocol | **UDP** or **TCP** |
  | Port | automatic (smallest free port above 1194) or **manual**, checked live; ports 22, 1194 and the panel port are reserved |
  | Duration (days) | `0` = unlimited, up to 3650; expired users are removed automatically |
  | Max speed (Mbps) | `0` = unlimited; enforced for download and upload |
  | Daily limit (GB) | `0` = none; the user is blocked when reached and unblocked at the start of the next day (server time) |
  | Total limit (GB) | `0` = none; the user is blocked until you raise the limit or reset their usage |

- **User cards** show status, ID, port, protocol, speed and limits, time left (with a progress bar), usage, and these actions:

  | Action | What it does |
  |---|---|
  | **Download .ovpn** | downloads the user's config file |
  | **Edit** | change ID, port, protocol, speed and limits; if the new configuration fails to start, the old one is restored automatically. After changing ID, port or protocol the old `.ovpn` stops working — send the new file |
  | **Extend** | adds days to the expiry |
  | **Remove expiry** | makes the user unlimited in time |
  | **Reset usage** | zeroes the usage counters (inside the Edit dialog) |
  | **Delete** | stops the service, removes its files and firewall rules and revokes the certificate |
  | **Download password / Copy link / Open page / New password / New link** | manage the user's portal access |

- **Search** users by name or port.
- **Adopt existing clients**: clients that were created by the OpenVPN core setup on the main port can be moved into their own instance and port from the dashboard.

### Network Usage

Live server monitoring, refreshed every second:

- download and upload speed (with a 3-minute live chart),
- **CPU** (usage, cores, load average) and **RAM** (used/total, swap) gauges and chart,
- traffic today, this month, since boot, and the 3-minute peak.

### User Usage

One card per user, refreshed every 2 seconds:

- **online / offline**, **local VPN IP** (for example `10.101.1.2`), **last connection** time,
- creation date, download, upload, total and today's usage,
- progress bars for the daily and total limits, share of total traffic, live speed.

### Backup

- **Create backup**: downloads a zip with all users and settings, usage totals and the full certificate store.
- **Import backup**: restores users from such a zip. Users whose name already exists are skipped, IDs/ports are kept when free (otherwise the next free ones are used), expired users are skipped, and `.ovpn` files are rebuilt with this server's address — so it also works for moving to a new server. If the backup was made with a different certificate authority you must tick **Restore certificates**; a safety copy of the current certificates is saved first.
- Portal links and download passwords are kept by an import.

> 🔒 A backup contains **private keys** (including the CA key). Store it safely.

### Advanced

- **My account**: change your username and password.
- **Panel users**: add or remove other admins (several people can manage the server, each with their own login).

### Interface

English by default with a **فارسی** switch (right-to-left layout), automatic dark/light theme, a sidebar on desktop and a drop-down menu on phones.

---

## The user portal

Every user has a private link, for example `http://YOUR_SERVER_IP:8089/u/<long-random-token>`. Copy it from the user's card in the panel. The page (English or Persian, switchable) shows:

- online/offline state, last connection and local VPN IP,
- server address, protocol, port and speed limit,
- creation date, expiry date and time left,
- download, upload, total and today's usage with the daily and total limits,
- a **locked download** for the `.ovpn` file: the user must type the **download password** you gave them.

**Download password:** each user gets a random 8-character password (upper- and lowercase letters, digits and symbols; look-alike characters such as `0/O` and `1/l` are avoided) generated by the panel and shown on their card. Send it to the user yourself, through any channel you prefer. Both the password and the link can be regenerated at any time; the old ones stop working immediately.

**Protection:** after 5 wrong passwords a link is locked for 5 minutes, and after 12 wrong attempts from one IP address that IP is locked for 10 minutes. The portal runs as a separate low-privilege process (`nobody`) on its own port, holds no data and no keys, and only talks to the panel through a localhost-only internal API protected by a shared secret.

---

## Command line: gasslight

The installer adds a management command:

| Command | What it does |
|---|---|
| `gasslight info` | shows the panel and portal addresses |
| `gasslight status` | status of the panel and portal services |
| `gasslight restart` | restarts the panel and the portal |
| `gasslight logs [N]` | last N log lines (default 100) |
| `gasslight reset-password [user]` | sets a new random password (default user: `gasslight`) and prints it |
| `gasslight update` | updates to the latest version from GitHub (data is kept) |
| `gasslight uninstall [--purge]` | removes the panel and portal; `--purge` also deletes panel data |

Most commands need `sudo`.

Useful service names: `ovpn-panel` (admin panel), `gasslight-portal` (portal), `openvpn-server@<username>` (one per user).

---

## Ports and firewall

Open these in your **cloud provider's firewall / security group** (and in `ufw` if you use it; the installer does the `ufw` part for the panel and portal ports):

| Port | Protocol | Purpose |
|---|---|---|
| 8088 | TCP | admin panel (or the port you chose) |
| 8089 | TCP | user portal (or the port you chose) |
| 1194 | UDP | OpenVPN core (or the port you chose during the setup) |
| each user's port | UDP or TCP | one port per user — open the range you plan to use, e.g. `1195–1299` |

---

## Security notes and HTTPS

- The panel runs with root privileges and plain HTTP by default. Use a **strong password**, and prefer `--local-only` with an SSH tunnel (`ssh -L 8088:127.0.0.1:8088 user@SERVER`) or put it behind an HTTPS reverse proxy.
- To serve the portal over HTTPS from a domain, put it behind a reverse proxy (for example Caddy or Nginx) and tell the panel the public address so copied links are correct:

  ```bash
  sudo systemctl edit ovpn-panel
  ```
  ```ini
  [Service]
  Environment=PORTAL_BASE_URL=https://vpn.example.com
  ```
  and, for the portal behind your own proxy:
  ```bash
  sudo systemctl edit gasslight-portal
  ```
  ```ini
  [Service]
  Environment=TRUST_PROXY=1
  ```
  then `sudo gasslight restart`. (Settings made with `systemctl edit` survive updates.)
- Admin passwords are stored as hashes. Users' **download passwords are stored in plain text** inside the panel's data folder (so you can see them); the folder is readable by root only. Backups contain private keys.
- The panel data lives in `/opt/ovpn-panel` (mode `700`).

---

## Updating and uninstalling

**Update** (users, ports and passwords are kept):

```bash
sudo gasslight update
```

*(or run `sudo bash install.sh` again)*. The first start after updating from an older version adds a management socket to existing user instances and restarts each of them once; clients reconnect within seconds.

**Uninstall** the panel and portal (your users' VPN services keep running):

```bash
sudo gasslight uninstall           # keeps panel data
sudo gasslight uninstall --purge   # also deletes panel data
```

---

## Troubleshooting

| Problem | What to check |
|---|---|
| **Panel does not open** | open TCP 8088 in the cloud firewall; `sudo gasslight status`; `sudo gasslight logs` |
| **Forgot the password** | `sudo gasslight reset-password` |
| **Portal does not start** | `journalctl -u gasslight-portal -n 30`; make sure the panel was started once (it creates the shared secret) |
| **A user cannot connect** | open that user's port (and protocol) in the cloud firewall; `systemctl status openvpn-server@<username>` |
| **Speed limit not applied** | `tc` is part of `iproute2` (`sudo apt install iproute2`); the panel re-applies limits every 30 seconds |
| **User shows "Blocked"** | a data limit was reached — raise it in *Edit*, or use *Reset usage* |
| **`.ovpn` stopped working** | its ID, port or protocol was changed — send the newly downloaded file |
| **The installer says it needs a terminal** | download the script first (`wget`), then run it with `sudo bash install.sh` |

---

## Project layout

```text
openvpn/
├── install.sh              # one-command installer
├── core/                   # OpenVPN core setup used by the installer
├── panel/panel.py          # admin panel (Flask, single file)
└── portal/portal.py        # user portal (Flask, single file)
```

### Installed on the server:

| Path | Content |
|---|---|
| `/opt/ovpn-panel/` | panel code and all its data (users, usage, `.ovpn` files, backups) |
| `/opt/gasslight-portal/` | portal code |
| `/etc/gasslight/` | install settings and the shared portal secret |
| `/etc/openvpn/server/` | OpenVPN configuration, certificates and per-user instances |
| `/usr/local/bin/gasslight` | management command |

---

## Copyright and license

Copyright (c) 2026 [YOUR NAME]. All rights reserved by the author except as granted below.

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, subject to the following condition: the above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

You are responsible for using this software in accordance with the laws that apply to you and to your server provider.

*OpenVPN is a registered trademark of OpenVPN Inc. GassLight is an independent project and is not affiliated with or endorsed by OpenVPN Inc.*
