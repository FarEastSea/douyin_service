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
XHS_ENGINE_CANDIDATE=""
PREVIOUS_XHS_ENGINE=""
XHS_ENGINE_SWITCHED=0

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

prepare_runtime_environment() {
    local requirements_file="$CANDIDATE_DIR/requirements.txt"
    local python_bin="$RUNTIME_VENV/bin/python"
    if ! "$python_bin" -c 'import sys; raise SystemExit(sys.version_info[:2] not in ((3, 11), (3, 12)))'; then
        echo "Deploy failed: BT Panel runtime must use Python 3.11 or 3.12: $RUNTIME_VENV" >&2
        return 1
    fi
    echo "Installing project dependencies into the BT Panel environment..."
    "$python_bin" -m pip install -r "$requirements_file"
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
print("BT Panel environment dependencies OK")
PY
    (
        cd "$CANDIDATE_DIR"
        "$python_bin" -c 'import main; assert main.app is not None; print("BT Panel FastAPI import OK")'
    )
}

prepare_xhs_engine() {
    local lock_file="$CANDIDATE_DIR/xhs-engine.lock"
    local repository=""
    local revision=""
    local uv_version=""
    local engine_root="$SERVICE_ROOT/.xhs-engine"
    local releases_root="$engine_root/releases"
    local incomplete=""

    test -f "$lock_file" || {
        echo "Deploy failed: xhs-engine.lock is missing." >&2
        return 1
    }
    repository="$(sed -n 's/^repository=//p' "$lock_file")"
    revision="$(sed -n 's/^revision=//p' "$lock_file")"
    uv_version="$(sed -n 's/^uv_version=//p' "$lock_file")"
    if [ "$repository" != "https://github.com/Andy-SoulShell/xhs-downloader.git" ] || \
       ! [[ "$revision" =~ ^[0-9a-f]{40}$ ]] || \
       ! [[ "$uv_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        echo "Deploy failed: xhs-engine.lock contains an invalid or unapproved source." >&2
        return 1
    fi

    mkdir -p "$releases_root"
    XHS_ENGINE_CANDIDATE="$releases_root/$revision"
    if [ -x "$XHS_ENGINE_CANDIDATE/source/.venv/bin/xhs-api" ]; then
        echo "Pinned Xiaohongshu collector is already installed: $revision"
        return 0
    fi

    if [ -e "$XHS_ENGINE_CANDIDATE" ]; then
        incomplete="$releases_root/.${revision}.incomplete.$(date +%s)"
        mv "$XHS_ENGINE_CANDIDATE" "$incomplete"
        echo "Moved an incomplete collector installation aside: $incomplete"
    fi
    mkdir "$XHS_ENGINE_CANDIDATE"
    echo "Installing pinned Xiaohongshu collector in an isolated environment..."
    git init "$XHS_ENGINE_CANDIDATE/source"
    git -C "$XHS_ENGINE_CANDIDATE/source" remote add origin "$repository"
    git -C "$XHS_ENGINE_CANDIDATE/source" fetch --depth 1 origin "$revision"
    git -C "$XHS_ENGINE_CANDIDATE/source" checkout --detach FETCH_HEAD
    test "$(git -C "$XHS_ENGINE_CANDIDATE/source" rev-parse HEAD)" = "$revision" || {
        echo "Deploy failed: Xiaohongshu collector revision mismatch." >&2
        return 1
    }
    "$RUNTIME_VENV/bin/python" -m pip install "uv==$uv_version"
    "$RUNTIME_VENV/bin/uv" sync --frozen --no-dev \
        --project "$XHS_ENGINE_CANDIDATE/source" --package xhs-api \
        --python "$RUNTIME_VENV/bin/python"
    test -x "$XHS_ENGINE_CANDIDATE/source/.venv/bin/xhs-api" || {
        echo "Deploy failed: isolated xhs-api executable was not created." >&2
        return 1
    }
    echo "Xiaohongshu collector dependencies OK: $revision"
}

prepare_xhs_browser() {
    local python_bin="$XHS_ENGINE_CANDIDATE/source/.venv/bin/python"
    local browser_cache="$SERVICE_ROOT/.xhs-engine/browser-cache"
    local deps_marker="$browser_cache/.system-deps-ready"
    local executable=""
    test -x "$python_bin" || {
        echo "Deploy failed: isolated Xiaohongshu Python is unavailable." >&2
        return 1
    }
    mkdir -p "$browser_cache"
    if [ ! -f "$deps_marker" ]; then
        if [ "$(id -u)" -eq 0 ] && command -v apt-get >/dev/null 2>&1; then
            echo "Installing Chromium system dependencies for Xiaohongshu profile collection..."
            PLAYWRIGHT_BROWSERS_PATH="$browser_cache" "$python_bin" -m playwright install-deps chromium
            touch "$deps_marker"
        else
            echo "Chromium system dependencies were not changed; validating the existing host libraries."
        fi
    fi
    if ! command -v Xvfb >/dev/null 2>&1 || \
       ! command -v xvfb-run >/dev/null 2>&1 || \
       ! command -v xauth >/dev/null 2>&1; then
        if [ "$(id -u)" -eq 0 ] && command -v apt-get >/dev/null 2>&1; then
            echo "Installing Xvfb for Xiaohongshu browser login..."
            apt-get update
            DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends xvfb xauth
        else
            echo "Deploy failed: Xvfb is required for Xiaohongshu browser login." >&2
            return 1
        fi
    fi
    echo "Installing the pinned Playwright Chromium build..."
    PLAYWRIGHT_BROWSERS_PATH="$browser_cache" "$python_bin" -m playwright install chromium
    executable="$(PLAYWRIGHT_BROWSERS_PATH="$browser_cache" "$python_bin" - <<'PY'
from playwright.sync_api import sync_playwright
with sync_playwright() as playwright:
    print(playwright.chromium.executable_path)
PY
)"
    test -x "$executable" || {
        echo "Deploy failed: Playwright Chromium executable was not created." >&2
        return 1
    }
    PLAYWRIGHT_BROWSERS_PATH="$browser_cache" xvfb-run -a \
        -s "-screen 0 1280x960x24 -nolisten tcp" \
        "$python_bin" - "$executable" <<'PY'
from playwright.sync_api import sync_playwright
import sys
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=False, executable_path=sys.argv[1])
    browser.close()
print("Xiaohongshu Chromium launch check OK")
PY
    if id www >/dev/null 2>&1; then
        chown -R "www:$(id -gn www)" "$browser_cache"
    fi
}

