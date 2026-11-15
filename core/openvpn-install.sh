#!/bin/bash
#
# Non-interactive OpenVPN 2.7.x installer
# Based on https://github.com/Nyr/openvpn-install (MIT License, Copyright (c) 2013 Nyr)
#
# - asks only ONE question: the public IP/hostname clients connect to (Enter = detected default)
# - installs OpenVPN 2.7.9 from the official OpenVPN repositories
# - does NOT create any client (create them later with easy-rsa / your panel)
# - file layout is identical to the original script, so existing tooling keeps working

# ----------------------------------------------------------------------------
# Defaults (edit here if you ever need to change them)
# ----------------------------------------------------------------------------
protocol="udp"                                  # default protocol (panel can override per user)
port="1194"                                     # default port
dns1="1.1.1.1"                                  # Cloudflare DNS
dns2="1.0.0.1"
OPENVPN_VERSION="${OPENVPN_VERSION:-2.7.9}"     # wanted OpenVPN version
# Optional: export PUBLIC_IP=x.x.x.x to change the default shown for the IP question

set -eo pipefail
export DEBIAN_FRONTEND=noninteractive

# Detect Debian users running the script with "sh" instead of bash
if readlink /proc/$$/exe | grep -q "dash"; then
	echo 'This installer needs to be run with "bash", not "sh".'
	exit 1
fi

# Detect OS
# $os_version variables aren't always in use, but are kept here for convenience
if grep -qs "ubuntu" /etc/os-release; then
	os="ubuntu"
	os_version=$(grep 'VERSION_ID' /etc/os-release | cut -d '"' -f 2 | tr -d '.')
	group_name="nogroup"
elif [[ -e /etc/debian_version ]]; then
	os="debian"
	os_version=$(grep -oE '[0-9]+' /etc/debian_version | head -1)
	group_name="nogroup"
elif [[ -e /etc/almalinux-release || -e /etc/rocky-release || -e /etc/centos-release ]]; then
	os="centos"
	os_version=$(grep -shoE '[0-9]+' /etc/almalinux-release /etc/rocky-release /etc/centos-release | head -1)
	group_name="nobody"
elif [[ -e /etc/fedora-release ]]; then
	os="fedora"
	os_version=$(grep -oE '[0-9]+' /etc/fedora-release | head -1)
	group_name="nobody"
else
	echo "This installer seems to be running on an unsupported distribution.
Supported distros are Ubuntu, Debian, AlmaLinux, Rocky Linux, CentOS and Fedora."
	exit 1
fi

if [[ "$os" == "ubuntu" && "$os_version" -lt 2204 ]]; then
	echo "Ubuntu 22.04 or higher is required to use this installer.
This version of Ubuntu is too old and unsupported."
	exit 1
fi

if [[ "$os" == "debian" ]]; then
	if grep -q '/sid' /etc/debian_version; then
		echo "Debian Testing and Debian Unstable are unsupported by this installer."
		exit 1
	fi
	if [[ "$os_version" -lt 11 ]]; then
		echo "Debian 11 or higher is required to use this installer.
This version of Debian is too old and unsupported."
		exit 1
	fi
fi

if [[ "$os" == "centos" && "$os_version" -lt 9 ]]; then
	os_name=$(sed 's/ release.*//' /etc/almalinux-release /etc/rocky-release /etc/centos-release 2>/dev/null | head -1)
	echo "$os_name 9 or higher is required to use this installer.
This version of $os_name is too old and unsupported."
	exit 1
fi

# Detect environments where $PATH does not include the sbin directories
if ! grep -q sbin <<< "$PATH"; then
	echo '$PATH does not include sbin. Try using "su -" instead of "su".'
	exit 1
fi

if [[ "$EUID" -ne 0 ]]; then
	echo "This installer needs to be run with superuser privileges."
	exit 1
fi

if [[ ! -e /dev/net/tun ]] || ! ( exec 7<>/dev/net/tun ) 2>/dev/null; then
	echo "The system does not have the TUN device available.
