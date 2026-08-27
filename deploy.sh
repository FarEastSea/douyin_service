#!/bin/bash
set -Eeuo pipefail

# 远端仓库目录只负责取得版本；运行代码放在独立 release 中。
# .env 与 logs 始终留在服务根目录，版本切换不会覆盖运行配置和数据。
SERVICE_ROOT="${PROJECT_DIR_OVERRIDE:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
cd "$SERVICE_ROOT"
SERVICE_ROOT="$(pwd -P)"

if [ "$SERVICE_ROOT" = "/" ]; then
    echo "Deploy failed: service root cannot be /." >&2
    exit 1
fi

PROJECT_NAME="$(basename "$SERVICE_ROOT")"
BRANCH="${DEPLOY_BRANCH:-main}"
PORT="${APP_PORT:-15000}"
TARGET_SHA="${DEPLOY_TARGET_SHA:-}"
RELEASE_RETENTION="${RELEASE_RETENTION:-5}"
RELEASES_DIR="$SERVICE_ROOT/.releases"
CURRENT_LINK="$SERVICE_ROOT/.current"
LOG_DIR="$SERVICE_ROOT/logs"
BASE_PYTHON="${PYTHON_BIN:-}"

PREVIOUS_RELEASE_DIR=""
PREVIOUS_CONTROL_DIR=""
CANDIDATE_DIR=""
NEW_RELEASE_DIR=""
CODE_SWITCHED=0
ROLLING_BACK=0

validate_layout() {
    test -d "$SERVICE_ROOT/.git" || {
        echo "Deploy failed: $SERVICE_ROOT is not the deployment source repository." >&2
        return 1
    }
    if [ -e "$CURRENT_LINK" ] && [ ! -L "$CURRENT_LINK" ]; then
        echo "Deploy failed: $CURRENT_LINK exists but is not a symbolic link." >&2
        return 1
    fi
    case "$RELEASE_RETENTION" in
        ''|*[!0-9]*) echo "Deploy failed: RELEASE_RETENTION must be a positive integer." >&2; return 1 ;;
    esac
    [ "$RELEASE_RETENTION" -ge 2 ] || {
        echo "Deploy failed: RELEASE_RETENTION must be at least 2 for rollback safety." >&2
        return 1
    }
    command -v git >/dev/null
    command -v tar >/dev/null
    command -v readlink >/dev/null
}

select_python() {
    if [ -n "$BASE_PYTHON" ]; then
        command -v "$BASE_PYTHON" >/dev/null 2>&1 || {
            echo "Deploy failed: PYTHON_BIN is not executable." >&2
            return 1
        }
    else
        for candidate in python3.12 python3.11; do
            if command -v "$candidate" >/dev/null 2>&1; then
                BASE_PYTHON="$candidate"
                break
            fi
        done
    fi
    if [ -z "$BASE_PYTHON" ] || ! "$BASE_PYTHON" -c 'import sys; raise SystemExit(sys.version_info[:2] not in ((3, 11), (3, 12)))'; then
        echo "Deploy failed: Python 3.11 or 3.12 is required. Set PYTHON_BIN to a supported interpreter." >&2
        return 1
    fi
}

install_dependencies() {
    local release_dir="$1"
    "$BASE_PYTHON" -m venv "$release_dir/.venv"
    "$release_dir/.venv/bin/python" -m pip install --upgrade pip
    "$release_dir/.venv/bin/python" -m pip install -r "$release_dir/requirements.txt"
}

preflight() {
    local release_dir="$1"
    (
        trap - ERR
        cd "$release_dir"
        .venv/bin/python - <<'PY'
from pathlib import Path

files = [Path("main.py"), *Path("app").rglob("*.py")]
for path in files:
    compile(path.read_text(encoding="utf-8"), str(path), "exec")
print(f"Python syntax OK: {len(files)} files")
PY
        .venv/bin/python -c 'import main; assert main.app is not None; print("FastAPI import OK")'
        .venv/bin/python - <<'PY'
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

index = Path("static/app/index.html")
if not index.is_file() or index.stat().st_size == 0:
    raise SystemExit("Frontend preflight failed: static/app/index.html is missing")

class AssetParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.assets = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        value = values.get("src") if tag == "script" else values.get("href") if tag == "link" else None
        if value and value.startswith("/static/app/"):
            self.assets.append(value)

parser = AssetParser()
parser.feed(index.read_text(encoding="utf-8"))
if not parser.assets:
    raise SystemExit("Frontend preflight failed: no production assets are referenced")
missing = [asset for asset in parser.assets if not Path(urlsplit(asset).path.lstrip("/")).is_file()]
if missing:
    raise SystemExit(f"Frontend preflight failed: missing assets: {missing}")
print(f"Frontend asset integrity OK: {len(parser.assets)} referenced assets")
PY
        # 在停旧版本前执行连接和迁移校验；失败不会切换代码或中断现有服务。
        .venv/bin/python -c 'from app.models.database import init_db_sync; init_db_sync(); print("Database migrations OK")'
    )
}

