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
DEPENDENCIES_CHANGED=0
DEPENDENCY_BACKUP=""
DATABASE_BACKUP=""
DATABASE_MIGRATED=0
MAINTENANCE_STARTED=0
RUNTIME_PREVIOUS_BUILD=""
OLD_RUNTIME_STATE=""
PANEL_PYTHON=""
PANEL_STATE=""
PANEL_CAPTURED=0
PANEL_INHIBITED=0
FINALIZATION_STARTED=0

validate_layout() {
    test -d "$SERVICE_ROOT/.git" || {
        echo "Deploy failed: $SERVICE_ROOT is not a Git repository." >&2
        return 1
    }
    command -v git >/dev/null
    command -v tar >/dev/null
    command -v pgrep >/dev/null
    command -v readlink >/dev/null
    # 宝塔可返回环境根目录或其中的 bin 目录，统一后所有阶段使用同一解释器。
    if [ -n "$RUNTIME_VENV" ] && [ ! -x "$RUNTIME_VENV/bin/python" ] && \
       [ -x "$RUNTIME_VENV/python" ] && [ -x "$RUNTIME_VENV/gunicorn" ]; then
        RUNTIME_VENV="$(cd "$RUNTIME_VENV/.." && pwd -P)"
    fi
    if [ -z "$RUNTIME_VENV" ] || [ ! -x "$RUNTIME_VENV/bin/python" ] || [ ! -x "$RUNTIME_VENV/bin/gunicorn" ]; then
        echo "Deploy failed: REMOTE_PYTHON_ENV must point to the BT Panel environment containing bin/python and bin/gunicorn." >&2
        return 1
    fi
    RUNTIME_VENV="$(cd "$RUNTIME_VENV" && pwd -P)"
    PANEL_PYTHON="/www/server/panel/pyenv/bin/python"
    if [ ! -x "$PANEL_PYTHON" ]; then
        echo "Deploy failed: the verified BT Panel project runtime is unavailable." >&2
        return 1
    fi
    # 仅扩展本次发布进程的 PATH，不修改服务器的全局环境。
    if ! command -v pg_dump >/dev/null || ! command -v pg_restore >/dev/null || ! command -v psql >/dev/null; then
        if [ -x /www/server/pgsql/bin/pg_dump ] && [ -x /www/server/pgsql/bin/pg_restore ] && \
           [ -x /www/server/pgsql/bin/psql ]; then
            export PATH="/www/server/pgsql/bin:$PATH"
        fi
    fi
}

prepare_runtime_environment() {
    local requirements_file="$CANDIDATE_DIR/requirements.txt"
    local python_bin="$RUNTIME_VENV/bin/python"
    if ! "$python_bin" -c 'import sys; raise SystemExit(sys.version_info[:2] not in ((3, 11), (3, 12)))'; then
        echo "Deploy failed: BT Panel runtime must use Python 3.11 or 3.12: $RUNTIME_VENV" >&2
        return 1
    fi
    echo "Installing project dependencies into the BT Panel environment..."
    DEPENDENCIES_CHANGED=1
    (cd "$CANDIDATE_DIR" && sha256sum --check wheelhouse.sha256)
    "$python_bin" -m pip install --no-index --find-links "$CANDIDATE_DIR/.wheelhouse" \
        -r "$CANDIDATE_DIR/resolved-requirements.txt"
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

    mkdir -p "$releases_root" || return 1
    XHS_ENGINE_CANDIDATE="$releases_root/$revision"
    if [ -x "$XHS_ENGINE_CANDIDATE/source/.venv/bin/xhs-api" ]; then
        echo "Pinned Xiaohongshu collector is already installed: $revision"
        return 0
    fi

    if [ -e "$XHS_ENGINE_CANDIDATE" ]; then
        incomplete="$releases_root/.${revision}.incomplete.$(date +%s)"
        mv "$XHS_ENGINE_CANDIDATE" "$incomplete" || return 1
        echo "Moved an incomplete collector installation aside: $incomplete"
    fi
    mkdir "$XHS_ENGINE_CANDIDATE" || return 1
    echo "Installing pinned Xiaohongshu collector in an isolated environment..."
    git init "$XHS_ENGINE_CANDIDATE/source" || return 1
    git -C "$XHS_ENGINE_CANDIDATE/source" remote add origin "$repository" || return 1
    git -C "$XHS_ENGINE_CANDIDATE/source" fetch --depth 1 origin "$revision" || return 1
    git -C "$XHS_ENGINE_CANDIDATE/source" checkout --detach FETCH_HEAD || return 1
    test "$(git -C "$XHS_ENGINE_CANDIDATE/source" rev-parse HEAD)" = "$revision" || {
        echo "Deploy failed: Xiaohongshu collector revision mismatch." >&2
        return 1
    }
    "$BUILD_VENV/bin/python" -m pip install "uv==$uv_version" || return 1
    "$BUILD_VENV/bin/uv" sync --frozen --no-dev \
        --project "$XHS_ENGINE_CANDIDATE/source" --package xhs-api \
        --python "$RUNTIME_VENV/bin/python" || return 1
    test -x "$XHS_ENGINE_CANDIDATE/source/.venv/bin/xhs-api" || {
        echo "Deploy failed: isolated xhs-api executable was not created." >&2
        return 1
    }
    echo "Xiaohongshu collector dependencies OK: $revision"
}