TUN needs to be enabled before running this installer."
	exit 1
fi

if [[ -e /etc/openvpn/server/server.conf ]]; then
	echo "OpenVPN is already installed (/etc/openvpn/server/server.conf exists). Nothing to do."
	exit 0
fi

echo "Installing OpenVPN $OPENVPN_VERSION (protocol: $protocol, port: $port, DNS: $dns1 $dns2)..."

# ----------------------------------------------------------------------------
# Network detection
# ----------------------------------------------------------------------------

# IPv4: use the address of the default route; fall back to the first non-loopback IPv4
ip=$(ip -4 route get 1.1.1.1 2>/dev/null | grep -oE 'src [0-9]{1,3}(\.[0-9]{1,3}){3}' | cut -d ' ' -f 2 | head -1 || true)
if [[ -z "$ip" ]]; then
	ip=$(ip -4 addr | grep inet | grep -vE '127(\.[0-9]{1,3}){3}' | cut -d '/' -f 1 | grep -oE '[0-9]{1,3}(\.[0-9]{1,3}){3}' | head -1 || true)
fi
if [[ -z "$ip" ]]; then
	echo "Could not detect an IPv4 address on this server."
	exit 1
fi

# Address that clients will connect to. This is the ONLY question the installer asks.
# Default (just press Enter): $PUBLIC_IP if exported, else the detected public IPv4
# when the server is behind NAT (e.g. AWS), else the server's own IPv4.
default_ip="$PUBLIC_IP"
if [[ -z "$default_ip" ]]; then
	if echo "$ip" | grep -qE '^(10\.|172\.1[6789]\.|172\.2[0-9]\.|172\.3[01]\.|192\.168)'; then
		# Private IP => behind NAT. Get public IP and sanitize with grep (try several services)
		for url in "http://ip1.dynupdate.no-ip.com/" "https://api.ipify.org" "https://ipv4.icanhazip.com"; do
			default_ip=$( { wget -T 10 -t 1 -4qO- "$url" 2>/dev/null || curl -m 10 -4Ls "$url" 2>/dev/null; } | grep -m 1 -oE '^[0-9]{1,3}(\.[0-9]{1,3}){3}$' || true)
			if [[ -n "$default_ip" ]]; then
				break
			fi
		done
	else
		default_ip="$ip"
	fi
fi

# Where to read the answer from: the terminal, even when the script is piped (curl | bash)
if [[ -t 0 ]]; then
	input_dev="/dev/stdin"
elif { true < /dev/tty; } 2>/dev/null; then
	input_dev="/dev/tty"
else
	input_dev=""
fi

public_ip=""
while [[ -z "$public_ip" ]]; do
	answer=""
	eof=0
	if [[ -n "$input_dev" ]]; then
		echo
		if [[ -n "$default_ip" ]]; then
			read -r -p "Public IPv4 address / hostname for clients [$default_ip]: " answer < "$input_dev" || eof=1
		else
			read -r -p "Public IPv4 address / hostname for clients: " answer < "$input_dev" || eof=1
		fi
	fi
	# Enter (empty answer) => default
	if [[ -z "$answer" ]]; then
		answer="$default_ip"
	fi
	if [[ -z "$answer" ]]; then
		if [[ -z "$input_dev" || "$eof" -eq 1 ]]; then
			echo "The public IPv4 of this server could not be detected and no input is available."
			echo "Run again with:  PUBLIC_IP=x.x.x.x bash $0"
			exit 1
		fi
		echo "Invalid input."
	elif [[ "$answer" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ || "$answer" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]]; then
		public_ip="$answer"
	else
		echo "$answer: invalid address."
		if [[ -z "$input_dev" || "$eof" -eq 1 ]]; then
			exit 1
		fi
	fi
done