smoke_check() {
    local release_dir="$1"
    local allow_legacy_health="${2:-0}"
    (
        trap - ERR
        cd "$release_dir"
        APP_PORT="$PORT" ALLOW_LEGACY_HEALTH="$allow_legacy_health" "$release_dir/.venv/bin/python" - <<'PY'
import json, os, time, urllib.error, urllib.request
from app.core.config import settings

port = os.environ["APP_PORT"]
allow_legacy_health = os.environ.get("ALLOW_LEGACY_HEALTH") == "1"
base = f"http://127.0.0.1:{port}"
token = settings.ADMIN_TOKEN
headers = {"Authorization": f"Bearer {token}"} if token else {}

def get(path, auth=False):
    request = urllib.request.Request(base + path, headers=headers if auth else {})
    with urllib.request.urlopen(request, timeout=5) as response:
        if response.status != 200:
            raise RuntimeError(f"{path} returned HTTP {response.status}")
        return response.read()

last_error = None
for _ in range(30):
    try:
        try:
            readiness = json.loads(get("/api/ready"))
            if readiness.get("status") != "ready":
                raise RuntimeError("dependency readiness payload is not ready")
        except urllib.error.HTTPError as exc:
            if not (allow_legacy_health and exc.code == 404):
                raise
            health = json.loads(get("/api/health"))
            if health.get("status") not in {"healthy", "alive"}:
                raise RuntimeError("legacy health payload is not healthy")
        get("/")
        get("/docs")
        if token:
            authors = json.loads(get("/api/authors/?page=1&page_size=1", True))
            tasks = json.loads(get("/api/tasks/?page=1&page_size=20", True))
            previewable = next((item for item in tasks.get("items", []) if item.get("local_preview_available")), None)
            if previewable:
                get(f"/api/tasks/{previewable.get('id')}/preview", True)
            if not isinstance(authors.get("items"), list) or not isinstance(tasks.get("items"), list):
                raise RuntimeError("management list payload is invalid")
        print("Smoke checks OK: dependencies ready, home, docs, tasks, authors, media preview when available")
        raise SystemExit(0)
    except Exception as exc:
        last_error = exc
        time.sleep(1)
raise SystemExit(f"Smoke checks failed: {last_error}")
PY
    )
}

start_service() {
    local control_dir="$1"
    APP_PORT="$PORT" VENV_DIR="$control_dir/.venv" RUNTIME_DIR="$SERVICE_ROOT" bash "$control_dir/start.sh"
}

stop_service() {
    local control_dir="$1"
    RUNTIME_DIR="$SERVICE_ROOT" bash "$control_dir/stop.sh"
}

switch_current() {
    local target_dir="$1"
    local next_link="$SERVICE_ROOT/.current.next.$$"
    rm -f "$next_link"
    ln -s "$target_dir" "$next_link"
    mv -Tf "$next_link" "$CURRENT_LINK"
}

restore_previous_release() {
    if [ "$PREVIOUS_RELEASE_DIR" = "$SERVICE_ROOT" ]; then
        if [ -L "$CURRENT_LINK" ]; then
            rm -f "$CURRENT_LINK"
        fi
        PREVIOUS_CONTROL_DIR="$SERVICE_ROOT"
    else
        switch_current "$PREVIOUS_RELEASE_DIR"
        PREVIOUS_CONTROL_DIR="$CURRENT_LINK"
    fi
}

cleanup_candidate() {
    if [ -n "$CANDIDATE_DIR" ] && [ -d "$CANDIDATE_DIR" ]; then
        case "$CANDIDATE_DIR" in
            "$RELEASES_DIR"/.preparing-*) rm -rf -- "$CANDIDATE_DIR" ;;
        esac
    fi
}

