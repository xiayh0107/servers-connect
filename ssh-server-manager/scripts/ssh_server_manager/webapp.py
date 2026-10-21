import asyncio
import os
import posixpath
import secrets
import shutil
import stat
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .application import ApplicationServices
from .auth import AuthenticationError, RevealAuth
from .db import ConflictError, Database, DatabaseError, NotFoundError
from .paths import asset_dir, error_log_path, managed_ssh_config_path, runtime_dir
from .ssh_config import SSHConfigError, render_config
from .ssh_runner import SSHError, SSHRunner
from .runtime import (
    LocalRuntime, UIError, _read_ui_state, _select_loopback_port,
    _write_ui_state, ui_status, ui_stop,
)
from .validation import ValidationError
from .vault import VaultError, get_vault


SESSION_COOKIE = "ssm_session"
# The browser stages transfers through memory and a temp file; large files
# belong on the CLI path, which streams straight through sftp.
MAX_WEB_TRANSFER_BYTES = 100 * 1024 * 1024


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
        # Two private locations decide whether host actions can work at all: the
        # host database (every action records its result) and the managed SSH
        # config. Probe both once at startup instead of failing per request.
        self.config_problem: str | None = None
        self.check_config(database)

    def check_config(self, database: Database) -> None:
        from .config_errors import database_write_problem, directory_write_problem

        # Both failures share one cause and one fix, so report the locations
        # together rather than repeating the remedy per location.
        locations = [
            str(path) for path, problem in (
                (database.path.parent, database_write_problem(database.path)),
                (managed_ssh_config_path().parent, directory_write_problem(managed_ssh_config_path().parent)),
            ) if problem is not None
        ]
        if not locations:
            self.config_problem = None
            return
        from .config_errors import UI_WRITE_NOTE

        self.config_problem = (
            f"this app cannot write {', '.join(locations)}; host actions that record "
            f"results or render the managed config will fail. {UI_WRITE_NOTE}."
        )

    def create_session(self) -> tuple[str, BrowserSession]:
        identifier = secrets.token_urlsafe(32)
        session = BrowserSession(csrf=secrets.token_urlsafe(32))
        self.sessions[identifier] = session
        return identifier, session

    def clean(self) -> None:
        cutoff = time.time() - 12 * 60 * 60
        self.sessions = {key: value for key, value in self.sessions.items() if value.created_at > cutoff}
        browser = getattr(self, "local_key_browser", None)
        if browser is not None:
            browser.clean(set(self.sessions))

    def consume_launch_url_file(self) -> None:
        if self.launch_url_file is None:
            return
        try:
            self.launch_url_file.unlink(missing_ok=True)
        except OSError:
            pass


def open_error_log():
    """Append-mode handle for the private UI error log; stderr when unavailable."""
    try:
        path = error_log_path()
        handle = open(path, "a", encoding="utf-8")
        if os.name != "nt":
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        return handle
    except (OSError, RuntimeError):
        return sys.stderr


