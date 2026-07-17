import asyncio
import json
import os
import secrets
import signal
import socket
import stat
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .auth import AuthenticationError, RevealAuth
from .db import ConflictError, Database, DatabaseError, NotFoundError
from .importer import apply_import, preview_import
from .paths import asset_dir, managed_ssh_config_path, runtime_dir
from .service import CredentialService
from .ssh_config import SSHConfigError, render_config
from .ssh_runner import SSHError, SSHRunner
from .validation import ValidationError
from .vault import VaultError, get_vault


SESSION_COOKIE = "ssm_session"


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


def _write_private_url(path: Path, url: str) -> Path:
    """Write a one-time launch URL without exposing it in command output."""
    # Keep the final path component intact so an existing symlink cannot be
    # resolved away before O_NOFOLLOW/lstat checks.
    target = Path(os.path.abspath(os.fspath(path.expanduser())))
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


@dataclass
class BrowserSession:
    csrf: str
    created_at: float = field(default_factory=time.time)
    reveal_grants: dict[str, float] = field(default_factory=dict)
    failed_master_attempts: list[float] = field(default_factory=list)


class WebState:
    def __init__(
        self,
        database: Database,
        *,
        launch_token: str,
        origin: str,
        launch_url_file: Path | None = None,
    ) -> None:
        self.database = database
        self.launch_token = launch_token
        self.launch_token_used = False
        self.launch_url_file = launch_url_file
        self.origin = origin
        self.sessions: dict[str, BrowserSession] = {}
        self.auth = RevealAuth(database, rp_id="localhost", origin=origin)

    def create_session(self) -> tuple[str, BrowserSession]:
        identifier = secrets.token_urlsafe(32)
        session = BrowserSession(csrf=secrets.token_urlsafe(32))
        self.sessions[identifier] = session
        return identifier, session

    def clean(self) -> None:
        cutoff = time.time() - 12 * 60 * 60
        self.sessions = {key: value for key, value in self.sessions.items() if value.created_at > cutoff}

    def consume_launch_url_file(self) -> None:
        if self.launch_url_file is None:
            return
        try:
            self.launch_url_file.unlink(missing_ok=True)
        except OSError:
            pass