activate_xhs_engine() {
    [ -n "$XHS_ENGINE_CANDIDATE" ] || return 0
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

    # 与生产使用相同解释器构建全新环境，避免缓存或 Python ABI 差异掩盖问题。
    BUILD_VENV="$CANDIDATE_DIR/.validation-env"
    "$RUNTIME_VENV/bin/python" -m venv "$BUILD_VENV"
    mkdir -p "$CANDIDATE_DIR/.wheelhouse"
    "$BUILD_VENV/bin/python" -m pip wheel --wheel-dir "$CANDIDATE_DIR/.wheelhouse" -r "$CANDIDATE_DIR/requirements.txt"
    "$BUILD_VENV/bin/python" -m pip install --no-index --find-links "$CANDIDATE_DIR/.wheelhouse" -r "$CANDIDATE_DIR/requirements.txt"
    "$BUILD_VENV/bin/python" -m pip check
    "$BUILD_VENV/bin/python" -m pip freeze --exclude pip --exclude setuptools > "$CANDIDATE_DIR/resolved-requirements.txt"
    (cd "$CANDIDATE_DIR" && sha256sum .wheelhouse/*.whl > wheelhouse.sha256)
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
        "$BUILD_VENV/bin/python" - <<'PY'
import importlib
import main
from fastapi import FastAPI
probe = FastAPI()
for name in ("tasks", "authors", "system", "x_tasks", "works", "platforms", "platform_downloads", "operations"):
    probe.include_router(importlib.import_module("app.api." + name).router, prefix="/api")
assert main.app is not None and probe.openapi().get("paths")
print("FastAPI deferred route imports and OpenAPI OK")
PY
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
        # 预检保持只读。数据库迁移在所有旧入口停止、队列排空和备份验证之后执行。
        command -v pg_dump >/dev/null
        command -v pg_restore >/dev/null
        command -v psql >/dev/null
        "$BUILD_VENV/bin/python" -m app.core.deployment assert-recovery --root "$SERVICE_ROOT"
        test ! -f "$SERVICE_ROOT/.runtime/queue-pending.json"
        test ! -f "$SERVICE_ROOT/.runtime/config-change.json"
    )
}

is_service_command() {
    local executable="${1##*/}"
    shift
    local argument=""
    local server=0 app=0
    # A shell/pgrep/diagnostic command may mention the server, but is not it.
    case "$executable" in python*|gunicorn|uvicorn) ;; *) return 1 ;; esac
    for argument in "$@"; do
        case "$argument" in
            gunicorn|uvicorn|*/gunicorn|*/uvicorn) server=1 ;;
            main:app) app=1 ;;
        esac
    done
    if [ "$server" -eq 1 ] && [ "$app" -eq 1 ]; then return 0; fi
    # Gunicorn can replace argv with its process title when setproctitle is installed.
    case "${1:-}" in 'gunicorn: master [main:app]'|'gunicorn: worker [main:app]') return 0 ;; esac
    # Accept a Python script entrypoint, never arbitrary `python -c` source text.
    if [[ "$executable" == python* ]]; then
        shift
        for argument in "$@"; do
            case "$argument" in
                -c|-m) return 1 ;;
                -*) continue ;;
                *) [ "${argument##*/}" = main.py ]; return $? ;;
            esac
        done
    fi
    return 1
}

matches_service_process() {
    local pid="$1" executable="" process_cwd="" legacy_cwd=""
    local -a arguments=()
    [ -r "/proc/${pid}/cmdline" ] || return 1
    executable="$(readlink -f "/proc/${pid}/exe" 2>/dev/null || true)"
    process_cwd="$(readlink -f "/proc/${pid}/cwd" 2>/dev/null || true)"
    mapfile -d '' -t arguments < "/proc/${pid}/cmdline" 2>/dev/null || return 1
    [ "${#arguments[@]}" -gt 0 ] || return 1
    is_service_command "$executable" "${arguments[@]}" || return 1
    [ "$process_cwd" = "$SERVICE_ROOT" ] && return 0
    # Only the exact previous .current checkout is a migration target.
    legacy_cwd="$(readlink -f "$SERVICE_ROOT/.current" 2>/dev/null || true)"
    [ -n "$legacy_cwd" ] && [ "$process_cwd" = "$legacy_cwd" ]
}

assert_no_running_instances() {
    local pid=""
    while IFS= read -r pid; do
        [ -n "$pid" ] || continue
        if matches_service_process "$pid"; then
            echo "Database operation blocked: an application process is running (PID=${pid}); maintenance and backups retained." >&2
            return 1
        fi
    done < <(pgrep -f "gunicorn.*main:app|uvicorn.*main:app|main.py" 2>/dev/null || true)
}