def create_app(database: Database, *, launch_token: str, port: int, launch_url_file: Path | None = None):
    try:
        from fastapi import (
            Body,
            Depends,
            FastAPI,
            File,
            Form,
            HTTPException,
            Request,
            Response,
            UploadFile,
        )
        from fastapi.exceptions import RequestValidationError
        from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
        from fastapi.security import APIKeyCookie, APIKeyHeader
        from starlette.exceptions import HTTPException as StarletteHTTPException
    except ImportError as exc:
        raise RuntimeError("UI dependencies are missing; run scripts/bootstrap") from exc

    services = ApplicationServices(
        database, runner_factory=lambda: SSHRunner(database),
        vault_factory=lambda: get_vault(), config_renderer=render_config,
    )
    origin = f"http://localhost:{port}"
    state = WebState(database, launch_token=launch_token, origin=origin, launch_url_file=launch_url_file)
    app = FastAPI(title="SSH Server Manager", docs_url=None, redoc_url=None, openapi_url=None)

    app.state.services = services
    app.state.web = state

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, _exc: RequestValidationError):
        # Pydantic error records can include the supplied password or an entire
        # credential body. Never echo validation input, even on legacy routes.
        return JSONResponse(
            {"ok": False, "error": "RequestValidationError", "message": "request validation failed"},
            status_code=422,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        content = (
            {"ok": False, "error": "HTTPException", "message": str(exc.detail)}
            if request.url.path.startswith("/api/v1/") else {"detail": exc.detail}
        )
        return JSONResponse(content, status_code=exc.status_code, headers=exc.headers)

    @app.middleware("http")
    async def security_middleware(request: Request, call_next):
        host = request.headers.get("host", "").split(":", 1)[0].strip("[]").casefold()
        if host not in {"localhost", "127.0.0.1", "::1"}:
            return JSONResponse(
                {"ok": False, "error": "InvalidHost", "message": "invalid host"}
                if request.url.path.startswith("/api/v1/") else {"error": "invalid host"},
                status_code=400, headers={"Cache-Control": "no-store"},
            )
        request_origin = request.headers.get("origin")
        if request_origin and request_origin != state.origin:
            return JSONResponse(
                {"ok": False, "error": "InvalidOrigin", "message": "invalid origin"}
                if request.url.path.startswith("/api/v1/") else {"error": "invalid origin"},
                status_code=403, headers={"Cache-Control": "no-store"},
            )
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

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        # Only failures no expected-error handler claimed reach this point: they
        # surface in the UI as "Request failed (500)". Record the traceback to
        # the private runtime log (never the request body, which can carry
        # secrets) so the next report is actionable, and keep the response body
        # free of internal detail.
        try:
            with open_error_log() as handle:
                handle.write(f"\n{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {request.method} {request.url.path}\n")
                traceback.print_exception(exc, file=handle)
                handle.flush()
        except OSError:
            pass
        if request.url.path.startswith("/api/"):
            return JSONResponse(
                {"ok": False, "error": "InternalServerError", "message": "internal server error"},
                status_code=500,
            )
        return JSONResponse({"error": "internal server error"}, status_code=500)

    @app.exception_handler(AuthenticationError)
    @app.exception_handler(ValidationError)
    @app.exception_handler(DatabaseError)
    @app.exception_handler(VaultError)
    @app.exception_handler(SSHConfigError)
    @app.exception_handler(SSHError)
    async def expected_error(_request: Request, exc: Exception):
        from .ssh_runner import FileOperationError
        status = (
            404
            if isinstance(exc, NotFoundError)
            else 409
            if isinstance(exc, ConflictError)
            else 502
            if isinstance(exc, (SSHError, SSHConfigError))
            else 400
        )
        payload = {"ok": False, "error": exc.__class__.__name__, "message": str(exc)}
        if isinstance(exc, FileOperationError):
            if _request.url.path.startswith("/api/v1/hosts/"):
                status = {"not_found": 404, "exists": 409, "permission_denied": 403,
                          "too_large": 413, "unsupported": 501}.get(exc.error_code, 502)
                payload["error_code"] = exc.error_code
                if exc.existing is not None:
                    from .api_models import FileConflictEntry
                    payload["existing"] = FileConflictEntry.model_validate(exc.existing).model_dump()
            elif exc.error_code == "too_large":
                status = 400
            elif exc.error_code == "exists":
                status = 409
        return JSONResponse(payload, status_code=status)

    cookie_scheme = APIKeyCookie(name=SESSION_COOKIE, auto_error=False)
    csrf_scheme = APIKeyHeader(name="X-CSRF-Token", auto_error=False)

    def browser_session(request: Request, _cookie=Depends(cookie_scheme)) -> tuple[str, BrowserSession]:
        state.clean()
        identifier = request.cookies.get(SESSION_COOKIE)
        session = state.sessions.get(identifier or "")
        if not identifier or not session or session.created_at <= time.time() - 12 * 60 * 60:
            raise HTTPException(status_code=401, detail="browser session is not initialized")
        return identifier, session

    async def csrf_session(request: Request, supplied=Depends(csrf_scheme)) -> tuple[str, BrowserSession]:
        identifier, session = browser_session(request)
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
        html = (asset_dir() / "index.html").read_text(encoding="utf-8")
        response = HTMLResponse(html, headers={"Cache-Control": "no-store"})
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
        from .ui_assets import resource_names

        if filename not in resource_names(asset_dir()):
            raise HTTPException(status_code=404)
        return FileResponse(asset_dir() / filename, headers={"Cache-Control": "private, max-age=31536000, immutable"})

    @app.get("/api/bootstrap")
    async def bootstrap(session_data=Depends(browser_session)):
        _identifier, session = session_data
        return {
            "csrf": session.csrf,
            "servers": services.hosts.list(),
            "credentials": services.credentials.list(),
            "contexts": services.contexts.list(),
            "skills": services.skills.list(),
            "auth": state.auth.status(),
            "managed_config": str(managed_ssh_config_path()),
        }

    @app.post("/api/contexts")
    async def add_context(payload: dict[str, Any] = Body(...), _session=Depends(csrf_session)):
        server_ids = payload.get("server_ids")
        if server_ids is not None and not isinstance(server_ids, list):
            raise ValidationError("server_ids must be a list")
        return services.contexts.create(
            str(payload.get("name", "")), server_ids=server_ids
        )

    @app.put("/api/contexts")
    async def update_context(payload: dict[str, Any] = Body(...), _session=Depends(csrf_session)):
        server_ids = payload.get("server_ids")
        if server_ids is not None and not isinstance(server_ids, list):
            raise ValidationError("server_ids must be a list")
        return services.contexts.update(
            str(payload.get("name", "")),
            new_name=str(payload["new_name"]) if "new_name" in payload else None,
            server_ids=server_ids,
        )

    @app.delete("/api/contexts")
    async def remove_context(payload: dict[str, Any] = Body(...), _session=Depends(csrf_session)):
        return services.contexts.delete(str(payload.get("name", "")))

    @app.get("/api/skills/discover")
    async def discover_skills(_session=Depends(browser_session)):
        return await asyncio.to_thread(services.skills.discover)

    @app.post("/api/skills")
    async def add_skill(payload: dict[str, Any] = Body(...), _session=Depends(csrf_session)):
        server_ids = payload.get("server_ids")
        if server_ids is not None and not isinstance(server_ids, list):
            raise ValidationError("server_ids must be a list")
        return await asyncio.to_thread(
            services.skills.register,
            str(payload.get("path", "")),
            server_identifiers=server_ids,
        )

    @app.post("/api/skills/{identifier}/refresh")
    async def refresh_skill(
        identifier: str, payload: dict[str, Any] = Body(default={}), _session=Depends(csrf_session)
    ):
        path = str(payload["path"]) if payload.get("path") is not None else None
        return await asyncio.to_thread(services.skills.refresh, identifier, path=path)

    @app.put("/api/skills/{identifier}/servers")
    async def set_skill_servers(
        identifier: str, payload: dict[str, Any] = Body(...), _session=Depends(csrf_session)
    ):
        server_ids = payload.get("server_ids")
        if not isinstance(server_ids, list):
            raise ValidationError("server_ids must be a list")
        return services.skills.set_servers(identifier, server_ids)

    @app.delete("/api/skills/{identifier}")
    async def remove_skill(identifier: str, _session=Depends(csrf_session)):
        return services.skills.delete(identifier)

    @app.post("/api/servers")
    async def add_server(payload: dict[str, Any] = Body(...), _session=Depends(csrf_session)):
        result = services.hosts.create(**payload)
        return result

    @app.put("/api/servers/{identifier}")
    async def update_server(identifier: str, payload: dict[str, Any] = Body(...), _session=Depends(csrf_session)):
        result = services.hosts.update(identifier, **payload)
        return result

    @app.delete("/api/servers/{identifier}")
    async def remove_server(identifier: str, _session=Depends(csrf_session)):
        result = services.hosts.delete(identifier)
        return result

    @app.put("/api/servers/{identifier}/skills")
    async def set_server_skills(
        identifier: str, payload: dict[str, Any] = Body(...), _session=Depends(csrf_session)
    ):
        skill_ids = payload.get("skill_ids")
        if not isinstance(skill_ids, list):
            raise ValidationError("skill_ids must be a list")
        return await asyncio.to_thread(
            services.skills.set_server_skills, identifier, skill_ids
        )

    @app.post("/api/servers/{identifier}/test")
    async def test_server(identifier: str, _session=Depends(csrf_session)):
        return await asyncio.to_thread(services.diagnostics.test, identifier)

    @app.post("/api/servers/{identifier}/diagnose")
    async def diagnose_server(identifier: str, _session=Depends(csrf_session)):
        return await asyncio.to_thread(services.diagnostics.diagnose, identifier)

    @app.post("/api/servers/{identifier}/notes")
    @app.put("/api/servers/{identifier}/notes")
    async def update_server_notes(
        identifier: str, payload: dict[str, Any] = Body(...), _session=Depends(csrf_session)
    ):
        mode = str(payload.get("mode", "append"))
        if mode not in {"append", "set", "clear"}:
            raise ValidationError("note mode must be append, set, or clear")
        if mode == "append" and "text" not in payload:
            raise ValidationError("note text is required when appending")
        text = "" if mode == "clear" else payload.get("text", "")
        return await asyncio.to_thread(
            services.hosts.notes, identifier, text, mode=mode
        )

    @app.get("/api/servers/{identifier}/files")
    async def list_server_files(
        identifier: str,
        path: str | None = None,
        _session=Depends(browser_session),
    ):
        return await asyncio.to_thread(services.files.list_directory, identifier, path)

    @app.get("/api/servers/{identifier}/paths")
    async def list_saved_paths(identifier: str, _session=Depends(browser_session)):
        return await asyncio.to_thread(services.files.list_paths, identifier)

    @app.post("/api/servers/{identifier}/paths")
    async def create_saved_path(
        identifier: str,
        payload: dict[str, Any] = Body(...),
        _session=Depends(csrf_session),
    ):
        return await asyncio.to_thread(
            services.files.create_path,
            identifier,
            label=payload.get("label", ""),
            path=payload.get("path"),
            notes=payload.get("notes"),
        )

    @app.put("/api/servers/{identifier}/paths")
    async def replace_saved_paths(
        identifier: str,
        payload: dict[str, Any] = Body(...),
        _session=Depends(csrf_session),
    ):
        entries = payload.get("paths")
        if not isinstance(entries, list):
            raise ValidationError("paths must be a list")
        return await asyncio.to_thread(services.files.replace_paths, identifier, entries)

    @app.post("/api/servers/{identifier}/paths/{path_id}/used")
    async def touch_saved_path(
        identifier: str, path_id: str, _session=Depends(csrf_session)
    ):
        return await asyncio.to_thread(services.files.touch_path, identifier, path_id)

    @app.delete("/api/servers/{identifier}/paths/{path_id}")
    async def delete_saved_path(
        identifier: str, path_id: str, _session=Depends(csrf_session)
    ):
        return await asyncio.to_thread(services.files.delete_path, identifier, path_id)

    # Transfers are POST, including the download. A plain <a href> cannot carry
    # the CSRF header, and a GET that moves bytes off a host is exactly the
    # request a hostile page would try to forge.
    @app.post("/api/servers/{identifier}/download")
    async def download_server_file(
        identifier: str,
        payload: dict[str, Any] = Body(...),
        _session=Depends(csrf_session),
    ):
        remote_path = payload.get("path")
        if not isinstance(remote_path, str) or not remote_path.strip():
            raise ValidationError("path is required")
        from .remote_files import validate_web_file_path
        from .ssh_runner import FileOperationError
        remote_path = validate_web_file_path(remote_path)
        name = posixpath.basename(remote_path.rstrip("/")) or "download"
        if name in {".", ".."}:
            raise ValidationError("path must name a file")
        staging = Path(tempfile.mkdtemp(dir=runtime_dir(), prefix="dl-"))
        target = staging / "download"
        try:
            result = await asyncio.to_thread(
                services.files.download, identifier, remote_path, target, max_bytes=MAX_WEB_TRANSFER_BYTES
            )
            if result["bytes"] > MAX_WEB_TRANSFER_BYTES:
                raise FileOperationError(
                    f"file is larger than the {MAX_WEB_TRANSFER_BYTES // (1024 * 1024)} MB "
                    "web transfer limit; use serverctl get instead", "too_large"
                )
            data = await asyncio.to_thread(target.read_bytes)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        return Response(
            content=data,
            media_type="application/octet-stream",
            headers={
                # RFC 5987 form so non-ASCII filenames survive the header.
                "Content-Disposition": (
                    "attachment; filename*=UTF-8''" + quote(name, safe="")
                ),
                "Cache-Control": "no-store",
            },
        )

    @app.post("/api/servers/{identifier}/upload")
    async def upload_server_file(
        identifier: str,
        file: UploadFile = File(...),
        path: str = Form(...),
        _session=Depends(csrf_session),
        overwrite: bool = Form(False),
    ):
        from .remote_files import validate_web_file_path
        from .ssh_runner import FileOperationError
        path = validate_web_file_path(path)
        staging = Path(tempfile.mkdtemp(dir=runtime_dir(), prefix="ul-"))
        source = staging / "upload"
        try:
            written = 0
            with source.open("wb") as handle:
                while chunk := await file.read(1024 * 1024):
                    written += len(chunk)
                    if written > MAX_WEB_TRANSFER_BYTES:
                        raise FileOperationError(
                            f"upload exceeds the {MAX_WEB_TRANSFER_BYTES // (1024 * 1024)} MB "
                            "web transfer limit; use serverctl put instead", "too_large"
                        )
                    handle.write(chunk)
            return await asyncio.to_thread(
                services.files.upload, identifier, source, path, overwrite=overwrite
            )
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    @app.post("/api/import/preview")
    async def import_preview(payload: dict[str, Any] = Body(default={}), _session=Depends(csrf_session)):
        return await asyncio.to_thread(services.imports.preview, config=payload.get("config"))

    @app.post("/api/import/apply")
    async def import_apply(payload: dict[str, Any] = Body(...), _session=Depends(csrf_session)):
        preview = await asyncio.to_thread(services.imports.preview, config=payload.get("config"))
        result = services.imports.apply(preview, overwrite=bool(payload.get("overwrite")))
        return result

    @app.post("/api/credentials")
    async def add_credential(payload: dict[str, Any] = Body(...), _session=Depends(csrf_session)):
        service = services.credentials
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
        return result

    @app.put("/api/credentials/{identifier}")
    async def update_credential(
        identifier: str, payload: dict[str, Any] = Body(...), _session=Depends(csrf_session)
    ):
        service = services.credentials
        result = service.update(
            identifier,
            label=payload.get("label"),
            key_path=payload.get("key_path"),
            secret=payload.get("secret"),
            passphrase=payload.get("passphrase"),
            clear_passphrase=bool(payload.get("clear_passphrase")),
        )
        return result

    @app.delete("/api/credentials/{identifier}")
    async def remove_credential(identifier: str, _session=Depends(csrf_session)):
        result = services.credentials.delete(identifier)
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
        services.credentials.get(payload.get("credential_id", ""))
        return state.auth.begin_authentication(identifier)

    @app.post("/api/auth/reveal/verify")
    async def reveal_verify(payload: dict[str, Any] = Body(...), session_data=Depends(csrf_session)):
        identifier, session = session_data
        credential_id = services.credentials.get(payload.get("credential_id", ""))["id"]
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
        credential_id = services.credentials.get(payload.get("credential_id", ""))["id"]
        session.reveal_grants[credential_id] = now + 30
        return {"ok": True}

    @app.post("/api/credentials/{identifier}/reveal")
    async def reveal_credential(identifier: str, session_data=Depends(csrf_session)):
        _session_id, session = session_data
        credential_id = services.credentials.get(identifier)["id"]
        expires = session.reveal_grants.pop(credential_id, 0)
        if expires < time.time():
            raise AuthenticationError("reauthentication is required")
        result = services.credentials.reveal(credential_id)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    from .api_v1 import build_router

    app.include_router(build_router(
        services, state=state, browser_session=browser_session, csrf_session=csrf_session,
        transfer_limit=MAX_WEB_TRANSFER_BYTES,
        handlers={
            "download": download_server_file, "upload": upload_server_file,
            "master_enroll": master_enroll, "master_verify": master_verify,
            "register_options": passkey_register_options, "register_verify": passkey_register_verify,
            "reveal_options": reveal_options, "reveal_verify": reveal_verify, "reveal": reveal_credential,
        },
    ))

    return app


def run_ui(
    *,
    database: Database,
    port: int = 0,
    open_browser: bool = True,
    url_file: Path | None = None,
    launch_fragment: str | None = None,
) -> None:
    # Compatibility entry point; both CLI spellings use the same runtime.
    LocalRuntime(database, port=port, app_factory=create_app, launch_fragment=launch_fragment).run_web(
        open_browser=open_browser, url_file=url_file,
    )