def create_app(database: Database, *, launch_token: str, port: int, launch_url_file: Path | None = None):
    try:
        from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
        from fastapi.responses import FileResponse, JSONResponse
    except ImportError as exc:
        raise RuntimeError("UI dependencies are missing; run scripts/bootstrap") from exc

    origin = f"http://localhost:{port}"
    state = WebState(database, launch_token=launch_token, origin=origin, launch_url_file=launch_url_file)
    app = FastAPI(title="SSH Server Manager", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def security_middleware(request: Request, call_next):
        host = request.headers.get("host", "").split(":", 1)[0].strip("[]").casefold()
        if host not in {"localhost", "127.0.0.1", "::1"}:
            return JSONResponse({"error": "invalid host"}, status_code=400)
        request_origin = request.headers.get("origin")
        if request_origin and request_origin != state.origin:
            return JSONResponse({"error": "invalid origin"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(AuthenticationError)
    @app.exception_handler(ValidationError)
    @app.exception_handler(DatabaseError)
    @app.exception_handler(VaultError)
    @app.exception_handler(SSHConfigError)
    @app.exception_handler(SSHError)
    async def expected_error(_request: Request, exc: Exception):
        status = (
            404
            if isinstance(exc, NotFoundError)
            else 409
            if isinstance(exc, ConflictError)
            else 502
            if isinstance(exc, SSHError)
            else 400
        )
        return JSONResponse({"ok": False, "error": exc.__class__.__name__, "message": str(exc)}, status_code=status)

    def browser_session(request: Request) -> tuple[str, BrowserSession]:
        identifier = request.cookies.get(SESSION_COOKIE)
        session = state.sessions.get(identifier or "")
        if not identifier or not session:
            raise HTTPException(status_code=401, detail="browser session is not initialized")
        return identifier, session

    async def csrf_session(request: Request) -> tuple[str, BrowserSession]:
        identifier, session = browser_session(request)
        supplied = request.headers.get("x-csrf-token")
        if not supplied or not secrets.compare_digest(supplied, session.csrf):
            raise HTTPException(status_code=403, detail="invalid CSRF token")
        return identifier, session

    @app.get("/")
    async def index(request: Request, token: str | None = None):
        state.clean()
        identifier = request.cookies.get(SESSION_COOKIE)
        session = state.sessions.get(identifier or "")
        new_identifier = None
        if not session:
            if state.launch_token_used or not token or not secrets.compare_digest(token, state.launch_token):
                raise HTTPException(status_code=401, detail="use the one-time URL opened by serverctl ui")
            state.launch_token_used = True
            state.consume_launch_url_file()
            new_identifier, session = state.create_session()
        response = FileResponse(asset_dir() / "index.html", headers={"Cache-Control": "no-store"})
        if new_identifier:
            response.set_cookie(
                SESSION_COOKIE,
                new_identifier,
                httponly=True,
                samesite="strict",
                secure=False,
                max_age=12 * 60 * 60,
                path="/",
            )
        return response

    @app.get("/assets/{filename}")
    async def assets(filename: str, _session=Depends(browser_session)):
        if filename not in {"app.js", "styles.css", "contexts.js", "contexts.css"}:
            raise HTTPException(status_code=404)
        return FileResponse(asset_dir() / filename)

    @app.get("/api/bootstrap")
    async def bootstrap(session_data=Depends(browser_session)):
        _identifier, session = session_data
        return {
            "csrf": session.csrf,
            "servers": database.list_servers(),
            "credentials": database.list_credentials(),
            "contexts": database.list_server_contexts(),
            "auth": state.auth.status(),
            "managed_config": str(managed_ssh_config_path()),
        }

    @app.post("/api/contexts")
    async def add_context(payload: dict[str, Any] = Body(...), _session=Depends(csrf_session)):
        server_ids = payload.get("server_ids")
        if server_ids is not None and not isinstance(server_ids, list):
            raise ValidationError("server_ids must be a list")
        return database.create_server_context(
            str(payload.get("name", "")), server_ids=server_ids
        )

    @app.put("/api/contexts")
    async def update_context(payload: dict[str, Any] = Body(...), _session=Depends(csrf_session)):
        server_ids = payload.get("server_ids")
        if server_ids is not None and not isinstance(server_ids, list):
            raise ValidationError("server_ids must be a list")
        return database.update_server_context(
            str(payload.get("name", "")),
            new_name=str(payload["new_name"]) if "new_name" in payload else None,
            server_ids=server_ids,
        )

    @app.delete("/api/contexts")
    async def remove_context(payload: dict[str, Any] = Body(...), _session=Depends(csrf_session)):
        return database.delete_server_context(str(payload.get("name", "")))

    @app.post("/api/servers")
    async def add_server(payload: dict[str, Any] = Body(...), _session=Depends(csrf_session)):
        result = database.create_server(**payload)
        render_config(database)
        return result

    @app.put("/api/servers/{identifier}")
    async def update_server(identifier: str, payload: dict[str, Any] = Body(...), _session=Depends(csrf_session)):
        result = database.update_server(identifier, **payload)
        render_config(database)
        return result

    @app.delete("/api/servers/{identifier}")
    async def remove_server(identifier: str, _session=Depends(csrf_session)):
        result = database.delete_server(identifier)
        render_config(database)
        return result

    @app.post("/api/servers/{identifier}/test")
    async def test_server(identifier: str, _session=Depends(csrf_session)):
        return await asyncio.to_thread(SSHRunner(database).test, identifier)

    @app.get("/api/servers/{identifier}/files")
    async def list_server_files(
        identifier: str,
        path: str | None = None,
        _session=Depends(browser_session),
    ):
        return await asyncio.to_thread(SSHRunner(database).list_directory, identifier, path)

    @app.post("/api/import/preview")
    async def import_preview(payload: dict[str, Any] = Body(default={}), _session=Depends(csrf_session)):
        return await asyncio.to_thread(preview_import, database, config=payload.get("config"))

    @app.post("/api/import/apply")
    async def import_apply(payload: dict[str, Any] = Body(...), _session=Depends(csrf_session)):
        preview = await asyncio.to_thread(preview_import, database, config=payload.get("config"))
        result = apply_import(database, preview, overwrite=bool(payload.get("overwrite")))
        render_config(database)
        return result

    @app.post("/api/credentials")
    async def add_credential(payload: dict[str, Any] = Body(...), _session=Depends(csrf_session)):
        service = CredentialService(database, get_vault())
        kind = payload.get("kind")
        if kind == "password":
            result = service.create_password(payload.get("label", ""), payload.get("secret", ""))
        elif kind == "key":
            result = service.create_key(
                payload.get("label", ""), payload.get("key_path", ""), payload.get("passphrase") or None
            )
        elif kind == "agent":
            result = service.create_agent(payload.get("label", ""))
        else:
            raise ValidationError("unsupported credential kind")
        render_config(database)
        return result

    @app.put("/api/credentials/{identifier}")
    async def update_credential(
        identifier: str, payload: dict[str, Any] = Body(...), _session=Depends(csrf_session)
    ):
        service = CredentialService(database, get_vault())
        result = service.update(
            identifier,
            label=payload.get("label"),
            key_path=payload.get("key_path"),
            secret=payload.get("secret"),
            passphrase=payload.get("passphrase"),
            clear_passphrase=bool(payload.get("clear_passphrase")),
        )
        render_config(database)
        return result

    @app.delete("/api/credentials/{identifier}")
    async def remove_credential(identifier: str, _session=Depends(csrf_session)):
        result = CredentialService(database, get_vault()).delete(identifier)
        render_config(database)
        return result

    @app.post("/api/auth/passkey/register/options")
    async def passkey_register_options(session_data=Depends(csrf_session)):
        identifier, _session = session_data
        return state.auth.begin_registration(identifier)

    @app.post("/api/auth/passkey/register/verify")
    async def passkey_register_verify(
        payload: dict[str, Any] = Body(...), session_data=Depends(csrf_session)
    ):
        identifier, _session = session_data
        return state.auth.finish_registration(identifier, payload)

    @app.post("/api/auth/master/enroll")
    async def master_enroll(payload: dict[str, Any] = Body(...), _session=Depends(csrf_session)):
        return state.auth.enroll_master_password(payload.get("password", ""))

    @app.post("/api/auth/reveal/options")
    async def reveal_options(payload: dict[str, Any] = Body(...), session_data=Depends(csrf_session)):
        identifier, _session = session_data
        database.get_credential(payload.get("credential_id", ""))
        return state.auth.begin_authentication(identifier)

    @app.post("/api/auth/reveal/verify")
    async def reveal_verify(payload: dict[str, Any] = Body(...), session_data=Depends(csrf_session)):
        identifier, session = session_data
        credential_id = payload.get("credential_id", "")
        state.auth.finish_authentication(identifier, payload.get("response", {}))
        session.reveal_grants[credential_id] = time.time() + 30
        return {"ok": True}

    @app.post("/api/auth/master/verify")
    async def master_verify(payload: dict[str, Any] = Body(...), session_data=Depends(csrf_session)):
        _identifier, session = session_data
        now = time.time()
        session.failed_master_attempts = [item for item in session.failed_master_attempts if item > now - 60]
        if len(session.failed_master_attempts) >= 5:
            raise AuthenticationError("too many failed attempts; wait one minute")
        if not state.auth.verify_master_password(payload.get("password", "")):
            session.failed_master_attempts.append(now)
            raise AuthenticationError("master password is incorrect")
        credential_id = payload.get("credential_id", "")
        database.get_credential(credential_id)
        session.reveal_grants[credential_id] = now + 30
        return {"ok": True}

    @app.post("/api/credentials/{identifier}/reveal")
    async def reveal_credential(identifier: str, session_data=Depends(csrf_session)):
        _session_id, session = session_data
        expires = session.reveal_grants.pop(identifier, 0)
        if expires < time.time():
            raise AuthenticationError("reauthentication is required")
        result = CredentialService(database, get_vault()).reveal(identifier)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    return app


def _ui_state_path() -> Path:
    """The runtime record for the managed UI process (private mode-700 dir)."""
    return runtime_dir() / "ui-state.json"


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


def run_ui(
    *, database: Database, port: int = 0, open_browser: bool = True, url_file: Path | None = None
) -> None:
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError("UI dependencies are missing; run scripts/bootstrap") from exc
    if not open_browser and url_file is None:
        raise UIError("--no-open requires --url-file so the one-time URL stays out of terminal output")
    actual_port = _select_loopback_port(port)
    launch_token = secrets.token_urlsafe(32)
    url = f"http://localhost:{actual_port}/?token={launch_token}"
    written: Path | None = None
    if url_file is not None:
        written = _write_private_url(url_file, url)
        print(f"SSH Server Manager launch URL written to {written}", flush=True)
    elif open_browser:
        print(f"SSH Server Manager opening in your browser on port {actual_port}", flush=True)
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    app = create_app(database, launch_token=launch_token, port=actual_port, launch_url_file=url_file)
    _write_ui_state(
        {
            "pid": os.getpid(),
            "port": actual_port,
            "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "url_file": str(written) if written is not None else None,
        }
    )
    try:
        uvicorn.run(app, host="127.0.0.1", port=actual_port, log_level="warning", access_log=False)
    finally:
        _clear_ui_state(os.getpid())
        if url_file is not None:
            try:
                url_file.unlink(missing_ok=True)
            except OSError:
                pass
