#!/bin/bash
set -Eeuo pipefail

# 宝塔面板是唯一运行管理器：Jenkins 只负责候选版本预检、根目录代码切换、
# 停止旧进程和发布后验证，绝不使用预检虚拟环境启动应用。
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
BUILD_VENV="${VENV_DIR:-$SERVICE_ROOT/.venv}"
RUNTIME_VENV="${REMOTE_PYTHON_ENV:-}"
BASE_PYTHON="${PYTHON_BIN:-}"
CANDIDATE_DIR=""
PREVIOUS_SHA=""
CODE_SWITCHED=0
ROLLING_BACK=0
RESTART_REQUIRED=0

validate_layout() {
    test -d "$SERVICE_ROOT/.git" || {
        echo "Deploy failed: $SERVICE_ROOT is not a Git repository." >&2
        return 1
    }
    command -v git >/dev/null
    command -v tar >/dev/null
    command -v pgrep >/dev/null
    command -v readlink >/dev/null
    if [ -z "$RUNTIME_VENV" ] || [ ! -x "$RUNTIME_VENV/bin/python" ] || [ ! -x "$RUNTIME_VENV/bin/gunicorn" ]; then
        echo "Deploy failed: REMOTE_PYTHON_ENV must point to the BT Panel environment containing bin/python and bin/gunicorn." >&2
        return 1
    fi
    RUNTIME_VENV="$(cd "$RUNTIME_VENV" && pwd -P)"
}

validate_runtime_environment() {
    local requirements_file="$CANDIDATE_DIR/requirements.txt"
    local python_bin="$RUNTIME_VENV/bin/python"
    if ! "$python_bin" -c 'import sys; raise SystemExit(sys.version_info[:2] not in ((3, 11), (3, 12)))'; then
        echo "Deploy failed: BT Panel runtime must use Python 3.11 or 3.12: $RUNTIME_VENV" >&2
        return 1
    fi
    "$python_bin" -m pip check
    "$python_bin" - "$requirements_file" <<'PY'
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import sys

try:
    from packaging.requirements import Requirement
except ImportError:
    from pip._vendor.packaging.requirements import Requirement

errors = []
for raw_line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#"):
        continue
    requirement = Requirement(line)
    if requirement.marker and not requirement.marker.evaluate():
        continue
    try:
        installed = version(requirement.name)
    except PackageNotFoundError:
        errors.append(f"{requirement.name} is not installed")
        continue
    if requirement.specifier and installed not in requirement.specifier:
        errors.append(f"{requirement.name}=={installed} does not satisfy {requirement.specifier}")
if errors:
    raise SystemExit("BT Panel environment is incompatible:\n- " + "\n- ".join(errors))
print("BT Panel environment requirements OK")
PY
    (
        cd "$CANDIDATE_DIR"
        "$python_bin" -c 'import main; assert main.app is not None; print("BT Panel FastAPI import OK")'
    )
}

select_build_python() {
    if [ -x "$BUILD_VENV/bin/python" ]; then
        if ! "$BUILD_VENV/bin/python" -c 'import sys; raise SystemExit(sys.version_info[:2] not in ((3, 11), (3, 12)))'; then
            echo "Deploy failed: Jenkins build environment must use Python 3.11 or 3.12: $BUILD_VENV" >&2
            return 1
        fi
        return 0
    fi

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
        echo "Deploy failed: Python 3.11 or 3.12 is required for the Jenkins build environment." >&2
        return 1
    fi
    "$BASE_PYTHON" -m venv "$BUILD_VENV"
}

prepare_candidate() {
    CANDIDATE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/${PROJECT_NAME}-candidate.XXXXXX")"
    git archive "$TARGET_SHA" | tar -x -C "$CANDIDATE_DIR"
    ln -s "$SERVICE_ROOT/.env" "$CANDIDATE_DIR/.env"
    ln -s "$SERVICE_ROOT/logs" "$CANDIDATE_DIR/logs"

    select_build_python
    "$BUILD_VENV/bin/python" -m pip install --upgrade pip
    "$BUILD_VENV/bin/python" -m pip install -r "$CANDIDATE_DIR/requirements.txt"
}

