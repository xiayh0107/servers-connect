"""Loopback runtime lifecycle shared by browser and future native shells."""
from __future__ import annotations

import json
import os
import re
import secrets
import signal
import socket
import stat
import sys
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .db import Database
from .paths import runtime_dir
from .validation import ValidationError


# Allowed shapes for the optional client-side launch fragment. The fragment
# never reaches the server (browsers do not send it), so it can only steer the
# page the one-time URL opens — keep it free of anything sensitive anyway.
_LAUNCH_FRAGMENT_RE = re.compile(r"^servers/[A-Za-z0-9][A-Za-z0-9._-]{0,62}/credential$")


def _validate_launch_fragment(fragment: str | None) -> str | None:
    if fragment is None:
        return None
    if not _LAUNCH_FRAGMENT_RE.fullmatch(fragment):
        raise UIError(f"unsupported launch view: {fragment!r}")
    return fragment


class UIError(ValidationError):
    """A user-actionable local UI launch error."""


def _select_loopback_port(requested: int) -> int:
    """Resolve a requested port before building the browser origin.

    Port 0 asks the OS for an unused loopback port. Explicit ports are checked
    early so a bind failure produces a useful CLI error instead of a traceback
    after the one-time URL has already been emitted.
    """
    if not 0 <= requested <= 65535:
        raise UIError("UI port must be between 0 and 65535")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", requested))
        except OSError as exc:
            raise UIError(
                f"UI port {requested} is already in use; retry with --port 0 or choose another port"
            ) from exc
        return int(probe.getsockname()[1])


def _private_url_path(path: Path) -> Path:
    # Keep the final path component intact so an existing symlink cannot be
    # resolved away before O_NOFOLLOW/lstat checks.
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _write_private_url(path: Path, url: str) -> Path:
    """Write a one-time launch URL without exposing it in command output."""
    target = _private_url_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        info = target.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise UIError(f"launch URL path is not a regular file: {target}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(target, flags, 0o600)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(url)
            handle.write("\n")
    finally:
        if descriptor != -1:
            os.close(descriptor)
    if os.name == "nt":
        os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)
    return target


def _ui_state_path() -> Path:
    """The runtime record for the managed UI process (private mode-700 dir)."""
    return runtime_dir() / "ui-state.json"