activate_xhs_engine() {
    local engine_root="$SERVICE_ROOT/.xhs-engine"
    local current="$engine_root/current"
    local next="$engine_root/.current.next"
    if [ -e "$current" ] && [ ! -L "$current" ]; then
        echo "Deploy failed: $current must be a managed symlink." >&2
        return 1
    fi
    PREVIOUS_XHS_ENGINE="$(readlink -f "$current" 2>/dev/null || true)"
    rm -f "$next"
    ln -s "$XHS_ENGINE_CANDIDATE" "$next"
    mv -Tf "$next" "$current"
    XHS_ENGINE_SWITCHED=1
}

restore_xhs_engine() {
    local engine_root="$SERVICE_ROOT/.xhs-engine"
    local current="$engine_root/current"
    local next="$engine_root/.current.rollback"
    [ "$XHS_ENGINE_SWITCHED" -eq 1 ] || return 0
    rm -f "$next"
    if [ -n "$PREVIOUS_XHS_ENGINE" ]; then
        ln -s "$PREVIOUS_XHS_ENGINE" "$next"
        mv -Tf "$next" "$current"
    else
        rm -f "$current"
    fi
    XHS_ENGINE_SWITCHED=0
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

files = [Path("main.py"), *Path("app").rglob("*.py"), *Path("integrations").rglob("*.py")]
for path in files:
    compile(path.read_text(encoding="utf-8"), str(path), "exec")
print(f"Python syntax OK: {len(files)} files")
PY
        "$BUILD_VENV/bin/python" -c 'import main; assert main.app is not None; print("FastAPI import OK")'
        "$BUILD_VENV/bin/python" - <<'PY'
import time

from app.core.env_config import validate_env

attempts = 4
for attempt in range(1, attempts + 1):
    status = validate_env()
    if status.get("ready"):
        break
    missing = status.get("missing", [])
    errors = status.get("errors", [])
    storage_errors = [item for item in errors if item.get("key") == "DOWNLOAD_ROOT_ACCESS"]
    other_errors = [item for item in errors if item.get("key") != "DOWNLOAD_ROOT_ACCESS"]
    if missing or other_errors or not storage_errors or attempt == attempts:
        problems = [
            f"{item.get('label') or item.get('key')}: {item.get('message') or '缺少必填配置'}"
            for item in [*missing, *errors]
        ]
        raise SystemExit(
            "Runtime preflight failed before stopping the current service:\n- "
            + "\n- ".join(problems or ["unknown configuration error"])
        )
    print(
        f"Download storage is temporarily unavailable; retrying preflight "
        f"({attempt}/{attempts}) in 5 seconds..."
    )
    time.sleep(5)
print("Runtime configuration, database, Redis and download storage OK")
PY
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

stop_managed_xhs_engine() {
    local pid_file="$SERVICE_ROOT/logs/xhs-api.pid"
    local xvfb_pid_file="$SERVICE_ROOT/logs/xhs-xvfb.pid"
    local pid=""
    local cmdline=""
    if [ -f "$pid_file" ]; then
        pid="$(tr -dc '0-9' < "$pid_file")"
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            cmdline="$(tr '\0' ' ' < "/proc/${pid}/cmdline" 2>/dev/null || true)"
            if ! { { [[ "$cmdline" == *"xhs-api"* ]] && [[ "$cmdline" == *"$SERVICE_ROOT/.xhs-engine/"* ]]; } || \
                   [[ "$cmdline" == *"$SERVICE_ROOT/integrations/xhs_api_launcher.py"* ]]; }; then
                echo "Deploy failed: xhs-api.pid does not belong to this project." >&2
                return 1
            fi
            echo "Stopping managed Xiaohongshu collector (PID=${pid})..."
            kill -TERM "$pid"
            for _ in $(seq 1 20); do
                kill -0 "$pid" 2>/dev/null || break
                sleep 0.5
            done
            if kill -0 "$pid" 2>/dev/null; then
                echo "Deploy failed: Xiaohongshu collector did not stop in time." >&2
                return 1
            fi
            rm -f "$pid_file"
        fi
        rm -f "$pid_file"
    fi
    if [ -f "$xvfb_pid_file" ]; then
        pid="$(tr -dc '0-9' < "$xvfb_pid_file")"
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            cmdline="$(tr '\0' ' ' < "/proc/${pid}/cmdline" 2>/dev/null || true)"
            if [[ "$cmdline" != *"Xvfb :159"* ]]; then
                echo "Deploy failed: xhs-xvfb.pid does not belong to this project." >&2
                return 1
            fi
            echo "Stopping Xiaohongshu virtual display (PID=${pid})..."
            kill -TERM "$pid"
            for _ in $(seq 1 20); do
                kill -0 "$pid" 2>/dev/null || break
                sleep 0.5
            done
            if kill -0 "$pid" 2>/dev/null; then
                echo "Deploy failed: Xiaohongshu virtual display did not stop in time." >&2
                return 1
            fi
        fi
        rm -f "$xvfb_pid_file"
    fi
    return 0
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
xhs_api_url = "http://127.0.0.1:5556"

def get(path, auth=False):
    request = urllib.request.Request(base + path, headers=headers if auth else {})
    with urllib.request.urlopen(request, timeout=5) as response:
        if response.status != 200:
            raise RuntimeError(f"{path} returned HTTP {response.status}")
        return response.read()

def post_xhs(path, payload):
    request = urllib.request.Request(
        xhs_api_url.rstrip("/") + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=40) as response:
        if response.status != 200 and response.status != 202:
            raise RuntimeError(f"Xiaohongshu {path} returned HTTP {response.status}")
        return json.loads(response.read())

last_error = None
for _ in range(int(os.environ.get("SMOKE_ATTEMPTS", "150"))):
    try:
        try:
            readiness = json.loads(get("/api/ready"))
            if readiness.get("status") != "ready":
                raise RuntimeError("dependency readiness payload is not ready")
        except urllib.error.HTTPError as exc:
            if not (allow_legacy_health and exc.code == 404):
                if exc.code == 503:
                    try:
                        readiness = json.loads(exc.read())
                    except Exception:
                        raise
                    failures = [
                        f"{name}: {item.get('message', 'not ready')}"
                        for name, item in readiness.get("components", {}).items()
                        if item.get("ok") is not True
                    ]
                    raise RuntimeError(
                        "readiness failed: " + "; ".join(failures or ["HTTP 503"])
                    ) from exc
                raise
            health = json.loads(get("/api/health"))
            if health.get("status") not in {"healthy", "alive"}:
                raise RuntimeError("legacy health payload is not healthy")
        get("/")
        get("/docs")
        if xhs_api_url:
            with urllib.request.urlopen(xhs_api_url.rstrip("/") + "/health", timeout=5) as response:
                if response.status != 200:
                    raise RuntimeError(f"Xiaohongshu collector returned HTTP {response.status}")
            managed_browser = post_xhs("/browser/managed/start", {})
            if managed_browser.get("state") != "running" or not managed_browser.get("cdp_port"):
                raise RuntimeError("Xiaohongshu managed browser is not running")
            login_status = post_xhs(
                "/xhs/login/status?wait_seconds=30",
                {"request_id": f"deploy-smoke-{os.getpid()}-{int(time.time())}"},
            )
            if login_status.get("status") != "succeeded":
                raise RuntimeError(
                    "Xiaohongshu browser login probe failed: "
                    + str(login_status.get("message") or login_status.get("status"))
                )
        if token:
            authors = json.loads(get("/api/authors/?page=1&page_size=1", True))
            tasks = json.loads(get("/api/tasks/?page=1&page_size=20", True))
            previewable = next((item for item in tasks.get("items", []) if item.get("local_preview_available")), None)
            if previewable:
                get(f"/api/tasks/{previewable.get('id')}/preview", True)
            if not isinstance(authors.get("items"), list) or not isinstance(tasks.get("items"), list):
                raise RuntimeError("management list payload is invalid")
        print("Smoke checks OK: BT Panel runtime, managed browser, home, docs, tasks, authors, media preview when available")
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
    stop_managed_xhs_engine || true
    restore_xhs_engine
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
prepare_runtime_environment
prepare_xhs_engine
prepare_xhs_browser

if [ "$PREVIOUS_SHA" != "$TARGET_SHA" ] || [ -L "$SERVICE_ROOT/.current" ]; then
    # 预检完成后先停止所有旧入口，确保 Jenkins 环境和宝塔环境不会同时运行应用。
    stop_running_instances
    stop_managed_xhs_engine
    CODE_SWITCHED=1
    RESTART_REQUIRED=1
    activate_xhs_engine
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