# IPv6: use the first global IPv6 address, if any
ip6=$(ip -6 addr | grep 'inet6 [23]' | cut -d '/' -f 1 | grep -oE '([0-9a-fA-F]{0,4}:){1,7}[0-9a-fA-F]{0,4}' | head -1 || true)

# ----------------------------------------------------------------------------
# Packages
# ----------------------------------------------------------------------------

# Install a firewall if firewalld or iptables are not already available
firewall=""
if ! systemctl is-active --quiet firewalld.service && ! hash iptables 2>/dev/null; then
	if [[ "$os" == "centos" || "$os" == "fedora" ]]; then
		firewall="firewalld"
		echo "firewalld, which is required to manage routing tables, will also be installed."
	elif [[ "$os" == "debian" || "$os" == "ubuntu" ]]; then
		# iptables is way less invasive than firewalld so no warning is given
		firewall="iptables"
	fi
fi

# If running inside a container, disable LimitNPROC to prevent conflicts
if systemd-detect-virt -cq; then
	mkdir -p /etc/systemd/system/openvpn-server@server.service.d/
	echo "[Service]
LimitNPROC=infinity" > /etc/systemd/system/openvpn-server@server.service.d/disable-limitnproc.conf
fi

if [[ "$os" = "debian" || "$os" = "ubuntu" ]]; then
	apt-get update
	apt-get install -y --no-install-recommends ca-certificates curl gnupg
	# Official OpenVPN 2.7 apt repository (distro repos ship older versions)
	codename=$(. /etc/os-release && echo "${VERSION_CODENAME:-}")
	[[ -z "$codename" ]] && codename=$(lsb_release -cs 2>/dev/null || true)
	mkdir -p /etc/apt/keyrings
	curl -fsSL https://swupdate.openvpn.net/repos/repo-public.gpg | gpg --dearmor --yes -o /etc/apt/keyrings/openvpn-repo-public.gpg
	echo "deb [signed-by=/etc/apt/keyrings/openvpn-repo-public.gpg] https://build.openvpn.net/debian/openvpn/release/2.7 $codename main" > /etc/apt/sources.list.d/openvpn-aptrepo.list
	if ! apt-get update; then
		echo "WARNING: the official OpenVPN repository is not usable for '$codename'. Falling back to the distro package."
		rm -f /etc/apt/sources.list.d/openvpn-aptrepo.list
		apt-get update
	fi
	# Try the exact version first, then whatever is newest
	if ! apt-get install -y --no-install-recommends "openvpn=${OPENVPN_VERSION}*" openssl ca-certificates $firewall; then
		echo "WARNING: openvpn $OPENVPN_VERSION is not available in the repository yet. Installing the latest available version."
		apt-get install -y --no-install-recommends openvpn openssl ca-certificates $firewall
	fi
elif [[ "$os" = "centos" ]]; then
	dnf install -y epel-release
	dnf install -y 'dnf-command(copr)'
	# Official OpenVPN 2.7 Copr repository (EPEL builds)
	dnf copr enable -y @OpenVPN/openvpn-release-2.7 || echo "WARNING: could not enable the OpenVPN Copr repository."
	if ! dnf install -y "openvpn-${OPENVPN_VERSION}" openssl ca-certificates tar $firewall; then
		echo "WARNING: openvpn $OPENVPN_VERSION is not available in the repository yet. Installing the latest available version."
		dnf install -y openvpn openssl ca-certificates tar $firewall
	fi
else
	# Else, OS must be Fedora (Fedora repositories already carry the latest release)
	if ! dnf install -y "openvpn-${OPENVPN_VERSION}" openssl ca-certificates tar $firewall; then
		echo "WARNING: openvpn $OPENVPN_VERSION is not available in the repository yet. Installing the latest available version."
		dnf install -y openvpn openssl ca-certificates tar $firewall
	fi
fi

# Report the version that actually got installed
installed_version=$(openvpn --version 2>&1 | head -1 | awk '{print $2}' || true)
if [[ "$installed_version" != "$OPENVPN_VERSION" ]]; then
	echo "WARNING: installed OpenVPN version is $installed_version (wanted $OPENVPN_VERSION)."