stop_running_instances() {
    local pid=""
    local parent=""
    local found=0
    local -A candidates=()
    while IFS= read -r pid; do
        [ -n "$pid" ] || continue
        if matches_service_process "$pid"; then candidates["$pid"]=1; fi
    done < <(pgrep -f "gunicorn.*main:app|uvicorn.*main:app|main.py" 2>/dev/null || true)
    # Signal the master once. Sending TERM separately to workers races Gunicorn's
    # own socket/worker shutdown and may trigger unnecessary replacement workers.
    for pid in "${!candidates[@]}"; do
        parent="$(awk '/^PPid:/ {print $2}' "/proc/${pid}/status" 2>/dev/null || true)"
        if [ -n "$parent" ] && [ -n "${candidates[$parent]:-}" ]; then continue; fi
        if matches_service_process "$pid"; then
            echo "Stopping existing application process (PID=${pid})..."
            kill -TERM "$pid" 2>/dev/null || true
            found=1
        fi
    done

    if [ "$found" -eq 0 ]; then
        rm -f "$SERVICE_ROOT/logs/gunicorn.pid"
        echo "No running application process found; BT Panel watchdog may already be restarting it."
        return 0
    fi
    # Gunicorn's default graceful shutdown deadline is 30 seconds.
    for _ in $(seq 1 70); do
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
    echo "Deploy failed: a verified application process is still running (PID=${pid}); check Gunicorn shutdown or an external watchdog." >&2
    return 1
}

deployment_action() {
    (cd "$CANDIDATE_DIR" && "$BUILD_VENV/bin/python" -m app.core.deployment "$@" --root "$SERVICE_ROOT")
}

panel_action() {
    "$PANEL_PYTHON" "$CANDIDATE_DIR/integrations/bt_deployment.py" "$@" \
        --root "$SERVICE_ROOT" --runtime "$RUNTIME_VENV" --state "$PANEL_STATE" --target "$TARGET_SHA" 200>&-
}

assert_release_exclusion() {
    panel_action assert-held
    assert_no_running_instances
}

restore_panel_owner() {
    local attempt
    for attempt in 1 2 3; do
        if panel_action release; then
            PANEL_INHIBITED=0
            return 0
        fi
        sleep 1
    done
    echo "Validated runtime retained; BT ownership finalization needs retry from its durable journal." >&2
    return 1
}

