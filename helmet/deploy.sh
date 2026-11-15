#!/bin/sh
# Usage: sh deploy.sh [SSH_ALIAS_OR_IP] [ISOLATED_REMOTE_ROOT]
set -eu
BOARD=${1:-rk}
REMOTE_ROOT=${2:-/userdata/helmet-validation/deployment}
case "$BOARD" in *[!A-Za-z0-9.-]*|'') echo 'Invalid board' >&2; exit 1;; esac
case "$REMOTE_ROOT" in /userdata/helmet-validation/*) ;; *) echo 'Invalid remote root' >&2; exit 1;; esac
case "$REMOTE_ROOT" in *[!A-Za-z0-9_./-]*|*/../*|*/..) echo 'Unsafe remote root' >&2; exit 1;; esac
TARGET=$BOARD
[ "$BOARD" = rk ] || TARGET=root@$BOARD
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ssh -o BatchMode=yes -o ConnectTimeout=10 "$TARGET" "mkdir -p $REMOTE_ROOT/src/pc $REMOTE_ROOT/src/legacy_tests $REMOTE_ROOT/repo/helmet/pc $REMOTE_ROOT/repo/helmet/tests $REMOTE_ROOT/repo/tests $REMOTE_ROOT/repo/docs/experiments/2026-11-14 $REMOTE_ROOT/models $REMOTE_ROOT/input $REMOTE_ROOT/results"
scp -o BatchMode=yes "$SCRIPT_DIR"/*.py "$TARGET:$REMOTE_ROOT/src/"
scp -o BatchMode=yes "$SCRIPT_DIR"/*.py "$TARGET:$REMOTE_ROOT/repo/helmet/"
scp -o BatchMode=yes "$SCRIPT_DIR"/pc/*.py "$TARGET:$REMOTE_ROOT/src/pc/"
scp -o BatchMode=yes "$SCRIPT_DIR"/pc/*.py "$TARGET:$REMOTE_ROOT/repo/helmet/pc/"
scp -o BatchMode=yes "$SCRIPT_DIR"/tests/*.py "$TARGET:$REMOTE_ROOT/src/legacy_tests/"
scp -o BatchMode=yes "$SCRIPT_DIR"/tests/*.py "$TARGET:$REMOTE_ROOT/repo/helmet/tests/"
if [ -d "$SCRIPT_DIR/../tests" ]; then
    scp -o BatchMode=yes "$SCRIPT_DIR"/../tests/*.py "$TARGET:$REMOTE_ROOT/repo/tests/"
fi
scp -o BatchMode=yes "$SCRIPT_DIR/../docs/experiments/2026-11-14/verify_runtime.py" "$TARGET:$REMOTE_ROOT/repo/docs/experiments/2026-11-14/"
scp -o BatchMode=yes "$SCRIPT_DIR/../requirements-board.txt" "$SCRIPT_DIR/../requirements-host.txt" "$TARGET:$REMOTE_ROOT/repo/"
scp -o BatchMode=yes "$SCRIPT_DIR"/models/*.rknn "$SCRIPT_DIR/../models/helmet-640-640.rknn" "$TARGET:$REMOTE_ROOT/models/"
scp -o BatchMode=yes "$SCRIPT_DIR/../models/1.jpg" "$TARGET:$REMOTE_ROOT/input/1.jpg"
ssh -o BatchMode=yes -o ConnectTimeout=10 "$TARGET" "cd $REMOTE_ROOT/src && python3 -m compileall -q . && python3 helmet_realtime.py --help && cd $REMOTE_ROOT/repo && python3 -m compileall -q helmet tests && python3 -m unittest discover -s tests -p test_sources.py -v && sha256sum $REMOTE_ROOT/models/*.rknn"
printf 'Verified deployment: %s\n' "$REMOTE_ROOT"
