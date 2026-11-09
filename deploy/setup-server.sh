#!/bin/bash
# Tradewise server setup for any Ubuntu 22.04 / 24.04 server (Arm or x86): AWS, Oracle Cloud, others.
#
# Paste this whole file into the server's first-boot script box:
#   AWS EC2:      Launch instance -> Advanced details -> "User data"
#   Oracle Cloud: Create instance -> Show advanced options -> Management -> "Paste cloud-init script"
# Change the three lines below first. It runs once, on first boot, and takes about 10 minutes.
# Progress log on the server: /var/log/tradewise-setup.log
#
# ============================ CHANGE THESE ============================
APP_PASSWORD="change-me-to-a-long-password"
REPO_URL="https://github.com/ogdevelpers/tradewise.git"
GITHUB_TOKEN=""   # only if the repository is private: a read-only GitHub token for it
# =======================================================================

set -euo pipefail
exec > >(tee -a /var/log/tradewise-setup.log) 2>&1
echo "=== Tradewise setup started $(date) ==="
export DEBIAN_FRONTEND=noninteractive

APP_DIR=/opt/tradewise
DATA_DIR=/var/lib/tradewise
ENV_FILE=/etc/tradewise.env

# --- 1. System packages, swap (helps the 1 GB "Micro" shape), Caddy for HTTPS ---
apt-get update -y
apt-get install -y python3 python3-venv python3-pip git curl iptables-persistent \
  debian-keyring debian-archive-keyring apt-transport-https gnupg
if [ ! -f /swapfile ]; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --batch --yes --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
apt-get update -y && apt-get install -y caddy

# --- 2. Firewall: some images (e.g. Oracle's) block everything but SSH; open the web ports ---
for port in 80 443; do
  iptables -C INPUT -p tcp --dport "$port" -m state --state NEW -j ACCEPT 2>/dev/null || \
    iptables -I INPUT 1 -p tcp --dport "$port" -m state --state NEW -j ACCEPT
done
netfilter-persistent save

# --- 3. App user, code, Python environment ---
id tradewise >/dev/null 2>&1 || useradd --system --create-home --home-dir /home/tradewise --shell /usr/sbin/nologin tradewise
mkdir -p "$DATA_DIR"
CLONE_URL="$REPO_URL"
if [ -n "$GITHUB_TOKEN" ]; then CLONE_URL="${REPO_URL/https:\/\//https://x-access-token:${GITHUB_TOKEN}@}"; fi
if [ ! -d "$APP_DIR/.git" ]; then git clone --depth 1 "$CLONE_URL" "$APP_DIR"; fi
git -C "$APP_DIR" remote set-url origin "$REPO_URL"   # don't keep the token in the repo config
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -e "$APP_DIR"
chown -R tradewise:tradewise "$APP_DIR" "$DATA_DIR"
chmod 700 "$DATA_DIR"

# --- 4. Settings (readable only by root and the app) ---
umask 077
cat > "$ENV_FILE" <<EOF
TRADEWISE_DATA_DIR=$DATA_DIR
TRADEWISE_API_HOST=127.0.0.1
TRADEWISE_API_PORT=8000
APP_PASSWORD=$APP_PASSWORD
EOF
chown root:tradewise "$ENV_FILE" && chmod 640 "$ENV_FILE"
umask 022

# --- 5. Run Tradewise as a service (starts on boot, restarts if it ever stops) ---
cat > /etc/systemd/system/tradewise.service <<'EOF'
[Unit]
Description=Tradewise
After=network-online.target
Wants=network-online.target

[Service]
User=tradewise
EnvironmentFile=/etc/tradewise.env
WorkingDirectory=/opt/tradewise
ExecStart=/opt/tradewise/.venv/bin/tradewise serve
Restart=always
RestartSec=5
NoNewPrivileges=true
ProtectSystem=full
ReadWritePaths=/var/lib/tradewise /opt/tradewise

[Install]
WantedBy=multi-user.target
EOF

# --- 6. HTTPS address <ip-with-dashes>.sslip.io, refreshed on every boot in case the IP changes ---
cat > /usr/local/bin/tradewise-https <<'EOF'
#!/bin/bash
set -e
IP=$(curl -4 -s --max-time 10 https://api.ipify.org || curl -4 -s --max-time 10 https://ifconfig.me)
[ -n "$IP" ] || { echo "could not find the public IP"; exit 1; }
HOST="${IP//./-}.sslip.io"
cat > /etc/caddy/Caddyfile <<CADDY
$HOST {
    encode gzip
    reverse_proxy 127.0.0.1:8000
}
CADDY
systemctl reload caddy || systemctl restart caddy
cat > /etc/motd <<MOTD

  Tradewise is running at:  https://$HOST
  Server IP (register this with Zerodha):  $IP

MOTD
echo "https://$HOST" > /var/lib/tradewise/URL.txt
echo "$IP" > /var/lib/tradewise/IP.txt
EOF
chmod +x /usr/local/bin/tradewise-https
cat > /etc/systemd/system/tradewise-https.service <<'EOF'
[Unit]
Description=Point HTTPS at this server's current IP
After=network-online.target caddy.service
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/tradewise-https

[Install]
WantedBy=multi-user.target
EOF

# --- 7. One-line updater: sudo tradewise-update ---
cat > /usr/local/bin/tradewise-update <<'EOF'
#!/bin/bash
set -e
sudo -u tradewise git -C /opt/tradewise pull --ff-only
sudo -u tradewise /opt/tradewise/.venv/bin/pip install --quiet -e /opt/tradewise
systemctl restart tradewise
echo "Tradewise updated."
EOF
chmod +x /usr/local/bin/tradewise-update

systemctl daemon-reload
systemctl enable --now tradewise.service caddy.service tradewise-https.service
echo "=== Tradewise setup finished $(date): $(cat /var/lib/tradewise/URL.txt 2>/dev/null) ==="