class _RuntimeLock:
    """Exclusive OS lock held for one managed runtime's whole lifetime.

    The status record alone cannot serialize two launches that both pass the
    "already running" check. The kernel drops this lock when its holder exits,
    so a crashed runtime never leaves a stale lock behind. The lock file itself
    is never deleted: unlinking it would let a later launch lock a new inode.
    """

    def __init__(self) -> None:
        self._descriptor: int | None = None

    def acquire(self) -> bool:
        descriptor = os.open(runtime_dir() / "ui.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                # flock (not fcntl/lockf) so a second handle in the same
                # process conflicts too.
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(descriptor)
            return False
        self._descriptor = descriptor
        return True

    def release(self) -> None:
        if self._descriptor is None:
            return
        descriptor, self._descriptor = self._descriptor, None
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            os.close(descriptor)


def _read_ui_state() -> dict[str, Any] | None:
    try:
        raw = json.loads(_ui_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("pid"), int) or not isinstance(raw.get("port"), int):
        return None
    return raw


def _write_ui_state(state: dict[str, Any]) -> None:
    descriptor = os.open(_ui_state_path(), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(state, handle)


def _clear_ui_state(expected_pid: int | None = None) -> None:
    if expected_pid is not None:
        state = _read_ui_state()
        if state is None or state["pid"] != expected_pid:
            return
    try:
        _ui_state_path().unlink(missing_ok=True)
    except OSError:
        pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        # OpenProcess succeeds on a terminated process while any handle to it
        # is still held, so opening alone cannot distinguish alive from dead;
        # ask whether the process object is signaled (i.e. has exited).
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        SYNCHRONIZE = 0x00100000
        WAIT_TIMEOUT = 0x102
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid
        )
        if not handle:
            return False
        try:
            return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _serving_on_port(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1.0)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def ui_status() -> dict[str, Any]:
    """Report the UI process recorded by the most recent ``serverctl ui`` run."""
    state = _read_ui_state()
    if state is None:
        return {"running": False, "message": "no managed UI process is recorded"}
    # Require the recorded pid AND its loopback port so a recycled pid is not
    # mistaken for the UI.
    if not _pid_alive(state["pid"]) or not _serving_on_port(state["port"]):
        _clear_ui_state(state["pid"])
        return {"running": False, "message": "the recorded UI process is gone; removed the stale record"}
    return {
        "running": True,
        "pid": state["pid"],
        "port": state["port"],
        "started_at": state.get("started_at"),
    }


def ui_stop(*, timeout: float = 5.0) -> dict[str, Any]:
    """Stop the UI process recorded by the most recent ``serverctl ui`` run."""
    state = _read_ui_state()
    status = ui_status()
    if not status["running"]:
        return {**status, "stopped": False}
    pid = status["pid"]
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except PermissionError:
        return {
            "stopped": False,
            "running": True,
            "pid": pid,
            "port": status["port"],
            "message": "not permitted to signal the recorded UI process",
        }
    deadline = time.monotonic() + timeout
    while _pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    if _pid_alive(pid) and hasattr(signal, "SIGKILL"):
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        time.sleep(0.2)
    if _pid_alive(pid):
        return {
            "stopped": False,
            "running": True,
            "pid": pid,
            "port": status["port"],
            "message": f"the UI process did not exit within {timeout:.0f} seconds",
        }
    _clear_ui_state(pid)
    url_file = state.get("url_file") if state else None
    if url_file:
        try:
            Path(url_file).unlink(missing_ok=True)
        except OSError:
            pass
    return {"stopped": True, "running": False, "pid": pid, "port": status["port"]}


class LocalRuntime:
    """Own the server, launch URL and runtime record for one shell lifecycle.

    ``run_web`` serves in the calling thread. ``start`` waits for a background
    server, allowing a future GUI loop to stay on the main thread. All resources
    are released by ``stop`` or context-manager exit, including startup errors.
    """
    def __init__(
        self, database: Database, *, port: int = 0,
        app_factory: Callable | None = None, launch_fragment: str | None = None,
    ) -> None:
        self.database = database
        self.requested_port = port
        self.app_factory = app_factory
        self.fragment = _validate_launch_fragment(launch_fragment)
        self.port: int | None = None
        self._launch_url: str | None = None
        self._url_file: Path | None = None
        self._socket: socket.socket | None = None
        self._server: Any = None
        self._thread: threading.Thread | None = None
        self._error: BaseException | None = None
        self._owns_state = False
        self._lock = _RuntimeLock()

    @property
    def launch_url(self) -> str:
        if self._launch_url is None:
            raise UIError("local runtime has not started")
        return self._launch_url

    def _prepare(self, *, url_file: Path | None = None) -> None:
        if self._server is not None:
            raise UIError("local runtime is already started")
        # Keep the actual socket bound through Uvicorn startup. Probing and
        # closing a free port would let another process take it in between.
        if not 0 <= self.requested_port <= 65535:
            raise UIError("UI port must be between 0 and 65535")
        already_running = UIError("a managed UI is already running; use serverctl web --status or --stop")
        if ui_status()["running"]:
            raise already_running
        import uvicorn
        # Two launches can both pass the status check; only one gets the lock.
        if not self._lock.acquire():
            raise already_running
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if os.name != "nt":
            # Same as Uvicorn's own listener: TIME_WAIT connections left by a
            # just-stopped UI must not block an immediate fixed-port restart.
            # Windows is excluded because there it would allow port hijacking.
            self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._socket.bind(("127.0.0.1", self.requested_port))
        except OSError as exc:
            self._cleanup()
            raise UIError(
                f"UI port {self.requested_port} is already in use; retry with --port 0 or choose another port"
            ) from exc
        self.port = int(self._socket.getsockname()[1])
        self._socket.listen(128)
        token = secrets.token_urlsafe(32)
        self._launch_url = f"http://localhost:{self.port}/?token={token}"
        if self.fragment is not None:
            self._launch_url += f"#{self.fragment}"
        try:
            # Record the runtime before anything can use it, so --status and
            # --stop see it as soon as the URL file or listener is usable.
            _write_ui_state({
                "pid": os.getpid(), "port": self.port,
                "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "url_file": str(_private_url_path(url_file)) if url_file is not None else None,
            })
            self._owns_state = True
            if url_file is not None:
                self._url_file = _write_private_url(url_file, self.launch_url)
            factory = self.app_factory
            if factory is None:
                from .webapp import create_app
                factory = create_app
            app = factory(self.database, launch_token=token, port=self.port, launch_url_file=self._url_file)
            self._server = uvicorn.Server(uvicorn.Config(
                app, host="127.0.0.1", port=self.port, log_level="warning", access_log=False,
                timeout_graceful_shutdown=3,
            ))
        except BaseException:
            self._cleanup()
            raise

    def _serve(self) -> None:
        try:
            self._server.run(sockets=[self._socket])
        except KeyboardInterrupt:
            # Uvicorn restores and replays a foreground Ctrl-C after shutdown.
            pass
        except BaseException as exc:
            self._error = exc
        finally:
            self._cleanup()

    def _wait_ready(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while True:
            if self._server.should_exit:
                raise UIError("local backend stopped before becoming ready")
            if self._error is not None or (self._thread is not None and not self._thread.is_alive()):
                raise UIError("local backend failed to start") from self._error
            if self._server.started:
                return
            if time.monotonic() >= deadline:
                raise UIError("local backend did not become ready before the startup timeout")
            time.sleep(0.01)

    def start(self, *, timeout: float = 10.0) -> LocalRuntime:
        self._prepare()
        self._thread = threading.Thread(target=self._serve, name="ssm-local-backend", daemon=True)
        try:
            self._thread.start()
            self._wait_ready(timeout)
        except BaseException:
            self.stop()
            raise
        return self

    def run_web(self, *, open_browser: bool = True, url_file: Path | None = None) -> None:
        if not open_browser and url_file is None:
            raise UIError("--no-open requires --url-file so the one-time URL stays out of terminal output")
        self._prepare(url_file=url_file)
        if self._url_file is not None:
            print(f"SSH Server Manager launch URL written to {self._url_file}", flush=True)
        elif open_browser:
            print(f"SSH Server Manager opening in your browser on port {self.port}", flush=True)
        web_state = getattr(getattr(self._server.config.app, "state", None), "web", None)
        problem = getattr(web_state, "config_problem", None)
        if problem:
            print(f"Warning: {problem}", file=sys.stderr, flush=True)
        # Open only after Uvicorn's lifespan startup and socket registration.
        opener = None
        if open_browser:
            def open_when_ready():
                try:
                    self._wait_ready(10.0)
                    if not self._server.should_exit:
                        webbrowser.open(self.launch_url)
                except UIError:
                    pass
            opener = threading.Thread(target=open_when_ready, daemon=True)
            opener.start()
        try:
            self._serve()
            if self._error is not None:
                raise UIError("local backend stopped unexpectedly") from self._error
        finally:
            self._server.should_exit = True
            if opener is not None:
                opener.join(timeout=0.1)

    def stop(self, *, timeout: float = 5.0) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                self._server.force_exit = True
                self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                raise UIError("local backend did not stop before the shutdown timeout")
        self._cleanup()

    def _cleanup(self) -> None:
        if self._owns_state:
            _clear_ui_state(os.getpid())
            self._owns_state = False
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        if self._url_file is not None:
            try:
                self._url_file.unlink(missing_ok=True)
            except OSError:
                pass
            self._url_file = None
        self._launch_url = None
        # Last, so a successor cannot start while this runtime still owns state.
        self._lock.release()

    def __enter__(self) -> LocalRuntime:
        return self.start()

    def __exit__(self, *_exc: Any) -> None:
        self.stop()
