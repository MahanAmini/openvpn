<div align="center">
<h1>OpenVPN</h1>
<h4>⚡GassLight</h4>
</div>

<p align="center">
  <a href="README.fa.md"><img alt="Language: Persian" src="https://img.shields.io/badge/Language-Persian-2ea44f?style=for-the-badge"></a>
  <a href="README.md"><img alt="Language: English" src="https://img.shields.io/badge/Language-English-6d7cff?style=for-the-badge"></a>
</p>

<p align="center"><a href="README.fa.md">Persian</a> | English</p>

---

> **With a single command, an OpenVPN server, a web admin panel, and a dedicated user portal are installed. Each VPN user gets their very own user account.**
>
> On top of that, using the two scripts bundled with the project you can set up an **Iran tunnel** (without installing anything on the Iranian server) and connect your own **Exit Servers** in different countries to the panel, so users' exit IP rotates automatically.

GassLight turns a freshly installed Ubuntu/Debian server into a complete VPN service that you manage from your browser: creating users, assigning a dedicated port and protocol to each, setting speed, traffic and time limits, watching live traffic and CPU/RAM status, taking backups, and a personal page for each user with a password-protected `.ovpn` file download link.

---

## Table of Contents

- [Installation](#installation)
- [Iran Tunnel: Setup Tunnel](#iran-tunnel-setup-tunnel)
- [Exit Server: Setup Exit Servers](#exit-server-setup-exit-servers)
- [Proxy Routing](#proxy-routing)
- [Features](#features)
- [How It Works (Architecture)](#how-it-works-architecture)
- [Requirements](#requirements)
- [After Installation](#after-installation)
- [Admin Panel](#admin-panel)
- [User Portal](#user-portal)
- [Command Line: gasslight](#command-line-gasslight)
- [Ports and Firewall](#ports-and-firewall)
- [Security Notes and HTTPS](#security-notes-and-https)
- [Exit Servers](#exit-servers)
- [Address Inside .ovpn Files](#address-inside-ovpn-files)
- [Update and Uninstall](#update-and-uninstall)
- [Troubleshooting](#troubleshooting)
- [Project Structure](#project-structure)
- [Copyright and License](#copyright-and-license)

---

## Installation

Run these two commands on the server (as a user with `sudo` access):

```bash
wget https://raw.githubusercontent.com/mahanamini/openvpn/main/install.sh -O install.sh
sudo bash install.sh
```

> ⚠️ **Note:** Download first, then run. Do **not** use `curl ... | bash`; the OpenVPN core section asks a few questions (server IP, protocol, port, DNS, first client name) and needs a real terminal. Press Enter to accept the default settings.

The installer does the following:

1. Checks the system and installs only the missing packages.
2. Installs the OpenVPN core if it is not already installed (you answer its questions).
3. Installs the panel and portal and creates their systemd services.
4. Generates a **random password** for the admin.
5. Opens the panel and portal ports in `ufw` if it is active.
6. Installs the `gasslight` management command and prints the login info.

### Installation Options

| Option | Meaning | Default |
|---|---|---|
| `--panel-port N` | Admin panel port | `8088` |
| `--portal-port N` | User portal port | `8089` |
| `--local-only` | Panel listens only on `127.0.0.1` (use an SSH tunnel) | off |
| `--repo user/repo` | GitHub repository to download from | this project's repo |
| `--ref NAME` | Branch or tag to install | `main` |
| `-h`, `--help` | Show help | |

**Example:** `sudo bash install.sh --panel-port 9443 --portal-port 9444`

Environment variables with the same meaning also work: `PANEL_PORT`, `PORTAL_PORT`, `PANEL_HOST`, `GASSLIGHT_REPO`, `GASSLIGHT_REF`.

---

## Features

### 👑 For You (Admin)

- **One-command installation**: OpenVPN core, panel, portal and a management command.
- **One user = one OpenVPN instance**: dedicated port, protocol (TCP or UDP), certificate, tunnel and subnet.
- **Speed**, **daily and total traffic**, and **per-day expiration** limits for each user (expired users are automatically removed and their certificates revoked).
- Automatic **ID and port** selection that first fills the gaps left by deleted users; manual port entry with live validation.
- **Live monitoring**: server download/upload speed, CPU, RAM and swap, each user's online status, VPN local IP, last connection and usage.
- **Backup and restore** of all users, usage and certificates in a single zip file.
- **Multiple admins**: several panel users, each with their own username and password.
- Persian/English UI switchable with one click, dark/light theme, desktop sidebar and mobile menu.
- **Exit IP rotation** with three methods: public proxies, your own Exit Servers (SOCKS5), and smart address rewriting in `.ovpn` files.

### 👤 For Your Users

- A personal link to a personal page: status, time remaining, usage, limits and connection details.
- The `.ovpn` file is locked behind an **8-character download password** that you generate and send to them yourself.

---

## How It Works (Architecture)

```text
                     ┌──────────────────────────── your server ───────────────────────────┐
  User A  ──UDP 1195──►  openvpn-server@alice   tun · 10.101.1.0/24 · dedicated cert      │
  User B  ──TCP 1196──►  openvpn-server@bob     tun · 10.101.2.0/24 · dedicated cert      │
  legacy clients ─UDP 1194─►  main OpenVPN (shared core)                                 │
                     │                                                                    │
  Admin   ──TCP 8088──►  GassLight panel (Flask, root)  ◄── 127.0.0.1 internal API ──┐   │
  Users   ──TCP 8089──►  user portal (separate process, user nobody) ──────────────┘   │
                     └────────────────────────────────────────────────────────────────────┘
```

**One port per user.** A single OpenVPN process can only serve one port, so instead of putting everyone in a shared config, the panel creates a separate OpenVPN instance for each user. For each user the panel:

1. Issues a **personal certificate** (Easy-RSA) and a personal `.ovpn` file.
2. Writes a server config with a **unique port, protocol and /24 subnet** and brings it up as a systemd service named `openvpn-server@<name>`.
3. Restricts that instance to that user's certificate (`ccd-exclusive`), so one user's certificate cannot pass through another user's port.
4. Adds the subnet firewall rules (NAT, FORWARD) and opens the port in `ufw` if it is active.
5. Opens a private **management socket** so the panel can read the exact, live per-session byte counters and real online status.
6. Applies the speed limit with `tc` on that user's tunnel (shaping download and policing upload).

That is why creating, editing or deleting one user never disconnects another, and each user's usage is tracked separately.

**Shared resources:** the server's public IP, bandwidth, CPU and RAM. Each user's config works on one device at a time (a second connection with the same certificate replaces the first).

**Online status and usage** are read from OpenVPN's own counters every 2 seconds. A user is only counted as "online" when their device is actually sending data to the server (45-second window), so a device that suddenly disconnects does not stay "online".

### Exit IP Rotation Architecture (Proxy Routing and Exit Servers)

Both features ride on an internal "transparent relay" inside the panel that works like this:

```text
user VPN browser ──TCP──► panel server ──iptables REDIRECT──► local relay (127.0.0.1:8091)
                                                          │
                                                          ├─► selected SOCKS5/4 or HTTP CONNECT proxy ──► internet
                                                          └─► (only if fallback is on) direct connection
```

- Using `iptables` rules in the `nat` chain, the panel redirects users' VPN TCP traffic (except traffic to the server itself) to a **local relay port** (`8091`).
- The panel's internal relay (a multithreaded TCP server in Python) accepts the connection, picks a proxy/exit server according to the rotation mode (sticky / random / round-robin), performs the proxy handshake (SOCKS5 with authentication, SOCKS4 or HTTP CONNECT) and then passes data both ways.
- This relay **only accepts connections coming from users' VPN subnets**; no one from outside can connect to it, and when neither feature is enabled it is not open at all.
- **Only TCP** goes through the proxy; UDP (DNS, calls, games) still exits via your own server. With the "Block QUIC" option, UDP port 443 is dropped so the browser falls back to TCP and goes through the proxy.
- Proxy/exit server health is checked every 30 seconds so a dead one is quickly skipped and a recovered one rejoins rotation.
- Important note: while this feature is active, **the panel must stay up**; if you stop the panel, users have no internet until it comes back. If all proxies/exit servers are down and fallback is off, connections will not be established.

---

## Requirements

| Item | Description |
|---|---|
| **Operating system** | Ubuntu 22.04+ or Debian 11+ (with systemd) |
| **Access** | root (`sudo`) |
| **Network** | A public IP, TUN device available (`/dev/net/tun`) |
| **Resources** | 512 MB RAM is enough to start; each user instance consumes about 10 to 20 MB |
| **Cloud firewall** | You must open the ports listed in [Ports and Firewall](#ports-and-firewall) |

---

## After Installation

At the end of the installation, something like this is printed:

```text
 Panel     :  http://YOUR_SERVER_IP:8088
 Username  :  gasslight
 Password  :  <random 16 characters>
 Portal    :  http://YOUR_SERVER_IP:8089
```

| Detail | Description |
|---|---|
| **Default username** | `gasslight` |
| **Password** | Random, generated at install time and **shown only once** — save it |
| **Forgot it?** | `sudo gasslight reset-password` prints a new password |
| **Change password** | Panel → **Advanced → My Account** (you can change both username and password) |

### Quick Start:

1. Open `http://YOUR_SERVER_IP:8088` and log in.
2. Open the firewall ports in your cloud provider's firewall (see [Ports and Firewall](#ports-and-firewall)).
3. **Dashboard → Create New User**, pick a name, protocol and limits, and hit *Create*.
4. On the user's card click **Copy link** and send the link along with the **download password** (shown on the same card) to the user.

The first VPN client created during the OpenVPN core installation is saved at `/root/<name>.ovpn` and you can migrate it to its own port from the dashboard (see *Adding existing clients* below).

---

## Admin Panel

### Dashboard

- **Summary cards**: total users, active users, users nearing expiration (less than 3 days), users with no expiration.
- **Create New User**

  | Field | Details |
  |---|---|
  | ID | Automatic (lowest free number) |
  | Name | English letters, digits, `-` and `_`, up to 32 characters |
  | Protocol | **UDP** or **TCP** |
  | Port | Automatic (lowest free port above 1194) or **manual**, with live validation; ports 22, 1194 and the panel port are reserved |
  | Duration (days) | `0` = unlimited, max 3650; expired users are automatically removed |
  | Max speed (Mbps) | `0` = unlimited; applied to both download and upload |
  | Daily cap (GB) | `0` = no cap; the user is blocked until the next day (server time) when reached |
  | Total cap (GB) | `0` = no cap; the user stays blocked until you raise the cap or reset their usage |

- **User cards** show status, ID, port, protocol, speed and limits, time remaining (with progress bar), usage, and these actions:

  | Action | What it does |
  |---|---|
  | **Download .ovpn** | Download the user's config file |
  | **Edit** | Change ID, port, protocol, speed and caps; if the new config fails to come up, it automatically rolls back to the previous settings. After changing ID, port or protocol, the old `.ovpn` file stops working — send the new file |
  | **Extend** | Add days to the expiration |
  | **Remove expiry** | Make the user unlimited in time |
  | **Reset usage** | Zero out the usage counters (inside the Edit dialog) |
  | **Delete** | Stop the service, remove files and firewall rules and revoke the certificate |
  | **Download password / Copy link / Open page / New password / New link** | Manage the user's portal access |

- **Search** users by name or port.
- **Add existing clients**: clients created by the original OpenVPN installer on the main port can be migrated to a separate instance with their own port. The same certificate is used, so the old file keeps working on port 1194 too.

### Network Usage

Live server monitoring, updated every second:

- Download and upload speed (with a live 3-minute chart),
- **CPU** (usage, core count, load average) and **RAM** (used/total, swap) with gauges and charts,
- Traffic today, this month, since boot and the 3-minute peak.

### User Usage

One card per user, updated every 2 seconds:

- **Online/Offline**, **VPN local IP** (e.g. `10.101.1.2`), **last connection**,
- Creation date, download, upload, total and today's usage,
- Progress bars for daily and total caps, share of total traffic, instantaneous speed.

### Backup

- **Create backup**: downloads a zip containing all users and settings, total usage, and all certificates and keys.
- **Restore backup**: restores users from such a file. Users with existing names are skipped, IDs/ports are preserved if free (otherwise the nearest free value is used) and expired users are skipped. `.ovpn` files are rebuilt with this server's address, so it also works for **migrating to a new server**. If the backup was made with a different CA, tick **restore certificates**; a safe copy of current certificates is saved first.
- Portal links and download passwords are preserved in backups.

> 🔒 The backup contains **private keys** (including the CA key). Keep it somewhere safe.

### Advanced

This is where the panel's three new features and account settings live:

| Subpage | Description |
|---|---|
| **My account** | Change your own username and password; add/remove other admins |
| **Proxy routing** | Route users' TCP traffic through SOCKS4/5 or HTTP proxies |
| **Exit servers & .ovpn address** | Your own SOCKS5 exit servers + the address written inside `.ovpn` files |

These three sections are fully explained [below](#proxy-routing).

### User Interface

English by default; an **فارسی (Persian)** button switches to a right-to-left layout, with automatic dark/light theme, sidebar on desktop and drawer menu on mobile.

---

## User Portal

Each user has a private link, e.g. `http://YOUR_SERVER_IP:8089/u/<long-random-token>`. Copy it from the user's card in the panel. The page (Persian or English, switchable) shows:

- Online/Offline status, last connection and VPN local IP,
- Server address, protocol, port and speed limit,
- Creation date, expiration date and time remaining,
- Download, upload, total and today's usage along with the caps,
- **Locked download** of the `.ovpn` file: the user must enter the **download password**.

**Download password:** each user gets a random 8-character password (upper and lowercase letters, digits and symbols; lookalike characters like `0/O` and `1/l` are excluded) that the panel generates and shows on their card. You send it to the user yourself, through any channel you like. You can regenerate both the password and the link whenever you want; the old ones stop working instantly.

**Protection:** after 5 wrong passwords the link is locked for 5 minutes, and after 12 wrong attempts from one IP that IP is locked for 10 minutes. The portal runs as a separate low-privilege process (user `nobody`) on its own port, holds no data or keys, and only talks to the panel through an internal local API (localhost only) with a shared secret.

---

## Command Line: gasslight

The installer adds this management command:

| Command | What it does |
|---|---|
| `gasslight info` | Show panel and portal addresses |
| `gasslight status` | Panel and portal service status |
| `gasslight restart` | Restart panel and portal |
| `gasslight logs [N]` | Last N log lines (default 100) |
| `gasslight reset-password [user]` | Set a new random password (default for `gasslight`) and print it |
| `gasslight update` | Update to the latest version from GitHub (data is preserved) |
| `gasslight uninstall [--purge]` | Remove panel and portal; `--purge` also wipes panel data |

Most commands need `sudo`.

Useful service names: `ovpn-panel` (panel), `gasslight-portal` (portal), `openvpn-server@<username>` (one per user).

---

## Ports and Firewall

Open these ports in your provider's **cloud firewall** (and in `ufw` if you use it; the installer opens the panel and portal ports in `ufw`):

| Port | Protocol | Purpose |
|---|---|---|
| 8088 | TCP | Admin panel (or the port you chose) |
| 8089 | TCP | User portal (or the port you chose) |
| 1194 | UDP | Main OpenVPN (or the port chosen during installation) |
| Each user's port | UDP or TCP | One port per user — open the range you want to use, e.g. `1195–1299` |

> The local relay port (`8091`) must **not** be opened; it only answers VPN users' internal traffic and is not usable from outside in any way.

---

## Security Notes and HTTPS

- By default the panel runs with root privileges over plain HTTP. Use a **strong password** and preferably enable `--local-only` with an SSH tunnel (`ssh -L 8088:127.0.0.1:8088 user@SERVER`), or put it behind a reverse HTTPS proxy.
- To serve the portal over HTTPS with a domain, put it behind a reverse proxy (e.g. Caddy or Nginx) and give the public address to the panel so copied links are correct:

  ```bash
  sudo systemctl edit ovpn-panel
  ```
  ```ini
  [Service]
  Environment=PORTAL_BASE_URL=https://vpn.example.com
  ```
  And for the portal behind your own proxy:
  ```bash
  sudo systemctl edit gasslight-portal
  ```
  ```ini
  [Service]
  Environment=TRUST_PROXY=1
  ```
  Then `sudo gasslight restart`. (`systemctl edit` settings survive updates.)
- Admin passwords are stored hashed. **User download passwords are stored in plain text** in the panel's data directory (so you can see them); that directory is readable only by root. Backups contain private keys.
- Panel data lives in `/opt/ovpn-panel` (permission `700`).

---

## Iran Tunnel: `iran-tunnel-setup.sh`

This script runs on the **Iranian server** and passes everything that arrives on the specified ports (UDP + TCP) untouched to the foreign server (where OpenVPN and the panel are installed). Nothing is decrypted or inspected here; this server is just a **carrier**. Encryption remains between the user and the foreign server.

```text
      user  --->  Iran server (this script)  --->  foreign server
```

### Run

On the Iranian server as root:

```bash
wget https://raw.githubusercontent.com/mahanamini/openvpn/main/scripts/iran-tunnel-setup.sh -O iran-tunnel-setup.sh
sudo bash iran-tunnel-setup.sh --to Panel-IP --ports 1194:3000
```

### Options

| Option | Meaning |
|---|---|
| `--to IP` | Foreign server's IP (**required**) |
| `--ports LIST` | Ports to forward, allowed as ranges, comma-separated, max 15 entries (default `1194:3000` — the panel allocates ports starting from 1195) |
| `--status` | Show current status and forwarded packet counts |
| `--uninstall` | Completely remove the tunnel |
| `-h`, `--help` | Help |

### How the Script Works

The script creates a systemd service named `gl-tunnel` that applies these `iptables` rules and comes back up after restart/reboot:

- **DNAT** in `PREROUTING`: every UDP/TCP packet whose destination is one of the range's ports is forwarded, unopened and unmodified, to the foreign server's IP (the port is preserved).
- **MASQUERADE** in `POSTROUTING`: the packet's source IP is rewritten to the Iranian server's IP so the foreign server's reply comes back along the path it came.
- **ACCEPT in `FORWARD`**: this traffic and its related replies (ESTABLISHED,RELATED) are allowed to pass.
- **IP forwarding** is enabled (`net.ipv4.ip_forward = 1` in `sysctl.d` so it persists after reboot).

### Built-in Protections

- If the port range includes **port 22 (SSH)**, execution stops so you don't lock yourself out.
- The **current session's SSH port** (read from `SSH_CONNECTION`) is also checked; if the range includes it, execution stops.
- If a service on this server is listening on one of the range's ports, it warns you that service will become unreachable (because the traffic goes to the foreign server).

### After Running

1. Open the same range of ports (UDP and TCP) in the firewall / provider panel of the Iranian server.
2. In the panel, open **Advanced → .ovpn address** and enter the Iranian server's IP (full guide in [this section](#address-inside-ovpn-files)).
3. Check status anytime: `sudo bash iran-tunnel-setup.sh --status` — it shows the number of packets forwarded to the foreign server (if all are zero, no one has connected yet).
4. Remove: `sudo bash iran-tunnel-setup.sh --uninstall`

---

## Exit Server: `exit-server-setup.sh`

This script runs on **your foreign servers** (Germany, Netherlands, France, etc.) and brings up a **SOCKS5 proxy with username and password** so you can enter it in the panel's [Exit Servers](#exit-servers) section. Do not run it on the panel server.

### Run

```bash
wget https://raw.githubusercontent.com/mahanamini/openvpn/main/scripts/exit-server-setup.sh -O exit-server-setup.sh
sudo bash exit-server-setup.sh --allow-ip Panel-IP [--port 1080] [--user NAME] [--pass PASSWORD]
```

`port`, `user`, `password` are optional.

### Options

| Option | Meaning |
|---|---|
| `--allow-ip IP[,IP2]` | Only these IPs (the panel server) are allowed to connect. **Strongly recommended.** |
| `--port N` | Proxy port (default 1080) |
| `--user NAME` | Proxy username (default: random with `gl` prefix) |
| `--pass PASSWORD` | Proxy password (default: random, 20 characters) |
| `--iface NAME` | Outbound network interface (default: auto-detected) |
| `--allow-private` | Allow clients to access this server's private/local networks (blocked by default) |
| `--uninstall` | Remove the proxy, user and its config |
| `-h`, `--help` | Help |

### How the Script Works

1. Installs the `dante-server` package (the standard SOCKS server of the Linux world).
2. Creates a limited **system user** (`nologin`, no home) and sets the proxy password on it (username authentication is done inside dante itself; it has no shell or any other access).
3. Writes the `/etc/danted.conf` config: listening on all interfaces on the chosen port, `username` authentication, and client/socks rules shaped like this:
   - Only IPs in `--allow-ip` (and localhost) may connect; everyone else is blocked.
   - Access to private networks (`10.x`, `172.16-31.x`, `192.168.x`, `127.x`, `169.254.x`) is blocked by default so no one can use the proxy to reach the server's internal network.
   - `connect` and `udpassociate` are allowed and everything else is blocked.
4. Validates the config with `danted -V` **before** touching the running service; if it is broken, the running service is not broken.
5. Configures a systemd service with `Restart=always` so it starts on boot and comes back after a crash.
6. If `ufw` is active, opens the port only for the allowed IPs.
7. Runs a **self-test**: connects through the proxy from inside the server to api.ipify.org to make sure outbound works.
8. Re-running the script is safe: it only rewrites its own config (a copy of the system's original config is kept as `.orig`).

At the end it prints the server's public IP, port, username and password — enter these exact values in the **panel → Advanced → Exit servers** section. If you have a cloud firewall, open the port there too. Complete removal: `sudo bash exit-server-setup.sh --uninstall`

---

## Proxy Routing

**Path:** Panel → Advanced → Proxy routing

Passes users' TCP traffic through upstream proxies (SOCKS4 / SOCKS5 / HTTP) so websites see the proxy's IP instead of your server's IP. Applies to **all users on the panel**.

### Settings

| Option | Description |
|---|---|
| **Enable proxy routing** | Toggle on/off |
| **Proxy list** | One proxy per line. Formats: `ip:port`, `socks5://ip:port`, `socks4://ip:port`, `http://ip:port` and optionally `user:pass@` before the address. Lines starting with `#` are ignored. Max 500 proxies. |
| **Default type** | Default type for lines without a scheme |
| **Rotation** | Three modes: **one proxy per user** (only changes when it fails or after the time interval) / **random** per connection / **round-robin** per connection |
| **Re-pick every** | Time interval for switching proxies in sticky mode (minutes, `0` = only when it fails) |
| **Block QUIC** | Drop UDP 443 so the browser falls back to TCP and goes through the proxy |
| **Fallback** | If all proxies are down, establish a direct connection (your server's IP is exposed) |

### Buttons

- **Save**: save the list and apply firewall rules.
- **Test all**: test all proxies in parallel (connecting through each to api.ipify.org and reading the exit IP and response time).
- **Remove failed**: remove proxies whose test failed.

Each proxy has one of these statuses: healthy / failed / untested / temporarily set aside (cooldown after several consecutive failures).

### Important Notes

- Only TCP goes through the proxy; UDP exits via your own server.
- Free public proxies are slow and unstable and can read or modify unencrypted traffic. For serious use, use [your own Exit Servers](#exit-servers).
- If you stop the panel, users have no internet until it comes back.

---

## Exit Servers

**Path:** Panel → Advanced → Exit servers & .ovpn address

Routes users' TCP traffic out through **your own servers in other countries** (SOCKS5 with username and password). The panel rotates between active servers and skips a server that goes down. Users' `.ovpn` files never change. **When this section is active, it replaces Proxy Routing** (only one of them can be active at a time).

### Setup (End to End)

1. Get a fresh foreign server (Debian/Ubuntu).
2. Run on it:
   ```bash
   sudo bash exit-server-setup.sh --allow-ip Panel_IP
   ```
   (The script prints the IP, port, username and password at the end.)
3. Open the proxy port in that server's cloud firewall.
4. In the panel → Advanced → **Exit servers**, enter the printed values with **Add server**.
5. Tick **Enable exit servers**, set the rotation mode and fallback, and hit Save.
6. To see each server's exit IP and health, click the **Test** button.

### Settings

| Option | Description |
|---|---|
| **Enable exit servers** | Toggle on/off (turns itself off if no active server remains) |
| **Rotation** | One server per user (sticky) / random per connection / round-robin |
| **Re-pick every** | Switch interval in sticky mode (minutes, `0` = only when it fails) |
| **Block QUIC / Fallback** | Same as Proxy Routing |
| **Servers** | Live-status server list (healthy/down/disabled/...). Each server is editable, can be enabled/disabled and deleted. When editing, leave the password blank to keep the current one. Max 50 servers. |

### How It Works

- Every 30 seconds the health of active servers is checked (a test connection through them and reading the exit IP). A broken server is skipped in selection and rejoins rotation when it recovers.
- Each user's TCP traffic exits through one of the servers according to the rotation mode; the transparent relay architecture described in [How It Works](#how-it-works-architecture) applies.

---

## Address Inside .ovpn Files

**Path:** Panel → Advanced → Exit servers & .ovpn address (box at the bottom of the page)

Specifies the IP or domain written inside every downloaded `.ovpn` file.

### Main Use: Combining with the Iran Tunnel

When you use the [Iran tunnel](#iran-tunnel-setup-tunnel), fill this box with the **Iranian server's IP**:

- The user downloads the `.ovpn` file and it already contains the Iranian server's IP; no manual editing needed.
- Each user **keeps their own port** (only the host of the `remote` line changes, not the port), so the tunnel must forward that same port range.
- This change applies "at download time": all subsequent downloads (including for old users) get the new address, and if you clear the box, the server's original address comes back. Previously downloaded files do not change.
- The user portal displays this same replacement address.

### Validation Rules

Only a valid IPv4 address or a domain name is accepted; a port, `http://` or any extra content is not allowed. Leaving it empty means "use the server's own address".

---

## Update and Uninstall

**Update** (users, ports and passwords are preserved):

```bash
sudo gasslight update
```

*(Or run `sudo bash install.sh` again)*. The first run after updating from an older version adds a management socket to existing instances and restarts each once; clients reconnect within a few seconds.

**Remove** the panel and portal (users' VPN services keep working):

```bash
sudo gasslight uninstall           # keep panel data
sudo gasslight uninstall --purge   # also wipe panel data
```

---

## Troubleshooting

| Problem | What to check |
|---|---|
| **Panel won't open** | Open TCP 8088 in the cloud firewall; `sudo gasslight status`; `sudo gasslight logs` |
| **Forgot the password** | `sudo gasslight reset-password` |
| **Portal won't start** | `journalctl -u gasslight-portal -n 30`; make sure the panel has run at least once (it creates the shared secret) |
| **A user can't connect** | Open that user's port (and its protocol) in the cloud firewall; `systemctl status openvpn-server@<username>` |
| **Speed limit not applied** | `tc` is part of `iproute2` (`sudo apt install iproute2`); the panel re-applies limits every 30 seconds |
| **User shows "Blocked"** | A traffic cap is full — raise it in *Edit* or use *Reset usage* |
| **`.ovpn` file stopped working** | Its ID, port or protocol changed — send the freshly downloaded file |
| **Installer says it needs a terminal** | Download the script first (`wget`), then run with `sudo bash install.sh` |
| **Users lost internet after enabling Proxy/Exit** | Check proxy/exit server status (all down? is fallback on?); the panel must be up; `sudo gasslight logs` |
| **Iran tunnel not working** | `sudo bash iran-tunnel-setup.sh --status`; did you open the port range in the Iranian server's firewall? Is the foreign server IP in `--to` correct? |
| **Exit server test fails** | `systemctl status danted` on the exit server; does `--allow-ip` include the panel server's IP? Is the port open in the cloud firewall? |

---

## Project Structure

```text
openvpn/
├── install.sh              # one-line installer
├── core/                   # OpenVPN core installer used by the installer
│   └── openvpn-install.sh
├── panel/
│   └── panel.py            # admin panel (Flask, single file)
├── portal/
│   └── portal.py           # user portal (Flask, single file)
└── scripts/
    ├── exit-server-setup.sh  # SOCKS5 exit server builder (on foreign servers)
    └── iran-tunnel-setup.sh  # packet relay for the Iranian server
```

### On the Installed Server:

| Path | Contents |
|---|---|
| `/opt/ovpn-panel/` | panel code and all its data (users, usage, `.ovpn` files, backups) |
| `/opt/gasslight-portal/` | portal code |
| `/etc/gasslight/` | install settings and the portal's shared secret |
| `/etc/openvpn/server/` | OpenVPN configs, certificates and per-user instances |
| `/usr/local/bin/gasslight` | management command |
| `/usr/local/sbin/gl-tunnel` | Iran tunnel script (when installed) |
| `/etc/danted.conf` | exit server config (on exit servers) |

---

## Copyright and License

Copyright (c) 2026 Mahan Amini. All rights reserved by the author except as granted below.

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, subject to the following condition: the above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

You are responsible for using this software in accordance with the laws that apply to you and to your server provider.

*OpenVPN is a registered trademark of OpenVPN Inc. GassLight is an independent project and is not affiliated with or endorsed by OpenVPN Inc.*
