"""Per-project BT ownership during a release; run with the panel interpreter.

No application configuration is read or changed here. The journal contains panel
launch metadata and must remain in the ignored, protected runtime directory.
"""
from __future__ import annotations

import argparse
import ast
import base64
import contextlib
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import tempfile
import tokenize


PANEL_ROOT = Path("/www/server/panel")
OLD_WORKER = "uvicorn.workers.UvicornWorker"
NEW_WORKER = "uvicorn_worker.UvicornWorker"


class OwnershipError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise OwnershipError(message)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def inside(path: Path, directory: Path) -> bool:
    try:
        path.resolve().relative_to(directory.resolve())
        return True
    except ValueError:
        return False


def atomic_write(path: Path, data: bytes, *, mode: int = 0o600,
                 owner: tuple[int, int] | None = None) -> None:
    require(not path.is_symlink(), "Refusing a symbolic runtime file")
    metadata = path.stat() if path.exists() else None
    descriptor, temporary = tempfile.mkstemp(prefix=".release-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), stat.S_IMODE(metadata.st_mode) if metadata else mode)
            if metadata or owner:
                os.fchown(stream.fileno(), *(owner or (metadata.st_uid, metadata.st_gid)))
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class BTOwner:
    def __init__(self, root: Path, runtime: Path, state: Path, target: str):
        self.root = root.resolve(strict=True)
        self.runtime = runtime.resolve(strict=True)
        if self.runtime.name == "bin":
            self.runtime = self.runtime.parent
        self.state_path = state.absolute()
        self.active_path = self.root / ".runtime/deployment-bt-owner.json"
        require(inside(self.state_path, self.root / ".runtime"),
                "Ownership journal must be inside the project runtime directory")
        require(re.fullmatch(r"[0-9a-f]{40}", target) is not None, "Invalid release revision")
        self.target = target
        self.database = PANEL_ROOT / "data/db/site.db"
        self.stopped_path = PANEL_ROOT / "data/push/tips/project_stop.json"
        self.conf_path = self.root / "gunicorn_conf.py"
        self.state: dict = {}

    @contextlib.contextmanager
    def connect(self, *, write: bool = False):
        require(self.database.is_file() and not self.database.is_symlink(), "Unsupported panel database")
        connection = sqlite3.connect(f"file:{self.database}?mode={'rw' if write else 'ro'}", uri=True, timeout=15)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def site(self, connection: sqlite3.Connection) -> tuple[dict, dict]:
        matches = []
        for row in connection.execute("SELECT id,name,project_config FROM sites WHERE project_type='Python'"):
            conf = json.loads(row["project_config"])
            if Path(conf.get("path", "/nonexistent")).resolve() == self.root:
                matches.append((dict(row), conf))
        require(len(matches) == 1, "Expected exactly one matching BT Python project")
        row, conf = matches[0]
        require(conf.get("stype") == "gunicorn", "Unsupported BT project launcher")
        require(conf.get("pjname") == row["name"], "Panel project identity does not match")
        require(not conf.get("services"), "Additional panel services require an explicit release adapter")
        path = Path(conf.get("vpath", "/nonexistent")).resolve()
        require(path in {self.runtime, self.runtime / "bin"}, "Panel runtime does not match deployment runtime")
        require(Path(conf.get("rfile", "/nonexistent")).resolve() == self.root / "main.py",
                "Unsupported panel application entry point")
        if self.state:
            require(row["id"] == self.state["site_id"] and row["name"] == self.state["site_name"],
                    "Panel project identity changed during deployment")
        return row, conf

    def stopped(self) -> dict:
        if not self.stopped_path.exists():
            return {}
        require(not self.stopped_path.is_symlink(), "Unsupported panel stop-intent file")
        payload = json.loads(self.stopped_path.read_text(encoding="utf-8"))
        require(isinstance(payload, dict), "Invalid panel stop intent")
        return payload

    def set_stopped(self, value: bool, *, present: bool = True) -> None:
        # Merge only this project's entry; other panel projects keep their state.
        self.stopped_path.parent.mkdir(parents=True, exist_ok=True)
        created = False
        try:
            descriptor = os.open(self.stopped_path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            created = True
        except FileExistsError:
            descriptor = os.open(self.stopped_path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            held = os.fstat(descriptor)
            current = self.stopped_path.stat()
            if created and held.st_size == 0 and (held.st_dev, held.st_ino) == (current.st_dev, current.st_ino):
                payload = {}
            else:
                payload = self.stopped()
            key = str(self.state["site_id"])
            if present:
                payload[key] = value
            else:
                payload.pop(key, None)
            atomic_write(self.stopped_path, json.dumps(payload).encode())
        finally:
            os.close(descriptor)

    def set_auto_run(self, value, *, present: bool = True) -> None:
        with self.connect(write=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row, conf = self.site(connection)
            if present:
                conf["auto_run"] = value
            else:
                conf.pop("auto_run", None)
            connection.execute("UPDATE sites SET project_config=? WHERE id=? AND project_type='Python'",
                               (json.dumps(conf), row["id"]))

    def save(self, phase: str | None = None) -> None:
        if phase:
            self.state["phase"] = phase
        atomic_write(self.state_path, json.dumps(self.state).encode())

    def load(self) -> None:
        require(self.state_path.is_file() and not self.state_path.is_symlink(), "Ownership journal is missing")
        self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
        require(self.state.get("root") == str(self.root) and self.state.get("runtime") == str(self.runtime),
                "Ownership journal belongs to another runtime")
        require(self.state.get("target") == self.target, "Ownership journal belongs to another release")
        if self.state.get("phase") == "released" and not self.active_path.exists():
            pass
        else:
            require(self.active_path.is_file() and not self.active_path.is_symlink(), "Active ownership proof is missing")
            active = json.loads(self.active_path.read_text(encoding="utf-8"))
            require(active.get("state") == str(self.state_path) and active.get("target") == self.target,
                    "Another release owns the BT project")
        with self.connect() as connection:
            self.site(connection)

    def worker_assignment(self, content: bytes) -> ast.Constant:
        tree = ast.parse(content)
        assignments = [node.value for node in tree.body if isinstance(node, ast.Assign)
                       and any(isinstance(target, ast.Name) and target.id == "worker_class" for target in node.targets)]
        require(len(assignments) == 1 and isinstance(assignments[0], (ast.Constant, ast.Str))
                and ast.literal_eval(assignments[0]) in {OLD_WORKER, NEW_WORKER}, "Unsupported Gunicorn worker configuration")
        return assignments[0]

    def validate_contract(self, name: str) -> None:
        source = (PANEL_ROOT / "class/projectModel/pythonModel.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        classes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "main"]
        require(len(classes) == 1, "Unsupported panel Python manager")
        methods = {node.name for node in classes[0].body if isinstance(node, ast.FunctionDef)}
        require({"only_start_main_project", "get_project_run_state"} <= methods, "Unsupported panel start API")
        require("auto_run" in (PANEL_ROOT / "script/project_daemon.py").read_text(encoding="utf-8"),
                "Unsupported panel daemon")
        script = PANEL_ROOT / f"../python_project/vhost/scripts/{name}_gunicorn.sh"
        require(script.is_file(), "Panel Gunicorn launcher is missing")
        command = script.read_text(encoding="utf-8")
        require(str(self.root / "gunicorn_conf.py") in command and "main:app" in command,
                "Panel launcher does not load the project Gunicorn configuration")
        require((PANEL_ROOT / "pyenv/bin/python").is_file(), "Panel interpreter is missing")

    def capture(self) -> None:
        if self.active_path.exists():
            active = json.loads(self.active_path.read_text(encoding="utf-8"))
            previous_path = Path(active.get("state", ""))
            require(inside(previous_path, self.root / ".runtime"), "Invalid prior ownership journal")
            require(previous_path.is_file(), "Prior ownership journal must be recovered first")
            previous = json.loads(previous_path.read_text(encoding="utf-8"))
            if previous.get("phase") != "released":
                require(previous_path == self.state_path and active.get("target") == self.target,
                        "An unfinished BT ownership handoff requires recovery")
                self.load()
                return
        with self.connect() as connection:
            row, conf = self.site(connection)
        self.validate_contract(row["name"])
        require(self.conf_path.is_file() and not self.conf_path.is_symlink(), "Gunicorn configuration is missing")
        original = self.conf_path.read_bytes()
        self.worker_assignment(original)
        stopped = self.stopped()
        key = str(row["id"])
        self.state = {
            "format": 1, "root": str(self.root), "runtime": str(self.runtime), "target": self.target,
            "site_id": row["id"], "site_name": row["name"],
            "auto_run_present": "auto_run" in conf, "auto_run": conf.get("auto_run"),
            "stopped_present": key in stopped, "stopped": stopped.get(key, False),
            "gunicorn_original": base64.b64encode(original).decode("ascii"),
            "gunicorn_original_hash": digest(original), "gunicorn_prepared_hash": None,
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.save("captured")
        atomic_write(self.active_path, json.dumps({"state": str(self.state_path), "target": self.target}).encode())

    def inhibit(self) -> None:
        self.load()
        # Journal intent before either mutation. Recovery may safely repeat it.
        self.save("inhibiting")
        self.set_stopped(True)
        self.set_auto_run(False)
        self.assert_held()
        self.save("held")

    def assert_held(self) -> None:
        self.load()
        require(self.state["phase"] != "released", "BT ownership has already been released")
        with self.connect() as connection:
            _, conf = self.site(connection)
        require(conf.get("auto_run") is False and self.stopped().get(str(self.state["site_id"])) is True,
                "BT automatic restart inhibition was lost")

    def prepare_start(self) -> None:
        self.assert_held()
        content = self.conf_path.read_bytes()
        node = self.worker_assignment(content)
        require(digest(content) in {self.state["gunicorn_original_hash"], self.state.get("gunicorn_prepared_hash")},
                "Gunicorn configuration changed during deployment")
        if ast.literal_eval(node) == OLD_WORKER:
            lines = content.splitlines(keepends=True)
            tokens = [token for token in tokenize.tokenize(io.BytesIO(content).readline)
                      if token.type == tokenize.STRING and token.start[0] == node.lineno
                      and len(lines[token.start[0] - 1].decode("utf-8")[:token.start[1]].encode("utf-8")) == node.col_offset]
            require(len(tokens) == 1, "Unsupported Gunicorn worker literal")
            token = tokens[0]
            start = sum(map(len, lines[:token.start[0] - 1])) + len(lines[token.start[0] - 1].decode("utf-8")[:token.start[1]].encode("utf-8"))
            end = sum(map(len, lines[:token.end[0] - 1])) + len(lines[token.end[0] - 1].decode("utf-8")[:token.end[1]].encode("utf-8"))
            content = content[:start] + repr(NEW_WORKER).encode() + content[end:]
        # Write the intended hash before the atomic file replacement.
        self.state["gunicorn_prepared_hash"] = digest(content)
        self.save("preparing")
        atomic_write(self.conf_path, content)
        self.save("prepared")

    def restore_config(self) -> None:
        self.assert_held()
        content = self.conf_path.read_bytes()
        require(digest(content) in {self.state["gunicorn_original_hash"], self.state.get("gunicorn_prepared_hash")},
                "Gunicorn configuration changed outside deployment; recovery retained")
        original = base64.b64decode(self.state["gunicorn_original"], validate=True)
        require(digest(original) == self.state["gunicorn_original_hash"], "Gunicorn recovery checksum failed")
        atomic_write(self.conf_path, original)
        self.save("restored")

    def start(self) -> None:
        self.assert_held()
        self.save("starting")
        # Importing the panel manager is deferred until the controlled start.
        # Its normal API clears the user-stop flag, so preserve inhibition on
        # this instance while auto_run remains disabled in the panel database.
        previous_cwd = Path.cwd()
        output = io.StringIO()
        try:
            os.chdir(PANEL_ROOT)
            sys.path[:0] = [str(PANEL_ROOT), str(PANEL_ROOT / "class")]
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                from projectModel.pythonModel import main
                manager = main()
                manager.start_by_user = lambda project_id: None
                running = self.web_masters()
                require(len(running) <= 1, "Multiple project Web masters prevent controlled start")
                if not running:
                    manager.only_start_main_project(self.state["site_name"])
                pids = manager.get_project_run_state(self.state["site_name"])
            require(bool(pids), "Panel project start did not produce a running service")
            require(len(self.web_masters()) == 1, "Panel launch did not produce one verified project Web master")
        finally:
            os.chdir(previous_cwd)
        self.assert_held()
        self.save("started")

    def web_masters(self) -> list[int]:
        candidates: dict[int, int] = {}
        expected_executable = (self.runtime / "bin/python").resolve()
        for directory in Path("/proc").iterdir():
            if not directory.name.isdigit():
                continue
            try:
                if (directory / "cwd").resolve() != self.root:
                    continue
                command = (directory / "cmdline").read_bytes().replace(b"\0", b" ")
                if b"gunicorn" not in command or b"main:app" not in command:
                    continue
                require((directory / "exe").resolve() == expected_executable,
                        "A project Web process uses another interpreter")
                fields = (directory / "stat").read_text().rsplit(")", 1)[1].split()
                if fields[0] != "Z":
                    candidates[int(directory.name)] = int(fields[1])
            except (FileNotFoundError, ProcessLookupError):
                continue
        return [pid for pid, parent in candidates.items() if parent not in candidates]

    def release(self) -> None:
        self.load()
        if self.state["phase"] == "released":
            self.finish_release()
            return
        if self.state["phase"] != "releasing":
            if self.state["phase"] not in {"captured", "inhibiting"}:
                self.assert_held()
            self.save("releasing")
        self.set_stopped(self.state["stopped"], present=self.state["stopped_present"])
        self.set_auto_run(self.state["auto_run"], present=self.state["auto_run_present"])
        with self.connect() as connection:
            _, conf = self.site(connection)
        stopped = self.stopped()
        key = str(self.state["site_id"])
        require(("auto_run" in conf) == self.state["auto_run_present"]
                and conf.get("auto_run") == self.state["auto_run"]
                and (key in stopped) == self.state["stopped_present"]
                and stopped.get(key, False) == self.state["stopped"], "Panel launch intent was not restored")
        self.save("released")
        self.finish_release()

    def finish_release(self) -> None:
        if self.active_path.exists():
            active = json.loads(self.active_path.read_text(encoding="utf-8"))
            require(active.get("state") == str(self.state_path) and active.get("target") == self.target,
                    "Cannot release another deployment's ownership")
            self.active_path.unlink()
            descriptor = os.open(self.active_path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("capture", "inhibit", "assert-held", "prepare-start", "start", "restore-config", "release"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--target", required=True)
    arguments = parser.parse_args()
    try:
        owner = BTOwner(arguments.root, arguments.runtime, arguments.state, arguments.target)
        getattr(owner, arguments.action.replace("-", "_"))()
    except OwnershipError as error:
        print(f"BT ownership failed: {error}", file=sys.stderr)
        return 1
    except Exception as error:
        # Panel exceptions may contain commands, credentials or project metadata.
        print(f"BT ownership failed: {type(error).__name__}", file=sys.stderr)
        return 1
    print(f"BT ownership {arguments.action} OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