rollback() {
    local exit_code=$?
    trap - ERR
    cleanup_candidate
    if [ "$ROLLING_BACK" -eq 1 ] || [ "$CODE_SWITCHED" -eq 0 ] || [ -z "$PREVIOUS_RELEASE_DIR" ]; then
        exit "$exit_code"
    fi
    ROLLING_BACK=1
    echo "Deploy failed; restoring previous release ${PREVIOUS_RELEASE_DIR}..." >&2
    stop_service "$CURRENT_LINK" || true
    restore_previous_release
    start_service "$PREVIOUS_CONTROL_DIR"
    smoke_check "$PREVIOUS_CONTROL_DIR" 1
    echo "Rollback completed. Previous release is active again." >&2
    exit "$exit_code"
}

prune_releases() {
    local active_dir
    local kept=0
    active_dir="$(readlink -f "$CURRENT_LINK")"
    while IFS= read -r release_dir; do
        [ -n "$release_dir" ] || continue
        if [ "$release_dir" = "$active_dir" ] || [ "$kept" -lt "$RELEASE_RETENTION" ]; then
            kept=$((kept + 1))
            continue
        fi
        case "$release_dir" in
            "$RELEASES_DIR"/"$PROJECT_NAME"-*) rm -rf -- "$release_dir" ;;
        esac
    done < <(
        find "$RELEASES_DIR" -mindepth 2 -maxdepth 2 -name .release-ready -type f \
            -printf '%T@ %h\n' | sort -rn | cut -d' ' -f2-
    )
}

trap rollback ERR
validate_layout
mkdir -p "$RELEASES_DIR" "$LOG_DIR"

if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "Deploy aborted: tracked files contain local changes in the source repository." >&2
    exit 1
fi

echo "Preparing ${PROJECT_NAME} deployment..."
git fetch origin "$BRANCH"
if [ -z "$TARGET_SHA" ]; then
    TARGET_SHA="$(git rev-parse "origin/$BRANCH")"
fi
if ! git cat-file -e "${TARGET_SHA}^{commit}" 2>/dev/null; then
    echo "Deploy failed: target commit ${TARGET_SHA} is unavailable." >&2
    exit 1
fi

if [ -L "$CURRENT_LINK" ]; then
    PREVIOUS_RELEASE_DIR="$(readlink -f "$CURRENT_LINK")"
    PREVIOUS_CONTROL_DIR="$CURRENT_LINK"
    if [ -f "$PREVIOUS_RELEASE_DIR/.deploy-sha" ] && [ "$(cat "$PREVIOUS_RELEASE_DIR/.deploy-sha")" = "$TARGET_SHA" ]; then
        echo "Already active at ${TARGET_SHA}."
        smoke_check "$CURRENT_LINK"
        trap - ERR
        exit 0
    fi
else
    PREVIOUS_RELEASE_DIR="$SERVICE_ROOT"
    PREVIOUS_CONTROL_DIR="$SERVICE_ROOT"
fi

select_python
release_stamp="$(date -u +%Y%m%d%H%M%S)"
release_name="${PROJECT_NAME}-${TARGET_SHA:0:12}-${release_stamp}"
CANDIDATE_DIR="$RELEASES_DIR/.preparing-${release_name}-$$"
NEW_RELEASE_DIR="$RELEASES_DIR/$release_name"
mkdir -p "$CANDIDATE_DIR"
git archive "$TARGET_SHA" | tar -x -C "$CANDIDATE_DIR"
ln -s "$SERVICE_ROOT/.env" "$CANDIDATE_DIR/.env"
ln -s "$LOG_DIR" "$CANDIDATE_DIR/logs"

install_dependencies "$CANDIDATE_DIR"
preflight "$CANDIDATE_DIR"
printf '%s\n' "$TARGET_SHA" > "$CANDIDATE_DIR/.deploy-sha"
touch "$CANDIDATE_DIR/.release-ready"
mv "$CANDIDATE_DIR" "$NEW_RELEASE_DIR"
CANDIDATE_DIR=""

chmod +x "$NEW_RELEASE_DIR/start.sh" "$NEW_RELEASE_DIR/stop.sh"
stop_service "$PREVIOUS_CONTROL_DIR"
switch_current "$NEW_RELEASE_DIR"
CODE_SWITCHED=1
start_service "$CURRENT_LINK"
smoke_check "$CURRENT_LINK"

CODE_SWITCHED=0
trap - ERR
if ! prune_releases; then
    echo "Deploy warning: release cleanup failed; active release is unaffected." >&2
fi
echo "Deploy success. ${PROJECT_NAME} switched to ${TARGET_SHA} on port ${PORT}."
