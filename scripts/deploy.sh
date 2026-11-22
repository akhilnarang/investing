#!/usr/bin/env bash
# Update the server's git checkout from its remote, install the systemd user units, and restart.
# The server deploys what is pushed, never this machine's working tree: push first.
#
#   scripts/deploy.sh                          # fast-forward to the branch tip, then restart
#   DEPLOY_REF=<sha or tag> scripts/deploy.sh  # pin a revision, e.g. to go back to one
#   scripts/deploy.sh --with-db                # also push this machine's database
#
# Configured by the environment, or by DEPLOY_* lines in this checkout's .env:
#   DEPLOY_HOST       required, the ssh destination, e.g. user@host
#   DEPLOY_DEST       the git checkout on the server (default: investing, under its home)
#   DEPLOY_BRANCH     the branch to follow (default: main)
#   DEPLOY_REF        a revision to check out instead of the branch tip
#   DEPLOY_SSH_OPTS   extra ssh options, e.g. "-o HostKeyAlias=myserver"
#
# One-time server setup: git clone <repo url> ~/investing, then create ~/investing/.env there.
#
# The database is not pushed by default; it is a build artifact.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# Only the DEPLOY_* keys are taken from .env, and the environment wins over them.
if [ -f "$ROOT/.env" ]; then
    while IFS='=' read -r key value; do
        value="${value%\"}"; value="${value#\"}"
        [ -z "${!key:-}" ] && export "$key=$value"
    done < <(grep -E '^DEPLOY_[A-Z_]+=' "$ROOT/.env")
fi
HOST="${DEPLOY_HOST:?set DEPLOY_HOST (user@host), in the environment or .env}"
SSH_OPTS="-o ConnectTimeout=15 ${DEPLOY_SSH_OPTS:-}"
# Absolute on the server, so the unit files can name it.
DEST="$(ssh $SSH_OPTS "$HOST" "cd ${DEPLOY_DEST:-investing} && pwd")"
WITH_DB=0
for a in "$@"; do [ "$a" = "--with-db" ] && WITH_DB=1; done

# Stop if a tracked file was edited on the server. data/ and .env are gitignored.
ssh $SSH_OPTS "$HOST" DEST="$DEST" BRANCH="${DEPLOY_BRANCH:-main}" REF="${DEPLOY_REF:-}" \
    bash -seuo pipefail <<'REMOTE'
cd "$DEST"
[ -d .git ] || { echo "$DEST is not a git checkout; clone the repository there first" >&2; exit 1; }
if ! git diff --quiet HEAD --; then
    echo "tracked files are changed on the server; commit or discard them first:" >&2
    git status --short --untracked-files=no >&2
    exit 1
fi
git fetch -q --prune origin
if [ -n "$REF" ]; then
    git checkout -q --detach "$REF"
else
    git checkout -q "$BRANCH"
    git merge -q --ff-only "origin/$BRANCH"
fi
echo "revision $(git rev-parse --short HEAD): $(git log -1 --format=%s)"
REMOTE

if [ "$WITH_DB" = 1 ]; then
    echo "pushing the database, this takes a while..."
    # rsync of a live SQLite file can copy pages mid-write and misses the WAL. VACUUM INTO gives a
    # consistent copy with the WAL folded in.
    SNAP="$(mktemp -d)/investing.sqlite3"
    trap 'rm -rf "$(dirname "$SNAP")"' EXIT
    uv run --frozen python -c "
import sqlite3, sys
db = sqlite3.connect(sys.argv[1])
db.execute('VACUUM INTO ?', (sys.argv[2],))
" "$ROOT/data/investing.sqlite3" "$SNAP"
    # --inplace avoids needing twice the space for rsync's temporary copy.
    rsync -a --inplace --info=progress2 -e "ssh $SSH_OPTS" \
        "$SNAP" "$HOST:$DEST/data/investing.sqlite3"
    # A leftover -wal or -shm would be applied to the new file.
    ssh $SSH_OPTS "$HOST" "rm -f $DEST/data/investing.sqlite3-wal $DEST/data/investing.sqlite3-shm"
fi

# The units name the checkout as @ROOT@.
ssh $SSH_OPTS "$HOST" DEST="$DEST" bash -seuo pipefail <<'REMOTE'
mkdir -p "$DEST/data" ~/.config/systemd/user
for unit in "$DEST"/deploy/*.service "$DEST"/deploy/*.timer; do
    sed "s#@ROOT@#$DEST#g" "$unit" > ~/.config/systemd/user/"$(basename "$unit")"
done
cd "$DEST"
# --locked fails on a uv.lock that does not match pyproject.toml, where the unit's --frozen would not.
~/.local/bin/uv sync --locked --no-dev -q
systemctl --user daemon-reload
systemctl --user enable --now investing.service investing-refresh-daily.timer investing-refresh-weekly.timer
systemctl --user restart investing.service
REMOTE

# The unit is Type=simple, so systemd reports it active before uvicorn binds the socket. Poll it.
echo -n "waiting for the socket"
for _ in $(seq 30); do
    if ssh $SSH_OPTS "$HOST" "curl -fsS --unix-socket $DEST/data/investing.sock http://localhost/health" 2>/dev/null; then
        echo; echo "deployed to $HOST:$DEST"
        exit 0
    fi
    echo -n .; sleep 1
done
echo
echo "the service did not answer on its socket; last log lines:" >&2
ssh $SSH_OPTS "$HOST" 'journalctl --user -u investing.service -n 25 --no-pager' >&2
exit 1