fi

# If firewalld was just installed, enable it
if [[ "$firewall" == "firewalld" ]]; then
	systemctl enable --now firewalld.service
fi

# ----------------------------------------------------------------------------
# PKI
# ----------------------------------------------------------------------------

# Get easy-rsa
easy_rsa_url='https://github.com/OpenVPN/easy-rsa/releases/download/v3.2.7/EasyRSA-3.2.7.tgz'
mkdir -p /etc/openvpn/server/easy-rsa/
{ wget -qO- "$easy_rsa_url" 2>/dev/null || curl -sL "$easy_rsa_url" ; } | tar xz -C /etc/openvpn/server/easy-rsa/ --strip-components 1
chown -R root:root /etc/openvpn/server/easy-rsa/
cd /etc/openvpn/server/easy-rsa/

# Create the PKI, set up the CA and create TLS key
./easyrsa --batch init-pki
./easyrsa --batch build-ca nopass
./easyrsa --batch gen-tls-crypt-key

# Create the DH parameters file using the predefined ffdhe2048 group
echo '-----BEGIN DH PARAMETERS-----
MIIBCAKCAQEA//////////+t+FRYortKmq/cViAnPTzx2LnFg84tNpWp4TZBFGQz
+8yTnc4kmz75fS/jY2MMddj2gbICrsRhetPfHtXV/WVhJDP1H18GbtCFY2VVPe0a
87VXE15/V8k1mE8McODmi3fipona8+/och3xWKE2rec1MKzKT0g6eXq8CrGCsyT7
YdEIqUuyyOP7uWrat2DX9GgdT0Kj3jlN9K5W7edjcrsZCwenyO4KbXCeAvzhzffi
7MA0BM0oNC9hkXL+nOmFg/+OTxIy7vKBg8P+OxtMb61zO7X8vC7CIAXFjvGDfRaD
ssbzSibBsu/6iGtCOGEoXJf//////////wIBAg==
-----END DH PARAMETERS-----' > /etc/openvpn/server/dh.pem

# Make easy-rsa aware of our external DH file (prevents a warning)
ln -s /etc/openvpn/server/dh.pem pki/dh.pem

# Create the server certificate and the CRL (NO client certificate is created)
./easyrsa --batch --days=3650 build-server-full server nopass
./easyrsa --batch --days=3650 gen-crl

# Move the stuff we need
cp pki/ca.crt pki/private/ca.key pki/issued/server.crt pki/private/server.key pki/crl.pem /etc/openvpn/server
cp pki/private/easyrsa-tls.key /etc/openvpn/server/tc.key

# CRL is read with each client connection, while OpenVPN is dropped to nobody
chown nobody:"$group_name" /etc/openvpn/server/crl.pem
# Without +x in the directory, OpenVPN can't run a stat() on the CRL file
chmod o+x /etc/openvpn/server/

# ----------------------------------------------------------------------------
# server.conf
# ----------------------------------------------------------------------------
echo "local $ip
port $port
proto $protocol
dev tun
ca ca.crt
cert server.crt
key server.key
dh dh.pem
auth SHA512
tls-crypt tc.key
topology subnet
server 10.8.0.0 255.255.255.0" > /etc/openvpn/server/server.conf

# IPv6
if [[ -z "$ip6" ]]; then
	echo 'push "redirect-gateway def1 bypass-dhcp"' >> /etc/openvpn/server/server.conf
else
	echo 'server-ipv6 fddd:1194:1194:1194::/64' >> /etc/openvpn/server/server.conf
	echo 'push "redirect-gateway def1 ipv6 bypass-dhcp"' >> /etc/openvpn/server/server.conf
fi
echo 'ifconfig-pool-persist ipp.txt' >> /etc/openvpn/server/server.conf