preflight() {
    (
        trap - ERR
        cd "$CANDIDATE_DIR"
        "$BUILD_VENV/bin/python" - <<'PY'
from pathlib import Path

files = [Path("main.py"), *Path("app").rglob("*.py")]
for path in files:
    compile(path.read_text(encoding="utf-8"), str(path), "exec")
print(f"Python syntax OK: {len(files)} files")
PY
        "$BUILD_VENV/bin/python" -c 'import main; assert main.app is not None; print("FastAPI import OK")'
        "$BUILD_VENV/bin/python" - <<'PY'
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
        # 使用 Jenkins 预检环境执行向后兼容的数据库迁移；不启动 Web/Celery 进程。
        "$BUILD_VENV/bin/python" -c 'from app.models.database import init_db_sync; init_db_sync(); print("Database migrations OK")'
    )
}

matches_service_process() {
    local pid="$1"
    local cmdline=""
    local process_cwd=""
    cmdline="$(tr '\0' ' ' < "/proc/${pid}/cmdline" 2>/dev/null || true)"
    process_cwd="$(readlink -f "/proc/${pid}/cwd" 2>/dev/null || true)"

    if [ "$process_cwd" = "$SERVICE_ROOT" ] && \
       { [[ "$cmdline" == *"gunicorn"* && "$cmdline" == *"main:app"* ]] || \
         [[ "$cmdline" == *"uvicorn"* && "$cmdline" == *"main:app"* ]] || \
         [[ "$cmdline" == *"main.py"* ]]; }; then
        return 0
    fi
    # 兼容上一版 Jenkins 从 .current 启动的 Gunicorn，仅用于迁移时停止旧实例。
    if [[ "$cmdline" == *"gunicorn"* && "$cmdline" == *"main:app"* && "$cmdline" == *"$SERVICE_ROOT/.current"* ]]; then
        return 0
    fi
    return 1
}

stop_running_instances() {
    local pid=""
    local found=0
    while IFS= read -r pid; do
        [ -n "$pid" ] || continue
        if matches_service_process "$pid"; then
            echo "Stopping existing application process (PID=${pid})..."
            kill -TERM "$pid" 2>/dev/null || true
            found=1
        fi
    done < <(pgrep -f "gunicorn.*main:app|uvicorn.*main:app|main.py" 2>/dev/null || true)

    if [ "$found" -eq 0 ]; then
        rm -f "$SERVICE_ROOT/logs/gunicorn.pid"
        echo "No running application process found; BT Panel watchdog may already be restarting it."
        return 0
    fi
    for _ in $(seq 1 20); do
        local alive=0
        while IFS= read -r pid; do
            [ -n "$pid" ] || continue
            if matches_service_process "$pid"; then
                alive=1
                break
            fi
        done < <(pgrep -f "gunicorn.*main:app|uvicorn.*main:app|main.py" 2>/dev/null || true)
        if [ "$alive" -eq 0 ]; then
            rm -f "$SERVICE_ROOT/logs/gunicorn.pid"
            return 0
        fi
        sleep 0.5
    done
    echo "Deploy failed: the previous application process did not stop in time." >&2
    return 1
}

start_runtime() {
    echo "Starting root application with the BT Panel Python environment..."
    APP_PORT="$PORT" VENV_DIR="$RUNTIME_VENV" RUNTIME_DIR="$SERVICE_ROOT" bash "$SERVICE_ROOT/start.sh"
}

