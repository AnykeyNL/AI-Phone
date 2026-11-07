#!/usr/bin/env bash
# Removes AI-Phone. Asterisk stays installed; its original configuration is restored
# from /etc/asterisk.before-aiphone. Pass --purge to also delete settings and the call log.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run with sudo." >&2; exit 1; }
systemctl disable --now aiphone 2>/dev/null || true
rm -f /etc/systemd/system/aiphone.service /etc/sudoers.d/aiphone /usr/local/bin/aiphone-reset-password
systemctl daemon-reload
rm -rf /opt/aiphone
if [[ -d /etc/asterisk.before-aiphone ]]; then
  rm -rf /etc/asterisk
  mv /etc/asterisk.before-aiphone /etc/asterisk
  systemctl restart asterisk || true
  echo "Original Asterisk configuration restored."
fi
if [[ "${1:-}" == "--purge" ]]; then
  rm -rf /var/lib/aiphone
  userdel aiphone 2>/dev/null || true
  echo "Settings and call log deleted."
else
  echo "Settings kept in /var/lib/aiphone (use --purge to delete them)."
fi
echo "AI-Phone removed."