# DNS (Cloudflare)
echo "push \"dhcp-option DNS $dns1\"" >> /etc/openvpn/server/server.conf
echo "push \"dhcp-option DNS $dns2\"" >> /etc/openvpn/server/server.conf
echo 'push "block-outside-dns"' >> /etc/openvpn/server/server.conf
echo "keepalive 10 120
user nobody
group $group_name
persist-key
persist-tun
verb 3
crl-verify crl.pem" >> /etc/openvpn/server/server.conf
if [[ "$protocol" = "udp" ]]; then
	echo "explicit-exit-notify" >> /etc/openvpn/server/server.conf
fi

# ----------------------------------------------------------------------------
# Forwarding + firewall/NAT
# ----------------------------------------------------------------------------

# Enable net.ipv4.ip_forward for the system
echo 'net.ipv4.ip_forward=1' > /etc/sysctl.d/99-openvpn-forward.conf
# Enable without waiting for a reboot or service restart
echo 1 > /proc/sys/net/ipv4/ip_forward
if [[ -n "$ip6" ]]; then
	# Enable net.ipv6.conf.all.forwarding for the system
	echo "net.ipv6.conf.all.forwarding=1" >> /etc/sysctl.d/99-openvpn-forward.conf
	# Enable without waiting for a reboot or service restart
	echo 1 > /proc/sys/net/ipv6/conf/all/forwarding
fi

if systemctl is-active --quiet firewalld.service; then
	# Using both permanent and not permanent rules to avoid a firewalld reload.
	# We don't use --add-service=openvpn because that would only work with
	# the default port and protocol.
	firewall-cmd --add-port="$port"/"$protocol"
	firewall-cmd --zone=trusted --add-source=10.8.0.0/24
	firewall-cmd --permanent --add-port="$port"/"$protocol"
	firewall-cmd --permanent --zone=trusted --add-source=10.8.0.0/24
	# Set NAT for the VPN subnet
	firewall-cmd --direct --add-rule ipv4 nat POSTROUTING 0 -s 10.8.0.0/24 ! -d 10.8.0.0/24 -j SNAT --to "$ip"
	firewall-cmd --permanent --direct --add-rule ipv4 nat POSTROUTING 0 -s 10.8.0.0/24 ! -d 10.8.0.0/24 -j SNAT --to "$ip"
	if [[ -n "$ip6" ]]; then
		firewall-cmd --zone=trusted --add-source=fddd:1194:1194:1194::/64
		firewall-cmd --permanent --zone=trusted --add-source=fddd:1194:1194:1194::/64
		firewall-cmd --direct --add-rule ipv6 nat POSTROUTING 0 -s fddd:1194:1194:1194::/64 ! -d fddd:1194:1194:1194::/64 -j SNAT --to "$ip6"
		firewall-cmd --permanent --direct --add-rule ipv6 nat POSTROUTING 0 -s fddd:1194:1194:1194::/64 ! -d fddd:1194:1194:1194::/64 -j SNAT --to "$ip6"
	fi
else
	# Create a service to set up persistent iptables rules
	iptables_path=$(command -v iptables)
	ip6tables_path=$(command -v ip6tables || true)
	# nf_tables is not available as standard in OVZ kernels. So use iptables-legacy
	# if we are in OVZ, with a nf_tables backend and iptables-legacy is available.
	if [[ $(systemd-detect-virt || true) == "openvz" ]] && readlink -f "$(command -v iptables)" | grep -q "nft" && hash iptables-legacy 2>/dev/null; then
		iptables_path=$(command -v iptables-legacy)
		ip6tables_path=$(command -v ip6tables-legacy)
	fi
	echo "[Unit]