record_stage() {
    "$RUNTIME_VENV/bin/python" - "$SERVICE_ROOT" "$1" "${2:-running}" "$TARGET_SHA" "$PREVIOUS_SHA" <<'PY'
import datetime, json, os, pathlib, sys, tempfile
root, stage, state, target, previous = sys.argv[1:]
directory = pathlib.Path(root) / ".runtime"
descriptor, temporary = tempfile.mkstemp(prefix=".deployment-status-", dir=directory)
try:
    with os.fdopen(descriptor, "w") as output:
        json.dump({"stage": stage, "state": state, "target": target, "previous": previous,
                   "checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}, output)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, directory / "deployment-status.json")
finally:
    if os.path.exists(temporary): os.unlink(temporary)
PY
}

old_runtime_state() {
    # 失败发生在代码切换前时，仍由旧 Web 使用自己的解释器和进程协议恢复。
    # 候选管理器不能为旧代码启动 Worker/Beat，更不能替换其在途 Worker。
    (
        cd "$SERVICE_ROOT"
        APP_PORT="$PORT" "$RUNTIME_VENV/bin/python" - "$1" "$OLD_RUNTIME_STATE" <<'PY'
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request

import psutil
from app.core.config import settings

action, filename = sys.argv[1:]
path = Path(filename)
port = int(os.environ["APP_PORT"])
if not 1 <= port <= 65535:
    raise SystemExit("Invalid runtime port")
base = f"http://127.0.0.1:{port}"
token = settings.ADMIN_TOKEN
headers = {"Authorization": f"Bearer {token}"} if token else {}

def request(endpoint, *, write=False, payload=None):
    req = urllib.request.Request(base + endpoint, headers={**headers, "Content-Type": "application/json"},
                                 data=json.dumps(payload or {}).encode() if write else None,
                                 method="POST" if write else "GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        if not write and endpoint == "/api/ready" and exc.code == 503:
            return exc.code, json.loads(exc.read())
        raise RuntimeError(f"Old runtime API failed: {endpoint}, HTTP {exc.code}") from None
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Old runtime API failed: {endpoint}, {type(exc).__name__}") from None

def process_identity(item):
    if not item.get("pid"):
        return None
    try:
        return psutil.Process(int(item["pid"])).create_time()
    except psutil.NoSuchProcess:
        return None

if action == "capture":
    _, status = request("/api/process/status")
    _, readiness = request("/api/ready")
    state = {"port": port, "worker_concurrency": int(settings.MAX_CONCURRENT_DOWNLOADS),
             "components": {name: bool(value.get("ok"))
             for name, value in readiness.get("components", {}).items()}, "processes": {}}
    for name in ("worker", "beat"):
        item = status[name]
        state["processes"][name] = {"running": bool(item.get("running")),
            "desired": bool(item.get("desired", item.get("running"))), "pid": item.get("pid"),
            "created_at": process_identity(item)}
    candidate = path.with_suffix(".tmp")
    with open(candidate, "w", encoding="utf-8") as handle:
        os.chmod(candidate, 0o600)
        json.dump(state, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(candidate, path)
    print("Previous runtime process intent and readiness recorded")
elif action == "restore":
    state = json.loads(path.read_text(encoding="utf-8"))
    if state["port"] != port:
        raise SystemExit("Old runtime recovery port mismatch")
    targets = {name: bool(item["running"] or item["desired"]) for name, item in state["processes"].items()}
    for name in ("worker", "beat"):
        _, current = request("/api/process/status")
        if not targets[name] or current[name].get("running"):
            continue
        original = state["processes"][name]
        if original.get("running") and original.get("created_at") is not None:
            try:
                process = psutil.Process(int(original["pid"]))
                if (abs(process.create_time() - original["created_at"]) <= 0.01
                        and process.is_running() and process.status() != psutil.STATUS_ZOMBIE):
                    raise RuntimeError(f"Original {name} is alive but old Web cannot confirm ownership; no replacement attempted")
            except psutil.NoSuchProcess:
                pass
        try:
            payload = {"concurrency": int(state["worker_concurrency"])} if name == "worker" else {}
            request(f"/api/process/{name}/start", write=True, payload=payload)
        except RuntimeError:
            # 启动响应丢失时只读确认，不重复写入。
            pass
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        _, current = request("/api/process/status")
        code, readiness = request("/api/ready")
        components = readiness.get("components", {})
        processes_ok = all(not desired or current[name].get("running") for name, desired in targets.items())
        baseline_ok = all(not was_ready or components.get(name, {}).get("ok") is True
                          for name, was_ready in state["components"].items())
        targets_ready = all(not desired or components.get(name, {}).get("ok") is True
                            for name, desired in targets.items())
        if processes_ok and baseline_ok and targets_ready:
            print("Previous runtime restored and readiness confirmed through its own Web process")
            break
        time.sleep(1)
    else:
        raise SystemExit("Old runtime recovery did not confirm readiness; deployment artifacts retained")
else:
    raise SystemExit("Unknown runtime state action")
PY
    )
}

stop_optional_supervisor() {
    local file="$SERVICE_ROOT/logs/optional-services.pid" pid="" command=""
    [ -f "$file" ] || return 0
    pid="$(tr -dc '0-9' < "$file")"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        command="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
        if [[ "$command" != *"app.core.optional_services"* ]]; then
            echo "Optional service supervisor PID identity mismatch." >&2
            return 1
        fi
        kill -TERM "$pid"
        for _ in $(seq 1 50); do
            kill -0 "$pid" 2>/dev/null || break
            sleep 0.5
        done
        if kill -0 "$pid" 2>/dev/null; then return 1; fi
    fi
    rm -f "$file"
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
    echo "Starting application through its verified BT Panel project owner..."
    panel_action assert-held
    # 只启动项目辅助协调器；Web 始终由宝塔保存的同一个项目入口启动。
    if [ -f "$SERVICE_ROOT/app/core/optional_services.py" ]; then
        APP_PORT="$PORT" VENV_DIR="$RUNTIME_VENV" RUNTIME_DIR="$SERVICE_ROOT" \
            bash "$SERVICE_ROOT/start.sh" --support-only 200>&-
    elif (cd "$SERVICE_ROOT" && "$RUNTIME_VENV/bin/python" -c \
        'from app.core.config import settings; raise SystemExit(not settings.XHS_SERVICE_ENABLED)'); then
        # 旧版没有协调器；使用候选脚本的单次启动入口恢复旧引擎，不启动第二个 Web。
        if ! PROJECT_DIR_OVERRIDE="$SERVICE_ROOT" APP_PORT="$PORT" VENV_DIR="$RUNTIME_VENV" \
            RUNTIME_DIR="$SERVICE_ROOT" bash "$CANDIDATE_DIR/start.sh" --xhs-start-once 200>&-; then
            if "$RUNTIME_VENV/bin/python" - "$OLD_RUNTIME_STATE" <<'PY'
import json, sys
state = json.load(open(sys.argv[1], encoding="utf-8"))
raise SystemExit(not state.get("components", {}).get("xhs_collector", False))
PY
            then
                echo "Previously ready Xiaohongshu service did not recover." >&2
                return 1
            fi
            echo "Xiaohongshu service remains unavailable as before deployment." >&2
        fi
    fi
    panel_action start
}

smoke_check() {
    local allow_legacy_health="${1:-0}"
    local attempts="${2:-150}"
    (
        trap - ERR
        cd "$SERVICE_ROOT"
        APP_PORT="$PORT" ALLOW_LEGACY_HEALTH="$allow_legacy_health" SMOKE_ATTEMPTS="$attempts" \
            EXPECTED_RUNTIME_SHA="$(if [ "$allow_legacy_health" -eq 1 ]; then printf '%s' "$PREVIOUS_SHA"; else printf '%s' "$TARGET_SHA"; fi)" \
            "$RUNTIME_VENV/bin/python" - <<'PY'
import importlib.metadata, json, os, pathlib, sys, time, urllib.error, urllib.request
import psutil
from app.core.config import settings

base = f"http://127.0.0.1:{os.environ['APP_PORT']}"
allow_legacy_health = os.environ.get("ALLOW_LEGACY_HEALTH") == "1"
token = settings.ADMIN_TOKEN
headers = {"Authorization": f"Bearer {token}"} if token else {}
xhs_api_url = "http://127.0.0.1:5556" if getattr(settings, "XHS_SERVICE_ENABLED", False) else None
expected_revision = os.environ["EXPECTED_RUNTIME_SHA"]
expected_dependencies = {name: importlib.metadata.version(name)
                         for name in ("fastapi", "sqlalchemy", "celery", "gunicorn", "uvicorn")}

def verify_runtime(readiness):
    runtime = readiness.get("runtime") or {}
    if allow_legacy_health and not runtime:
        return
    if runtime.get("revision") != expected_revision or not runtime.get("instance"):
        raise RuntimeError("responding Web did not load the expected code revision")
    if any(runtime.get("dependencies", {}).get(name) != version
           for name, version in expected_dependencies.items()):
        raise RuntimeError("responding Web loaded a different dependency generation")
    process = psutil.Process(int(runtime["pid"]))
    if (pathlib.Path(process.cwd()).resolve() != pathlib.Path.cwd().resolve()
            or pathlib.Path(process.exe()).resolve() != pathlib.Path(sys.executable).resolve()
            or abs(process.create_time() - float(runtime["created_at"])) > 0.01):
        raise RuntimeError("responding Web process identity does not match the project runtime")
    masters = []
    for candidate in psutil.process_iter(["pid", "ppid", "cwd", "cmdline"]):
        try:
            command = " ".join(candidate.info["cmdline"] or [])
            if (candidate.info["cwd"] == str(pathlib.Path.cwd()) and "gunicorn" in command
                    and "main:app" in command):
                masters.append(candidate)
        except psutil.Error:
            continue
    ids = {item.pid for item in masters}
    if sum(item.ppid() not in ids for item in masters) != 1 or process.pid not in ids:
        raise RuntimeError("Web ownership is not unique for the deployed project")

def get(path, auth=False, *, range_probe=False):
    request_headers = dict(headers if auth else {})
    if range_probe:
        request_headers["Range"] = "bytes=0-1023"
    request = urllib.request.Request(base + path, headers=request_headers)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            if response.status != (206 if range_probe else 200):
                raise RuntimeError(f"{path} returned HTTP {response.status}")
            if range_probe and not response.headers.get("Content-Range", "").startswith("bytes 0-"):
                raise RuntimeError(f"{path} did not return a valid byte range")
            if range_probe:
                return response.read(1024)
            return response.read()
    except urllib.error.HTTPError as exc:
        # Preserve readiness's status/body handling while identifying the failing API.
        raise urllib.error.HTTPError(
            exc.url, exc.code, f"{path} returned HTTP {exc.code}", exc.headers, exc.fp,
        ) from exc

last_error = None
for _ in range(int(os.environ.get("SMOKE_ATTEMPTS", "150"))):
    try:
        verified_platforms = []
        pending_platforms = []
        try:
            readiness = json.loads(get("/api/ready"))
            if readiness.get("status") != "ready":
                raise RuntimeError("dependency readiness payload is not ready")
            verify_runtime(readiness)
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
                    if readiness.get("components", {}).get("configuration", {}).get("ok") is not True:
                        try:
                            bootstrap = json.loads(get("/api/bootstrap/status", True))
                            # Only diagnostic codes: configuration values may contain secrets.
                            keys = [str(item.get("key", "unknown")) for item in bootstrap.get("errors", [])]
                            failures.append("startup error codes: " + ", ".join(keys or ["unavailable"]))
                            for item in bootstrap.get("errors", []):
                                stage, kind = item.get("stage", ""), item.get("error_type", "")
                                if isinstance(stage, str) and isinstance(kind, str) and stage.isidentifier() and kind.isidentifier():
                                    failures.append(f"startup stage={stage} type={kind}")
                        except Exception:
                            failures.append("startup diagnostics unavailable; inspect application startup log")
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
            try:
                with urllib.request.urlopen(xhs_api_url.rstrip("/") + "/health", timeout=5) as response:
                    if response.status != 200:
                        print("Optional Xiaohongshu service is not ready")
            except Exception:
                print("Optional Xiaohongshu service unavailable; main service check continues")
        if token:
            authors = json.loads(get("/api/authors/?page=1&page_size=1", True))
            tasks = json.loads(get("/api/tasks/?page=1&page_size=20", True))
            unified = json.loads(get("/api/operations/tasks?page=1&page_size=1", True))
            platforms = json.loads(get("/api/operations/platform-readiness", True))
            storage = json.loads(get("/api/operations/storage-audit", True))
            previewable = next((item for item in tasks.get("items", []) if item.get("local_preview_available")), None)
            if previewable:
                get(f"/api/tasks/{previewable.get('id')}/preview", True, range_probe=True)
            if not isinstance(authors.get("items"), list) or not isinstance(tasks.get("items"), list):
                raise RuntimeError("management list payload is invalid")
            if not isinstance(unified.get("items"), list) or not isinstance(platforms.get("items"), list):
                raise RuntimeError("cross-platform operations payload is invalid")
            if len(platforms["items"]) < 6:
                raise RuntimeError("platform readiness matrix is incomplete")
            if storage.get("status") not in {"idle", "queued", "running", "completed", "failed"}:
                raise RuntimeError("storage audit status payload is invalid")
            if storage.get("result") and storage["result"].get("read_only") is not True:
                raise RuntimeError("storage audit did not preserve read-only semantics")
            for platform in platforms["items"]:
                platform_id = platform["platform"]
                page = json.loads(get(
                    f"/api/operations/tasks?platform={platform_id}&status=completed&page=1&page_size=20",
                    True,
                ))
                item = next((row for row in page.get("items", []) if row.get("preview_count")), None)
                if not item:
                    pending_platforms.append(platform_id)
                    continue
                if item.get("preview_endpoint"):
                    get("/api" + item["preview_endpoint"], True, range_probe=True)
                elif item.get("media_endpoint"):
                    assets = json.loads(get("/api" + item["media_endpoint"], True))
                    if not assets:
                        raise RuntimeError(f"{platform_id} completed task has no media assets")
                    get(assets[0]["preview_url"], True, range_probe=True)
                if item.get("stats_endpoint"):
                    json.loads(get("/api" + item["stats_endpoint"], True))
                verified_platforms.append(platform_id)
        print(
            "Smoke checks OK: BT Panel runtime, home, docs, unified tasks, platform readiness, "
            "storage audit and current-code media preview; "
            f"verified platforms={','.join(verified_platforms) or 'none'}; "
            f"awaiting first successful task={','.join(pending_platforms) or 'none'}"
        )
        raise SystemExit(0)
    except Exception as exc:
        last_error = exc
        time.sleep(1)
raise SystemExit(f"Smoke checks failed: {last_error}")
PY
    )
}

cleanup_candidate() {
    if [ -f "$SERVICE_ROOT/.runtime/deployment-bt-owner.json" ]; then
        echo "Unresolved BT project handoff; candidate retained for recovery." >&2
        return 0
    fi
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

reconcile_published_worktree_changes() {
    local previous_sha="$1"
    local target_sha="$2"
    local matched_sha=""
    local candidate_sha=""
    local backup_dir=""
    local backup_name=""
    local -a dirty_paths=()

    if git diff --quiet && git diff --cached --quiet; then
        return 0
    fi

    echo "Tracked files contain local changes:" >&2
    git status --short --untracked-files=no >&2

    if ! git diff --cached --quiet; then
        echo "Deploy aborted: staged service-root changes require manual review." >&2
        return 1
    fi
    if ! git merge-base --is-ancestor "$previous_sha" "$target_sha"; then
        echo "Deploy aborted: target commit is not a descendant of the deployed commit." >&2
        return 1
    fi

    mapfile -d '' dirty_paths < <(git diff --name-only -z)
    if [ "${#dirty_paths[@]}" -eq 0 ]; then
        echo "Deploy aborted: unable to classify the tracked service-root changes." >&2
        return 1
    fi

    while IFS= read -r candidate_sha; do
        if git diff --quiet "$candidate_sha" -- "${dirty_paths[@]}"; then
            matched_sha="$candidate_sha"
            break
        fi
    done < <(git rev-list --reverse --ancestry-path "${previous_sha}..${target_sha}")

    if [ -z "$matched_sha" ]; then
        echo "Deploy aborted: tracked changes do not match a published commit on the deployment path." >&2
        return 1
    fi

    backup_dir="$SERVICE_ROOT/.runtime/deploy-recovery"
    backup_name="$(date -u +%Y%m%dT%H%M%SZ)-${previous_sha:0:12}-to-${matched_sha:0:12}"
    mkdir -p "$backup_dir"
    git status --short --untracked-files=no > "$backup_dir/${backup_name}.status"
    git diff --binary "$previous_sha" -- "${dirty_paths[@]}" > "$backup_dir/${backup_name}.patch"

    echo "Local changes exactly match published commit ${matched_sha}; preserving an audit patch and cleaning the old worktree."
    git restore --source="$previous_sha" --worktree -- "${dirty_paths[@]}"
    if ! git diff --quiet || ! git diff --cached --quiet; then
        echo "Deploy aborted: the service root is still dirty after published-change reconciliation." >&2
        return 1
    fi
}

rollback() {
    local exit_code="${1:-$?}"
    trap - ERR
    # 收到第二个终止信号时仍完成恢复或留下明确的失败状态，不递归回滚。
    trap '' TERM HUP INT
    # 验收通过后的所有权释放可能已部分提交；不能再把已放行的库恢复成旧快照。
    if [ "$FINALIZATION_STARTED" -eq 1 ]; then
        record_stage finalization-pending failed || true
        echo "Runtime verification passed; release finalization is incomplete. Recovery materials retained." >&2
        exit "$exit_code"
    fi
    if [ "$ROLLING_BACK" -eq 1 ] || [ "$MAINTENANCE_STARTED" -eq 0 ] || [ -z "$PREVIOUS_SHA" ]; then
        cleanup_candidate
        exit "$exit_code"
    fi
    if [ "$CODE_SWITCHED" -eq 0 ] && [ "$DATABASE_MIGRATED" -eq 0 ]; then
        record_stage early-recovery
        if [ "$PANEL_INHIBITED" -eq 1 ]; then
            panel_action inhibit || exit "$exit_code"
            panel_action restore-config || exit "$exit_code"
            panel_action start || exit "$exit_code"
        fi
        rm -f "$SERVICE_ROOT/.runtime/deployment-maintenance" "$SERVICE_ROOT/.runtime/deployment-verifying"
        if ! old_runtime_state restore; then
            echo "Early rollback recovery failed; old code, candidate and backups retained for recovery." >&2
            printf '%s\n' 'Old runtime recovery failed before code switch; inspect old Web readiness.' \
                > "$DEPENDENCY_BACKUP/recovery-failed.txt"
            exit "$exit_code"
        fi
        if [ "$PANEL_CAPTURED" -eq 1 ]; then restore_panel_owner || exit "$exit_code"; fi
        record_stage early-recovered failed_recovered
        cleanup_candidate
        exit "$exit_code"
    fi
    ROLLING_BACK=1
    record_stage rollback
    echo "Deploy failed; restoring root worktree to ${PREVIOUS_SHA}..." >&2
    # 验证期间允许的自动接管必须先关闭，避免停止后被 Web 生命周期重新拉起。
    rm -f "$SERVICE_ROOT/.runtime/deployment-verifying"
    if ! panel_action assert-held || ! stop_running_instances || ! stop_optional_supervisor || ! stop_managed_xhs_engine || \
       ! deployment_action shutdown || ! assert_release_exclusion || ! deployment_action assert-empty; then
        echo "Rollback blocked: project processes did not stop; maintenance and backups retained." >&2
        exit "$exit_code"
    fi
    if [ "$DATABASE_MIGRATED" -eq 1 ]; then
        # 在恢复前再次检查，不能把外部看门狗的重新启动当成成功停止。
        assert_release_exclusion || exit "$exit_code"
        record_stage restore-database
        deployment_action restore --path "$DATABASE_BACKUP" || {
            echo "Database recovery failed; maintenance and backups retained." >&2
            exit "$exit_code"
        }
    fi
    restore_xhs_engine
    assert_release_exclusion || exit "$exit_code"
    git reset --hard "$PREVIOUS_SHA"
    if [ "$DEPENDENCIES_CHANGED" -eq 1 ]; then
        assert_release_exclusion || exit "$exit_code"
        record_stage restore-dependencies
        "$RUNTIME_VENV/bin/python" - "$DEPENDENCY_BACKUP/packages.txt" <<'PY'
import importlib.metadata
import re
import subprocess
import sys
from pathlib import Path
normalize = lambda name: re.sub(r"[-_.]+", "-", name).lower()
baseline = {normalize(line.split("==", 1)[0].split(" @ ", 1)[0])
            for line in Path(sys.argv[1]).read_text().splitlines() if line and not line.startswith("#")}
extra = [item.metadata["Name"] for item in importlib.metadata.distributions()
         if normalize(item.metadata["Name"]) not in baseline]
if extra:
    subprocess.run([sys.executable, "-m", "pip", "uninstall", "-y", *extra], check=True)
PY
        "$RUNTIME_VENV/bin/python" -m pip install --no-index --find-links "$DEPENDENCY_BACKUP/wheels" -r "$DEPENDENCY_BACKUP/packages.txt"
        "$RUNTIME_VENV/bin/python" -m pip check
    fi
    assert_release_exclusion || exit "$exit_code"
    panel_action restore-config || exit "$exit_code"
    rm -f "$SERVICE_ROOT/.runtime/deployment-maintenance" "$SERVICE_ROOT/.runtime/deployment-verifying"
    start_runtime
    smoke_check 1 30
    restore_panel_owner || exit "$exit_code"
    record_stage recovered failed_recovered
    cleanup_candidate
    echo "Code, dependencies and the pre-migration database snapshot have been recovered." >&2
    exit "$exit_code"
}

trap rollback ERR
trap 'rollback 143' TERM
trap 'rollback 129' HUP
trap 'rollback 130' INT
validate_layout
mkdir -p "$SERVICE_ROOT/logs"
mkdir -p "$SERVICE_ROOT/.runtime"
command -v flock >/dev/null
exec 200>"$SERVICE_ROOT/.runtime/deploy.lock"
flock -n 200 || { echo "Another deployment is running." >&2; exit 1; }

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
reconcile_published_worktree_changes "$PREVIOUS_SHA" "$TARGET_SHA"

record_stage prepare-candidate
prepare_candidate
record_stage preflight
preflight
if ! prepare_xhs_engine; then
    XHS_ENGINE_CANDIDATE=""
    if (cd "$CANDIDATE_DIR" && "$BUILD_VENV/bin/python" -c 'from app.core.config import settings; raise SystemExit(not settings.XHS_SERVICE_ENABLED)'); then
        echo "Enabled Xiaohongshu engine could not be prepared." >&2
        exit 1
    fi
    echo "Optional Xiaohongshu preinstallation unavailable; main release remains usable." >&2
fi

if [ "$PREVIOUS_SHA" != "$TARGET_SHA" ] || [ -L "$SERVICE_ROOT/.current" ]; then
    DEPENDENCY_BACKUP="$SERVICE_ROOT/.runtime/deploy-recovery/$(date -u +%Y%m%dT%H%M%SZ)-dependencies"
    mkdir -p "$DEPENDENCY_BACKUP/wheels"
    chmod 700 "$DEPENDENCY_BACKUP"
    "$RUNTIME_VENV/bin/python" -m pip freeze --all > "$DEPENDENCY_BACKUP/packages.txt"
    "$RUNTIME_VENV/bin/python" -m pip download --no-deps -r "$DEPENDENCY_BACKUP/packages.txt" -d "$DEPENDENCY_BACKUP/wheels"
    if [ -f "$SERVICE_ROOT/.env" ]; then
        cp -p "$SERVICE_ROOT/.env" "$DEPENDENCY_BACKUP/runtime.env"
        chmod 600 "$DEPENDENCY_BACKUP/runtime.env"
    fi
    OLD_RUNTIME_STATE="$DEPENDENCY_BACKUP/runtime-state.json"
    old_runtime_state capture
    PANEL_STATE="$DEPENDENCY_BACKUP/bt-owner.json"
    panel_action capture
    PANEL_CAPTURED=1
    touch "$SERVICE_ROOT/.runtime/deployment-maintenance"
    MAINTENANCE_STARTED=1
    PANEL_INHIBITED=1
    panel_action inhibit
    record_stage drain
    deployment_action drain --timeout 7500
    # 预检完成后先停止所有旧入口，确保 Jenkins 环境和宝塔环境不会同时运行应用。
    CODE_SWITCHED=1
    stop_running_instances
    stop_optional_supervisor
    deployment_action shutdown
    stop_managed_xhs_engine
    assert_release_exclusion
    deployment_action assert-empty
    record_stage backup
    DATABASE_BACKUP="$DEPENDENCY_BACKUP/database.dump"
    deployment_action backup --path "$DATABASE_BACKUP"
    DATABASE_MIGRATED=1
    assert_release_exclusion
    record_stage migrate
    (cd "$CANDIDATE_DIR" && "$BUILD_VENV/bin/python" -c 'from app.models.database import init_db_sync; init_db_sync(); print("Database migrations OK")')
    RESTART_REQUIRED=1
    assert_release_exclusion
    record_stage install-dependencies
    prepare_runtime_environment
    assert_release_exclusion
    activate_xhs_engine
    record_stage switch-code
    git reset --hard "$TARGET_SHA"
    assert_release_exclusion
    panel_action prepare-start
    touch "$SERVICE_ROOT/.runtime/deployment-verifying"
fi

if [ "$RESTART_REQUIRED" -eq 1 ]; then
    record_stage start
    start_runtime
fi
record_stage verify
smoke_check 0 30
FINALIZATION_STARTED=1
if [ "$PANEL_CAPTURED" -eq 1 ]; then restore_panel_owner; fi
rm -f "$SERVICE_ROOT/.runtime/deployment-maintenance" "$SERVICE_ROOT/.runtime/deployment-verifying"

CODE_SWITCHED=0
record_stage complete success
trap - ERR TERM HUP INT
cleanup_legacy_release_layout
cleanup_candidate
echo "Deploy success. ${PROJECT_NAME} root worktree is at ${TARGET_SHA}; Jenkins restarted it with the BT Panel environment."
