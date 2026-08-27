#!/bin/bash
set -Eeuo pipefail

PROJECT_DIR="${PROJECT_DIR_OVERRIDE:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
PROJECT_NAME="$(basename "$PROJECT_DIR")"
BRANCH="${DEPLOY_BRANCH:-main}"
PORT="${APP_PORT:-15000}"
VENV_DIR="${VENV_DIR:-$PROJECT_DIR/.venv}"
PYTHON_BIN="${PYTHON_BIN:-}"
LOG_DIR="$PROJECT_DIR/logs"
PREVIOUS_SHA=""
TARGET_SHA="${DEPLOY_TARGET_SHA:-}"
CODE_SWITCHED=0
ROLLING_BACK=0

select_python() {
    if [ -x "$VENV_DIR/bin/python" ]; then return 0; fi
    if [ -z "$PYTHON_BIN" ]; then
        for candidate in python3.12 python3.11; do
            if command -v "$candidate" >/dev/null 2>&1; then PYTHON_BIN="$candidate"; break; fi
        done
    fi
    if [ -z "$PYTHON_BIN" ] || ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(sys.version_info[:2] not in ((3, 11), (3, 12)))'; then
        echo "Deploy failed: Python 3.11 or 3.12 is required. Set PYTHON_BIN to a supported interpreter." >&2
        return 1
    fi
    "$PYTHON_BIN" -m venv "$VENV_DIR"
}

install_dependencies() {
    "$VENV_DIR/bin/python" -m pip install --upgrade pip
    "$VENV_DIR/bin/python" -m pip install -r "$PROJECT_DIR/requirements.txt"
}

preflight() {
    "$VENV_DIR/bin/python" -c 'from pathlib import Path; files=[Path("main.py"), *Path("app").rglob("*.py")]; [compile(path.read_text(encoding="utf-8"), str(path), "exec") for path in files]; print(f"Python syntax OK: {len(files)} files")'
    "$VENV_DIR/bin/python" -c 'import main; assert main.app is not None; print("FastAPI import OK")'
    test -s "$PROJECT_DIR/static/app/index.html"
}

smoke_check() {
    APP_PORT="$PORT" "$VENV_DIR/bin/python" -c '
import json, os, time, urllib.request
from app.core.config import settings
base = f"http://127.0.0.1:{os.environ[\"APP_PORT\"]}"
token = settings.ADMIN_TOKEN
headers = {"Authorization": f"Bearer {token}"} if token else {}
def get(path, auth=False):
    request = urllib.request.Request(base + path, headers=headers if auth else {})
    with urllib.request.urlopen(request, timeout=5) as response:
        if response.status != 200: raise RuntimeError(f"{path} returned HTTP {response.status}")
        return response.read()
last_error = None
for _ in range(30):
    try:
        health = json.loads(get("/api/health"))
        if health.get("status") != "healthy": raise RuntimeError("health payload is not healthy")
        get("/"); get("/docs")
        if token:
            authors = json.loads(get("/api/authors/?page=1&page_size=1", True))
            tasks = json.loads(get("/api/tasks/?page=1&page_size=20", True))
            previewable = next((item for item in tasks.get("items", []) if item.get("local_preview_available")), None)
            if previewable:
                preview_id = previewable.get("id")
                get(f"/api/tasks/{preview_id}/preview", True)
            if not isinstance(authors.get("items"), list) or not isinstance(tasks.get("items"), list): raise RuntimeError("management list payload is invalid")
        print("Smoke checks OK: health, home, docs, tasks, authors, media preview when available")
        raise SystemExit(0)
    except Exception as exc:
        last_error = exc; time.sleep(1)
raise SystemExit(f"Smoke checks failed: {last_error}")
'
}

start_service() { APP_PORT="$PORT" VENV_DIR="$VENV_DIR" "$PROJECT_DIR/start.sh"; }

rollback() {
    local exit_code=$?
    if [ "$ROLLING_BACK" -eq 1 ] || [ "$CODE_SWITCHED" -eq 0 ] || [ -z "$PREVIOUS_SHA" ]; then exit "$exit_code"; fi
    ROLLING_BACK=1
    trap - ERR
    echo "Deploy failed; rolling back to ${PREVIOUS_SHA}..." >&2
    "$PROJECT_DIR/stop.sh" || true
    git reset --hard "$PREVIOUS_SHA"
    install_dependencies || true
    start_service
    smoke_check
    echo "Rollback completed. Service restored to ${PREVIOUS_SHA}." >&2
    exit "$exit_code"
}

trap rollback ERR
cd "$PROJECT_DIR"
mkdir -p "$LOG_DIR"
if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "Deploy aborted: tracked files contain local changes." >&2
    exit 1
fi

echo "Preparing ${PROJECT_NAME} deployment..."
select_python
PREVIOUS_SHA="$(git rev-parse HEAD)"
git fetch origin "$BRANCH"
if [ -z "$TARGET_SHA" ]; then
    TARGET_SHA="$(git rev-parse "origin/$BRANCH")"
fi
if ! git cat-file -e "${TARGET_SHA}^{commit}" 2>/dev/null; then
    echo "Deploy failed: target commit ${TARGET_SHA} is unavailable." >&2
    exit 1
fi
if [ "$PREVIOUS_SHA" = "$TARGET_SHA" ]; then
    echo "Already up to date at ${TARGET_SHA}."
    smoke_check
    exit 0
fi

git reset --hard "$TARGET_SHA"
CODE_SWITCHED=1
install_dependencies
preflight
chmod +x "$PROJECT_DIR/start.sh" "$PROJECT_DIR/stop.sh"
"$PROJECT_DIR/stop.sh"
start_service
smoke_check

CODE_SWITCHED=0
trap - ERR
echo "Deploy success. ${PROJECT_NAME} updated from ${PREVIOUS_SHA} to ${TARGET_SHA} on port ${PORT}."