After=network-online.target
Wants=network-online.target
[Service]
Type=oneshot
ExecStart=$iptables_path -w 5 -t nat -A POSTROUTING -s 10.8.0.0/24 ! -d 10.8.0.0/24 -j SNAT --to $ip
ExecStart=$iptables_path -w 5 -I INPUT -p $protocol --dport $port -j ACCEPT
ExecStart=$iptables_path -w 5 -I FORWARD -s 10.8.0.0/24 -j ACCEPT
ExecStart=$iptables_path -w 5 -I FORWARD -m state --state RELATED,ESTABLISHED -j ACCEPT
ExecStop=$iptables_path -w 5 -t nat -D POSTROUTING -s 10.8.0.0/24 ! -d 10.8.0.0/24 -j SNAT --to $ip
ExecStop=$iptables_path -w 5 -D INPUT -p $protocol --dport $port -j ACCEPT
ExecStop=$iptables_path -w 5 -D FORWARD -s 10.8.0.0/24 -j ACCEPT
ExecStop=$iptables_path -w 5 -D FORWARD -m state --state RELATED,ESTABLISHED -j ACCEPT" > /etc/systemd/system/openvpn-iptables.service
	if [[ -n "$ip6" ]]; then
		echo "ExecStart=$ip6tables_path -w 5 -t nat -A POSTROUTING -s fddd:1194:1194:1194::/64 ! -d fddd:1194:1194:1194::/64 -j SNAT --to $ip6
ExecStart=$ip6tables_path -w 5 -I FORWARD -s fddd:1194:1194:1194::/64 -j ACCEPT
ExecStart=$ip6tables_path -w 5 -I FORWARD -m state --state RELATED,ESTABLISHED -j ACCEPT
ExecStop=$ip6tables_path -w 5 -t nat -D POSTROUTING -s fddd:1194:1194:1194::/64 ! -d fddd:1194:1194:1194::/64 -j SNAT --to $ip6
ExecStop=$ip6tables_path -w 5 -D FORWARD -s fddd:1194:1194:1194::/64 -j ACCEPT
ExecStop=$ip6tables_path -w 5 -D FORWARD -m state --state RELATED,ESTABLISHED -j ACCEPT" >> /etc/systemd/system/openvpn-iptables.service
	fi
	echo "RemainAfterExit=yes
[Install]
WantedBy=multi-user.target" >> /etc/systemd/system/openvpn-iptables.service
	systemctl enable --now openvpn-iptables.service
fi

# If SELinux is enabled and a custom port was selected, we need this
if sestatus 2>/dev/null | grep "Current mode" | grep -q "enforcing" && [[ "$port" != 1194 ]]; then
	# Install semanage if not already present
	if ! hash semanage 2>/dev/null; then
		dnf install -y policycoreutils-python-utils
	fi
	semanage port -a -t openvpn_port_t -p "$protocol" "$port"
fi

# If the server is behind NAT, use the correct IP address for clients
if [[ -n "$public_ip" ]]; then
	ip="$public_ip"
fi

# client-common.txt is created so we have a template to add further users later
echo "client
dev tun
proto $protocol
remote $ip $port
resolv-retry infinite
nobind
persist-key
persist-tun
remote-cert-tls server
auth SHA512
ignore-unknown-option block-outside-dns
verb 3" > /etc/openvpn/server/client-common.txt

# ----------------------------------------------------------------------------
# Start
# ----------------------------------------------------------------------------
systemctl enable --now openvpn-server@server.service
sleep 2
if ! systemctl is-active --quiet openvpn-server@server.service; then
	echo "OpenVPN service failed to start. Check: journalctl -u openvpn-server@server.service -n 50"
	exit 1
fi

echo
echo "Finished! OpenVPN $installed_version is installed and running."
echo "  Server : $ip:$port/$protocol"
echo "  DNS    : $dns1, $dns2"
echo "  Config : /etc/openvpn/server/server.conf"
echo "No client was created. To create one:"
echo "  cd /etc/openvpn/server/easy-rsa && ./easyrsa --batch --days=3650 build-client-full NAME nopass"
echo "  (client keys/certs end up in pki/inline/private/NAME.inline; header template: /etc/openvpn/server/client-common.txt)"
