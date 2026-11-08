#!/usr/bin/env bash
# AI-Phone self-update. Runs as root from aiphone-update.service, which the web
# interface starts with "Update now" (or the nightly automatic update). It
# downloads the latest version of the repository into /opt/aiphone-src, using
# the GitHub token saved on the Settings page when the repository is private,
# and runs install.sh from there. Settings and the call log are kept.
set -euo pipefail
trap 'echo "Error: the update failed (line $LINENO). AI-Phone keeps running the previous version."' ERR

LOG=/var/log/aiphone-update.log
: >"$LOG"
chmod 644 "$LOG"
exec > >(tee -a "$LOG") 2>&1

echo "Update started $(date '+%Y-%m-%d %H:%M:%S')"
DB=/var/lib/aiphone/aiphone.db
SRC=/opt/aiphone-src

read_setting() {
  /opt/aiphone/venv/bin/python -I - "$DB" "$1" <<'PY'
import json, sqlite3, sys
row = sqlite3.connect(sys.argv[1]).execute(
    "SELECT value FROM settings WHERE key = ?", (sys.argv[2],)).fetchone()
print(json.loads(row[0]) if row and row[0] else "")
PY
}

REPO=$(read_setting update_repo)
BRANCH=$(read_setting update_branch)
TOKEN=$(read_setting update_token)
if [[ -z "$REPO" ]]; then
  REPO=$(/opt/aiphone/venv/bin/python -I -c "import json;print(json.load(open('/opt/aiphone/.install.json')).get('repo',''))" 2>/dev/null || true)
fi
REPO=${REPO:-AnykeyNL/AI-Phone}
BRANCH=${BRANCH:-main}
[[ "$REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || { echo "Invalid repository name: $REPO"; exit 1; }
[[ "$BRANCH" =~ ^[A-Za-z0-9_./-]+$ ]] || { echo "Invalid branch name: $BRANCH"; exit 1; }

URL="https://github.com/$REPO.git"
GIT=(git -c 'safe.directory=*')
if [[ -n "$TOKEN" ]]; then
  # The token goes in a header for this command only; it is never written to .git/config.
  AUTH=$(printf 'x-access-token:%s' "$TOKEN" | base64 -w0)
  GIT+=(-c "http.https://github.com/.extraheader=Authorization: Basic $AUTH")
fi

echo "Downloading $REPO ($BRANCH)"
if [[ ! -d "$SRC/.git" ]]; then
  rm -rf "$SRC"
  install -d -m 700 "$SRC"
  git -C "$SRC" init -q
  git -C "$SRC" remote add origin "$URL"
fi
git -C "$SRC" remote set-url origin "$URL"
"${GIT[@]}" -C "$SRC" fetch -q --depth 1 origin "$BRANCH"
git -C "$SRC" checkout -q -f -B "$BRANCH" FETCH_HEAD
git -C "$SRC" branch -q --set-upstream-to="origin/$BRANCH" 2>/dev/null || true
echo "New version: $(git -C "$SRC" log -1 --format='%h %s')"

cd "$SRC"
./install.sh
echo "Update finished $(date '+%Y-%m-%d %H:%M:%S')"