smoke_check() {
    local allow_legacy_health="${1:-0}"
    local attempts="${2:-150}"
    (
        trap - ERR
        cd "$SERVICE_ROOT"
        APP_PORT="$PORT" ALLOW_LEGACY_HEALTH="$allow_legacy_health" SMOKE_ATTEMPTS="$attempts" \
            "$BUILD_VENV/bin/python" - <<'PY'
import json, os, time, urllib.error, urllib.request
from app.core.config import settings

base = f"http://127.0.0.1:{os.environ['APP_PORT']}"
allow_legacy_health = os.environ.get("ALLOW_LEGACY_HEALTH") == "1"
token = settings.ADMIN_TOKEN
headers = {"Authorization": f"Bearer {token}"} if token else {}

def get(path, auth=False):
    request = urllib.request.Request(base + path, headers=headers if auth else {})
    with urllib.request.urlopen(request, timeout=5) as response:
        if response.status != 200:
            raise RuntimeError(f"{path} returned HTTP {response.status}")
        return response.read()

last_error = None
for _ in range(int(os.environ.get("SMOKE_ATTEMPTS", "150"))):
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
        print("Smoke checks OK: BT Panel runtime, dependencies, home, docs, tasks, authors, media preview when available")
        raise SystemExit(0)
    except Exception as exc:
        last_error = exc
        time.sleep(1)
raise SystemExit(f"Smoke checks failed: {last_error}")
PY
    )
}

cleanup_candidate() {
    if [ -n "$CANDIDATE_DIR" ] && [ -d "$CANDIDATE_DIR" ]; then
        case "$CANDIDATE_DIR" in
            "${TMPDIR:-/tmp}"/"$PROJECT_NAME"-candidate.*) rm -rf -- "$CANDIDATE_DIR" ;;
        esac
    fi
}

cleanup_legacy_release_layout() {
    local legacy_releases="$SERVICE_ROOT/.releases"
    if [ -L "$SERVICE_ROOT/.current" ]; then
        rm -f "$SERVICE_ROOT/.current"
    fi
    if [ -L "$legacy_releases" ]; then
        rm -f "$legacy_releases"
    elif [ -d "$legacy_releases" ] && [ "$(readlink -f "$legacy_releases")" = "$legacy_releases" ]; then
        rm -rf -- "$legacy_releases"
    fi
}

rollback() {
    local exit_code=$?
    trap - ERR
    if [ "$ROLLING_BACK" -eq 1 ] || [ "$CODE_SWITCHED" -eq 0 ] || [ -z "$PREVIOUS_SHA" ]; then
        cleanup_candidate
        exit "$exit_code"
    fi
    ROLLING_BACK=1
    echo "Deploy failed; restoring root worktree to ${PREVIOUS_SHA}..." >&2
    stop_running_instances || true
    git reset --hard "$PREVIOUS_SHA"
    start_runtime
    smoke_check 1 30
    cleanup_candidate
    echo "Rollback completed. BT Panel is running the previous root version." >&2
    exit "$exit_code"
}

trap rollback ERR
validate_layout
mkdir -p "$SERVICE_ROOT/logs"

if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "Deploy aborted: tracked files contain local changes in the service root." >&2
    exit 1
fi

echo "Preparing ${PROJECT_NAME} deployment for BT Panel..."
PREVIOUS_SHA="$(git rev-parse HEAD)"
git fetch origin "$BRANCH"
if [ -z "$TARGET_SHA" ]; then
    TARGET_SHA="$(git rev-parse "origin/$BRANCH")"
fi
if ! git cat-file -e "${TARGET_SHA}^{commit}" 2>/dev/null; then
    echo "Deploy failed: target commit ${TARGET_SHA} is unavailable." >&2
    exit 1
fi

prepare_candidate
preflight
validate_runtime_environment

if [ "$PREVIOUS_SHA" != "$TARGET_SHA" ] || [ -L "$SERVICE_ROOT/.current" ]; then
    # 预检完成后先停止所有旧入口，确保 Jenkins 环境和宝塔环境不会同时运行应用。
    stop_running_instances
    CODE_SWITCHED=1
    RESTART_REQUIRED=1
    git reset --hard "$TARGET_SHA"
fi

if [ "$RESTART_REQUIRED" -eq 1 ]; then
    start_runtime
fi
smoke_check 0 30

CODE_SWITCHED=0
trap - ERR
cleanup_legacy_release_layout
cleanup_candidate
echo "Deploy success. ${PROJECT_NAME} root worktree is at ${TARGET_SHA}; Jenkins restarted it with the BT Panel environment."
