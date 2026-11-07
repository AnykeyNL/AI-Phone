#!/usr/bin/env bash
# AI-Phone installer for Ubuntu Server 26.04 LTS (also works on 24.04).
#
# Usage (from a clone of the repository):
#   sudo ./install.sh [--port 8080] [--timezone Europe/Amsterdam] [--asterisk-from-source]
#
# Running it again upgrades AI-Phone in place and keeps your settings.

set -euo pipefail

WEB_PORT=8080
TIMEZONE=""
ASTERISK_FROM_SOURCE=0
ASTERISK_SOURCE_VERSION="22-current"

APP_DIR=/opt/aiphone
DATA_DIR=/var/lib/aiphone
AST_DIR=/etc/asterisk
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) WEB_PORT="$2"; shift 2 ;;
    --timezone) TIMEZONE="$2"; shift 2 ;;
    --asterisk-from-source) ASTERISK_FROM_SOURCE=1; shift ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

say()  { printf '\n\033[1;33m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32m%s\033[0m\n' "$*"; }
die()  { printf '\n\033[1;31mError: %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run this script with sudo: sudo ./install.sh"
[[ -f "$SRC_DIR/aiphone/__main__.py" ]] || die "Run install.sh from the AI-Phone repository folder."

. /etc/os-release
if [[ "${ID:-}" != "ubuntu" ]]; then
  echo "Warning: this installer is made for Ubuntu; you are running ${PRETTY_NAME:-unknown}."
elif [[ "${VERSION_ID:-}" != "26.04" ]]; then
  echo "Note: made for Ubuntu 26.04 LTS; you are running ${PRETTY_NAME}. Continuing."
fi

export DEBIAN_FRONTEND=noninteractive

# ------------------------------------------------------------------ packages
say "Installing system packages"
apt-get update -q
apt-get install -y -q python3 python3-venv python3-pip curl ca-certificates rsync \
  openssl sudo

asterisk_available() {
  apt-cache show asterisk >/dev/null 2>&1 && apt-cache show asterisk-modules >/dev/null 2>&1
}

build_asterisk_from_source() {
  say "Building Asterisk ${ASTERISK_SOURCE_VERSION} from source (this takes 10 to 30 minutes)"
  apt-get install -y -q build-essential wget pkg-config libedit-dev uuid-dev libjansson-dev \
    libxml2-dev libsqlite3-dev libssl-dev libcurl4-openssl-dev libsrtp2-dev bzip2 patch
  local build=/usr/src/asterisk-build
  rm -rf "$build"; mkdir -p "$build"; cd "$build"
  wget -q "https://downloads.asterisk.org/pub/telephony/asterisk/asterisk-${ASTERISK_SOURCE_VERSION}.tar.gz" -O asterisk.tar.gz
  tar xzf asterisk.tar.gz
  cd asterisk-*/
  ./configure --with-pjproject-bundled --with-jansson-bundled >/dev/null
  make menuselect.makeopts >/dev/null
  menuselect/menuselect --enable res_audiosocket --enable app_audiosocket \
    --enable chan_audiosocket --enable func_curl --enable res_curl menuselect.makeopts
  make -j"$(nproc)" >/dev/null
  make install >/dev/null
  [[ -f "$AST_DIR/asterisk.conf" ]] || make samples >/dev/null
  id asterisk >/dev/null 2>&1 || useradd --system --home-dir /var/lib/asterisk --shell /usr/sbin/nologin asterisk
  chown -R asterisk:asterisk /var/lib/asterisk /var/log/asterisk /var/spool/asterisk /var/run/asterisk 2>/dev/null || true
  chown -R asterisk:asterisk "$AST_DIR"
  sed -i 's/^;\?runuser.*/runuser = asterisk/; s/^;\?rungroup.*/rungroup = asterisk/' "$AST_DIR/asterisk.conf"
  cat >/etc/systemd/system/asterisk.service <<'UNIT'
[Unit]
Description=Asterisk PBX
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/sbin/asterisk -f -C /etc/asterisk/asterisk.conf
ExecReload=/usr/sbin/asterisk -rx 'core reload'
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  cd "$SRC_DIR"
  ok "Asterisk built and installed"
}

if command -v asterisk >/dev/null 2>&1; then
  ok "Asterisk is already installed: $(asterisk -V)"
elif [[ $ASTERISK_FROM_SOURCE -eq 0 ]] && asterisk_available; then
  say "Installing Asterisk from the Ubuntu repositories"
  apt-get install -y -q asterisk asterisk-modules
else
  [[ $ASTERISK_FROM_SOURCE -eq 1 ]] || echo "The asterisk package is not available here; building it from source."
  build_asterisk_from_source
fi

ASTERISK_MODDIR=$(asterisk -rx 'core show settings' 2>/dev/null | awk -F': *' '/Module directory/ {print $2}' || true)
if [[ -z "$ASTERISK_MODDIR" ]]; then
  ASTERISK_MODDIR=$(dirname "$(find /usr/lib /usr/lib64 -name app_audiosocket.so 2>/dev/null | head -1)" 2>/dev/null || true)
fi
for mod in app_audiosocket.so res_audiosocket.so func_curl.so; do
  if [[ -n "$ASTERISK_MODDIR" && ! -f "$ASTERISK_MODDIR/$mod" ]]; then
    die "Asterisk module $mod is missing. Re-run with --asterisk-from-source."
  fi
done
ok "AudioSocket and CURL modules present"

# --------------------------------------------------------------------- users
say "Creating the aiphone service user"
if ! id aiphone >/dev/null 2>&1; then
  useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin aiphone
fi
usermod -a -G asterisk aiphone
getent group systemd-journal >/dev/null && usermod -a -G systemd-journal aiphone

# ------------------------------------------------------------------ app code
say "Installing AI-Phone to $APP_DIR"
mkdir -p "$APP_DIR"
rsync -a --delete --exclude venv --exclude .git --exclude '__pycache__' \
  "$SRC_DIR/" "$APP_DIR/"
if [[ ! -x "$APP_DIR/venv/bin/python" ]]; then
  python3 -m venv "$APP_DIR/venv"
fi
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
chown -R root:root "$APP_DIR"
chmod -R a+rX "$APP_DIR"
ok "Python environment ready"

# ------------------------------------------------------------- data + secrets
mkdir -p "$DATA_DIR"
chown aiphone:aiphone "$DATA_DIR"
chmod 750 "$DATA_DIR"
BOOT="$DATA_DIR/bootstrap.json"
if [[ -f "$BOOT" ]]; then
  AMI_SECRET=$("$APP_DIR/venv/bin/python" -c "import json;print(json.load(open('$BOOT'))['ami_secret'])")
else
  AMI_SECRET=$(openssl rand -hex 24)
fi
cat >"$BOOT" <<EOF
{
  "web_port": $WEB_PORT,
  "ami_host": "127.0.0.1",
  "ami_port": 5038,
  "ami_username": "aiphone",
  "ami_secret": "$AMI_SECRET"
}
EOF
chown aiphone:aiphone "$BOOT"
chmod 600 "$BOOT"

# ----------------------------------------------------------------- Asterisk
say "Configuring Asterisk"
if [[ ! -d /etc/asterisk.before-aiphone ]]; then
  cp -a "$AST_DIR" /etc/asterisk.before-aiphone
  ok "Original configuration saved in /etc/asterisk.before-aiphone"
fi
install -d -o aiphone -g asterisk -m 2775 "$AST_DIR/aiphone"
for f in pjsip.conf extensions.conf rtp.conf modules.conf; do
  install -o asterisk -g asterisk -m 0640 "$APP_DIR/asterisk/$f" "$AST_DIR/$f"
done
sed "s/@AMI_SECRET@/$AMI_SECRET/" "$APP_DIR/asterisk/manager.conf.in" >"$AST_DIR/manager.conf"
chown asterisk:asterisk "$AST_DIR/manager.conf"
chmod 0640 "$AST_DIR/manager.conf"
# Sample files that would add demo SIP accounts or contexts.
for f in extensions.ael extensions.lua users.conf; do
  [[ -f "$AST_DIR/$f" ]] && mv "$AST_DIR/$f" "$AST_DIR/$f.disabled"
done
(cd "$APP_DIR" && sudo -u aiphone PYTHONPATH="$APP_DIR" AIPHONE_DATA_DIR="$DATA_DIR" \
  AIPHONE_ASTERISK_DIR="$AST_DIR/aiphone" "$APP_DIR/venv/bin/python" -m aiphone --write-asterisk-config)
chgrp asterisk "$AST_DIR"/aiphone/*.conf

# ---------------------------------------------------------- sudo + helpers
cat >/etc/sudoers.d/aiphone <<'EOF'
# Lets the AI-Phone web interface change the time zone used for schedules.
aiphone ALL=(root) NOPASSWD: /usr/bin/timedatectl set-timezone *
EOF
chmod 440 /etc/sudoers.d/aiphone
visudo -cf /etc/sudoers.d/aiphone >/dev/null

cat >/usr/local/bin/aiphone-reset-password <<EOF
#!/bin/sh
# Removes the AI-Phone admin password; the next visit to the web interface asks for a new one.
cd $APP_DIR && exec sudo -u aiphone PYTHONPATH=$APP_DIR AIPHONE_DATA_DIR=$DATA_DIR $APP_DIR/venv/bin/python -m aiphone --reset-password
EOF
chmod 755 /usr/local/bin/aiphone-reset-password

if [[ -n "$TIMEZONE" ]]; then
  timedatectl set-timezone "$TIMEZONE" && ok "Time zone set to $TIMEZONE"
fi

# ------------------------------------------------------------------ firewall
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  say "Opening firewall ports (ufw)"
  ufw allow "$WEB_PORT"/tcp comment 'AI-Phone web' >/dev/null
  ufw allow 5060/udp comment 'AI-Phone SIP' >/dev/null
  ufw allow 10000:10200/udp comment 'AI-Phone RTP' >/dev/null
  ok "Ports $WEB_PORT/tcp, 5060/udp and 10000-10200/udp opened"
fi

# ------------------------------------------------------------------ services
say "Starting services"
install -m 0644 "$APP_DIR/systemd/aiphone.service" /etc/systemd/system/aiphone.service
systemctl daemon-reload
systemctl enable asterisk >/dev/null 2>&1 || true
systemctl restart asterisk
systemctl enable aiphone >/dev/null
systemctl restart aiphone

for _ in $(seq 1 30); do
  curl -fsS -o /dev/null "http://127.0.0.1:$WEB_PORT/login" 2>/dev/null && break
  sleep 1
done
if ! curl -fsS -o /dev/null "http://127.0.0.1:$WEB_PORT/login"; then
  echo "AI-Phone did not start. See: journalctl -u aiphone -n 50"
  exit 1
fi

IP=$(ip -4 route get 192.0.2.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}')
say "AI-Phone is installed"
cat <<EOF

    Open the web interface:  http://${IP:-<server-ip>}:$WEB_PORT

    1. Choose an admin password.
    2. Settings: add your OpenAI key (and Anthropic + Deepgram for Claude).
    3. Phone & ATA: copy the values into your Grandstream HT801/HT802.
    4. Lift the handset.

EOF
