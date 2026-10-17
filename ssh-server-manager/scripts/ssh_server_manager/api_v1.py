"""Typed transport adapter; all use cases go through ApplicationServices."""
from __future__ import annotations

from typing import Any, Callable
import asyncio
import json
import queue
import threading
from datetime import datetime, timezone
from fastapi.responses import StreamingResponse

from fastapi import APIRouter, Body, Depends, File, Form, Response, UploadFile

from . import __version__
from . import api_models as dto
from .application import ApplicationServices
from .credential_keys import read_snapshot
from .local_metadata import LocalKeyBrowser, agent_metadata
from .validation import ValidationError


class NDJSONResponse(StreamingResponse):
    media_type = "application/x-ndjson"


def build_router(
    services: ApplicationServices,
    *,
    state: Any,
    browser_session: Callable,
    csrf_session: Callable,
    handlers: dict[str, Callable],
    transfer_limit: int,
) -> APIRouter:
    key_browser = LocalKeyBrowser()
    state.local_key_browser = key_browser

    def clean_local_metadata(_session=Depends(browser_session)):
        state.clean()
        key_browser.clean(set(state.sessions))

    router = APIRouter(
        prefix="/api/v1",
        dependencies=[Depends(browser_session), Depends(clean_local_metadata)],
        responses={
            **{code: {"model": dto.ErrorResponse} for code in (400, 401, 403, 404, 409, 501, 502)},
            # HTTPStatus phrases differ between supported Python versions.
            413: {"model": dto.ErrorResponse, "description": "Content Too Large"},
            422: {"model": dto.ErrorResponse, "description": "Unprocessable Content"},
        },
    )

    @router.get("/runtime", response_model=dto.RuntimeInfo, tags=["runtime"])
    def runtime(session_data=Depends(browser_session)):
        return {
            "version": __version__,
            "csrf": session_data[1].csrf,
            "auth": state.auth.status(),
            "capabilities": {"max_web_transfer_bytes": transfer_limit},
            "session": {"expires_at": datetime.fromtimestamp(session_data[1].created_at + 12 * 60 * 60, timezone.utc).isoformat()},
            "server_time": datetime.now(timezone.utc).isoformat(),
            "vault": services.credentials.vault_metadata,
        }

    @router.get("/hosts", response_model=list[dto.Host], tags=["hosts"])
    def hosts(query: str | None = None, tag: str | None = None):
        return services.hosts.list(query=query, tag=tag)

    @router.post("/hosts/test", response_model=dto.BulkTestResult, tags=["diagnostics"])
    def test_hosts(payload: dto.BulkProbeRequest, _session=Depends(csrf_session)):
        return services.diagnostics.test_many(
            payload.identifiers, timeout=payload.timeout, concurrency=payload.concurrency,
        )

    @router.post("/hosts/observe", response_model=dto.BulkTestResult, tags=["diagnostics"])
    def observe_hosts(payload: dto.BulkProbeRequest, _session=Depends(csrf_session)):
        return services.diagnostics.observe_many(
            payload.identifiers, timeout=payload.timeout, concurrency=payload.concurrency,
        )

    @router.get("/hosts/{identifier}", response_model=dto.Host, tags=["hosts"])
    def host(identifier: str):
        return services.hosts.get(identifier)

    @router.get("/hosts/{identifier}/workspace", response_model=dto.Workspace, tags=["hosts"])
    def workspace(identifier: str):
        return services.hosts.workspace(identifier)

    @router.get("/hosts/{identifier}/route", response_model=dto.HostRoute, tags=["hosts"])
    def route(identifier: str):
        return services.hosts.route(identifier)

    @router.post("/hosts", response_model=dto.Host, status_code=201, tags=["hosts"])
    def create_host(payload: dto.HostCreate, _session=Depends(csrf_session)):
        return services.hosts.create(**payload.model_dump())

    # Partial update only; PUT is deliberately absent because it would promise
    # full-replacement semantics the core does not implement.
    @router.patch("/hosts/{identifier}", response_model=dto.Host, tags=["hosts"])
    def update_host(identifier: str, payload: dto.HostUpdate, _session=Depends(csrf_session)):
        return services.hosts.update(identifier, **payload.model_dump(exclude_unset=True))

    @router.delete("/hosts/{identifier}", response_model=dto.Host, tags=["hosts"])
    def delete_host(identifier: str, _session=Depends(csrf_session)):
        return services.hosts.delete(identifier)

    @router.put("/hosts/{identifier}/notes", response_model=dto.Host, tags=["hosts"])
    def notes(identifier: str, payload: dto.NotesUpdate, _session=Depends(csrf_session)):
        return services.hosts.notes(identifier, payload.text, mode=payload.mode)

    @router.post("/hosts/{identifier}/test", response_model=dto.TestResult, tags=["diagnostics"])
    def test(identifier: str, payload: dto.ProbeRequest | None = None, _session=Depends(csrf_session)):
        return services.diagnostics.test(identifier, timeout=payload.timeout if payload else 8)

    @router.post("/hosts/{identifier}/observe", response_model=dto.TestResult, tags=["diagnostics"])
    def observe(identifier: str, payload: dto.ProbeRequest | None = None, _session=Depends(csrf_session)):
        return services.diagnostics.observe(identifier, timeout=payload.timeout if payload else 8)

    @router.post("/hosts/{identifier}/diagnose", response_model=dto.Diagnosis, tags=["diagnostics"])
    def diagnose(identifier: str, payload: dto.ProbeRequest | None = None, _session=Depends(csrf_session)):
        return services.diagnostics.diagnose(identifier, timeout=payload.timeout if payload else 8)

    @router.get("/hosts/{identifier}/files", response_model=dto.DirectoryListing, tags=["files"])
    def files(identifier: str, path: str | None = None, name_filter: str | None = None):
        return services.files.list_directory(identifier, path, name_filter=name_filter)

    @router.post(
        "/hosts/{identifier}/download", response_class=Response, tags=["files"],
        responses={200: {"content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}}}},
    )
    async def download(identifier: str, payload: dto.DownloadRequest, session_data=Depends(csrf_session)):
        return await handlers["download"](identifier, payload.model_dump(), session_data)

    @router.post("/hosts/{identifier}/upload", response_model=dto.TransferResult, tags=["files"])
    async def upload(
        identifier: str, file: UploadFile = File(...), path: str = Form(...),
        overwrite: bool = Form(False),
        session_data=Depends(csrf_session),
    ):
        return await handlers["upload"](identifier, file, path, session_data, overwrite=overwrite)

    @router.get("/hosts/{identifier}/paths", response_model=list[dto.SavedPath], tags=["paths"])
    def paths(identifier: str):
        return services.files.list_paths(identifier)

    @router.post("/hosts/{identifier}/paths", response_model=dto.SavedPath, status_code=201, tags=["paths"])
    def create_path(identifier: str, payload: dto.PathCreate, _session=Depends(csrf_session)):
        return services.files.create_path(identifier, **payload.model_dump())

    @router.put("/hosts/{identifier}/paths", response_model=list[dto.SavedPath], tags=["paths"])
    def replace_paths(identifier: str, payload: dto.PathsReplace, _session=Depends(csrf_session)):
        return services.files.replace_paths(identifier, payload.model_dump()["paths"])

    @router.patch("/hosts/{identifier}/paths/{path_id}", response_model=dto.SavedPath, tags=["paths"])
    def update_path(identifier: str, path_id: str, payload: dto.PathUpdate, _session=Depends(csrf_session)):
        return services.files.update_path(identifier, path_id, **payload.model_dump(exclude_unset=True))

    @router.post("/hosts/{identifier}/paths/{path_id}/used", response_model=dto.SavedPath, tags=["paths"])
    def touch_path(identifier: str, path_id: str, _session=Depends(csrf_session)):
        return services.files.touch_path(identifier, path_id)

    @router.delete("/hosts/{identifier}/paths/{path_id}", response_model=dto.SavedPath, tags=["paths"])
    def delete_path(identifier: str, path_id: str, _session=Depends(csrf_session)):
        return services.files.delete_path(identifier, path_id)

    @router.get("/credentials", response_model=list[dto.Credential], tags=["credentials"])
    def credentials():
        return services.credentials.list()

    def expected_revision(session_id: str, token: str | None, key_path: str) -> str | None:
        if token is None:
            return None
        snapshot = read_snapshot(key_path)
        return key_browser.expected_revision(session_id, token, str(snapshot.path))

    @router.post("/credentials/key/inspect", response_model=dto.KeyInspection, tags=["credentials"])
    def inspect_key(payload: dto.KeyInspectRequest, response: Response, session_data=Depends(csrf_session)):
        response.headers["Cache-Control"] = "no-store"
        metadata, revision = services.credentials.inspect_key(payload.key_path)
        return {"key_metadata": metadata, "key_revision": key_browser.revision(session_data[0], metadata["path"], revision)}

    @router.post("/credentials/key/validate", response_model=dto.KeyInspection, tags=["credentials"])
    def validate_key(payload: dto.KeyValidateRequest, response: Response, session_data=Depends(csrf_session)):
        response.headers["Cache-Control"] = "no-store"
        metadata, revision = services.credentials.inspect_key(payload.key_path,
            passphrase=payload.passphrase.get_secret_value() if payload.passphrase else None,
            credential_id=payload.credential_id, use_stored_passphrase=payload.use_stored_passphrase,
            expected_key_revision=expected_revision(session_data[0], payload.expected_key_revision, payload.key_path))
        if metadata["status"] != "valid":
            raise ValidationError("private key requires a passphrase before it can be validated")
        return {"key_metadata": metadata, "key_revision": key_browser.revision(session_data[0], metadata["path"], revision)}

    @router.post("/local/key-browser/scopes", response_model=dto.KeyBrowserScope, tags=["local"])
    def key_browser_scope(payload: dto.KeyBrowserScopeRequest, response: Response, session_data=Depends(csrf_session)):
        response.headers["Cache-Control"] = "no-store"
        return key_browser.scope(session_data[0], payload.root_path)

    @router.post("/local/key-browser/list", response_model=dto.KeyBrowserListing, tags=["local"])
    def key_browser_list(payload: dto.KeyBrowserListRequest, response: Response, session_data=Depends(csrf_session)):
        response.headers["Cache-Control"] = "no-store"
        return key_browser.list(session_data[0], payload.scope_id, payload.path, payload.offset)

    @router.get("/local/ssh-agent", response_model=dto.AgentMetadata, tags=["local"])
    def ssh_agent(response: Response):
        response.headers["Cache-Control"] = "no-store"
        return agent_metadata()

    @router.get("/credential-deletions", response_model=list[dto.CredentialDeletion], tags=["credentials"])
    def credential_deletions(pending: bool = False):
        return services.credentials.deletions(pending=pending)

    def deletion_response(operation: dict, response: Response):
        response.status_code = 202 if operation["cleanup_pending"] else 200
        response.headers["Cache-Control"] = "no-store"
        # Revocation applies to every browser session, including another tab.
        for session in list(state.sessions.values()):
            session.reveal_grants.pop(operation["credential_id"], None)
        return operation

    @router.post("/credential-deletions/{operation_id}/retry", response_model=dto.CredentialDeletion,
                 responses={202: {"model": dto.CredentialDeletion}}, tags=["credentials"])
    def retry_deletion(operation_id: str, response: Response, _session=Depends(csrf_session)):
        return deletion_response(services.credentials.retry_deletion(operation_id), response)

    @router.get("/credentials/{identifier}", response_model=dto.Credential, tags=["credentials"])
    def credential(identifier: str):
        return services.credentials.get(identifier)

    @router.post("/credentials", response_model=dto.Credential, status_code=201, tags=["credentials"])
    def create_credential(payload: dto.CredentialCreate, _session=Depends(csrf_session)):
        if isinstance(payload, dto.PasswordCreate):
            return services.credentials.create_password(payload.label, payload.secret.get_secret_value())
        if isinstance(payload, dto.KeyCreate):
            return services.credentials.create_key(
                payload.label, payload.key_path,
                payload.passphrase.get_secret_value() if payload.passphrase else None,
                expected_key_revision=expected_revision(_session[0], payload.expected_key_revision, payload.key_path),
            )
        return services.credentials.create_agent(payload.label)

    @router.patch("/credentials/{identifier}", response_model=dto.Credential, tags=["credentials"])
    def update_credential(identifier: str, payload: dto.CredentialUpdate, _session=Depends(csrf_session)):
        changes = payload.model_dump(exclude_unset=True)
        for field in ("secret", "passphrase"):
            value = getattr(payload, field)
            if value is not None:
                changes[field] = value.get_secret_value()
        if payload.expected_key_revision is not None:
            credential = services.credentials.get(identifier)
            changes["expected_key_revision"] = expected_revision(_session[0], payload.expected_key_revision,
                payload.key_path if payload.key_path is not None else credential["key_path"])
        return services.credentials.update(identifier, **changes)

    @router.get("/credentials/{identifier}/deletion-preview", response_model=dto.CredentialDeletionPreview, tags=["credentials"])
    def deletion_preview(identifier: str):
        return services.credentials.deletion_preview(identifier)

    @router.post("/credentials/{identifier}/detach-delete", response_model=dto.CredentialDeletion,
                 responses={202: {"model": dto.CredentialDeletion}}, tags=["credentials"])
    def detach_delete(identifier: str, payload: dto.CredentialDetachDelete, response: Response, _session=Depends(csrf_session)):
        return deletion_response(services.credentials.detach_delete(identifier, **payload.model_dump()), response)

    @router.delete("/credentials/{identifier}", response_model=dto.CredentialDeleteResult,
                 responses={202: {"model": dto.CredentialDeleteResult}}, tags=["credentials"])
    def delete_credential(identifier: str, response: Response, _session=Depends(csrf_session)):
        result = services.credentials.delete(identifier)
        deletion_response(services.credentials.database.get_credential_deletion(result["operation_id"]), response)
        return result

    @router.get("/credentials/{identifier}/tests", response_model=list[dto.CredentialProbeResult], tags=["credentials"])
    def credential_tests(identifier: str):
        return services.credentials.tests(identifier)

    @router.post("/credentials/{identifier}/test", response_class=NDJSONResponse,
                 response_model=dto.CredentialProbeStage | dto.CredentialProbeEvent, tags=["credentials"],
                 responses={200: {"description": "Newline-delimited stage events followed by one result event"}})
    async def credential_test(identifier: str, payload: dto.CredentialProbeRequest, _session=Depends(csrf_session)):
        credential = services.credentials.get(identifier)
        host = services.hosts.get(payload.host_id)
        cancelled = threading.Event()
        events = queue.Queue(maxsize=256)
        sentinel = object()

        def emit(event):
            while not cancelled.is_set():
                try:
                    events.put(event, timeout=0.1)
                    return
                except queue.Full:
                    continue

        def run():
            try:
                # One worker owns the generator and lock throughout its life.
                for event in services.credentials.test_stream(credential["id"], host["id"],
                        timeout=payload.timeout, cancel_event=cancelled):
                    emit(event)
            except Exception:
                result = {"ok": False, "error_code": "probe_unavailable", "message": "Credential test could not complete",
                    "latency_ms": 0, "checked_at": datetime.now(timezone.utc).isoformat(), "host_id": host["id"],
                    "host_alias": host["alias"], "credential_id": credential["id"], "method": None, "stages": []}
                emit({"type": "result", "result": result})
            finally:
                emit(sentinel)

        async def stream():
            worker = threading.Thread(target=run, name="ssm-credential-test", daemon=True)
            worker.start()
            try:
                while True:
                    try:
                        event = await asyncio.to_thread(events.get, True, 0.25)
                    except queue.Empty:
                        continue
                    if event is sentinel:
                        break
                    model = dto.CredentialProbeEvent if event["type"] == "result" else dto.CredentialProbeStage
                    yield json.dumps(model.model_validate(event).model_dump(exclude_none=True)) + "\n"
            finally:
                cancelled.set()
        return StreamingResponse(stream(), media_type="application/x-ndjson", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @router.get("/contexts", response_model=list[dto.Context], tags=["contexts"])
    def contexts():
        return services.contexts.list()

    @router.post("/contexts", response_model=dto.Context, status_code=201, tags=["contexts"])
    def create_context(payload: dto.ContextCreate, _session=Depends(csrf_session)):
        return services.contexts.create(payload.name, server_ids=payload.server_ids)

    # Context names may contain spaces and slashes; clients percent-encode the
    # name as one path segment, and the path converter accepts decoded slashes.
    @router.patch("/contexts/{name:path}", response_model=dto.Context, tags=["contexts"])
    def update_context(name: str, payload: dto.ContextUpdate, _session=Depends(csrf_session)):
        return services.contexts.update(name, new_name=payload.new_name, server_ids=payload.server_ids)

    @router.delete("/contexts/{name:path}", response_model=dto.ContextRemoved, tags=["contexts"])
    def delete_context(name: str, _session=Depends(csrf_session)):
        return services.contexts.delete(name)

    @router.get("/skills", response_model=list[dto.Skill], tags=["skills"])
    def skills(server: str | None = None):
        return services.skills.list(server)

    @router.get("/skills/discover", response_model=dto.SkillDiscovery, tags=["skills"])
    def discover():
        return services.skills.discover()

    @router.post("/skills", response_model=dto.Skill, status_code=201, tags=["skills"])
    def register_skill(payload: dto.SkillRegister, _session=Depends(csrf_session)):
        return services.skills.register(payload.path, server_identifiers=payload.server_ids)

    @router.post("/skills/{identifier}/refresh", response_model=dto.Skill, tags=["skills"])
    def refresh_skill(identifier: str, payload: dto.SkillRefresh | None = None, _session=Depends(csrf_session)):
        return services.skills.refresh(identifier, path=payload.path if payload else None)

    @router.put("/skills/{identifier}/servers", response_model=dto.Skill, tags=["skills"])
    def skill_servers(identifier: str, payload: dto.ServerBindings, _session=Depends(csrf_session)):
        return services.skills.set_servers(identifier, payload.server_ids)

    @router.put("/hosts/{identifier}/skills", response_model=list[dto.Skill], tags=["skills"])
    def host_skills(identifier: str, payload: dto.SkillBindings, _session=Depends(csrf_session)):
        return services.skills.set_server_skills(identifier, payload.skill_ids)

    @router.delete("/skills/{identifier}", response_model=dto.Skill, tags=["skills"])
    def delete_skill(identifier: str, _session=Depends(csrf_session)):
        return services.skills.delete(identifier)

    @router.post("/import/preview", response_model=dto.ImportPreview, tags=["import"])
    def import_preview(payload: dto.ImportRequest | None = None, _session=Depends(csrf_session)):
        return services.imports.preview(config=payload.config if payload else None)

    @router.post("/import/apply", response_model=dto.ImportResult, tags=["import"])
    def import_apply(payload: dto.ImportRequest, _session=Depends(csrf_session)):
        preview = services.imports.preview(config=payload.config)
        return services.imports.apply(preview, overwrite=payload.overwrite)

    @router.post("/auth/master/enroll", response_model=dto.AuthResult, tags=["auth"])
    async def master_enroll(payload: dto.MasterEnroll, session_data=Depends(csrf_session)):
        return await handlers["master_enroll"]({"password": payload.password.get_secret_value()}, session_data)

    @router.post("/auth/master/verify", response_model=dto.AuthResult, tags=["auth"])
    async def master_verify(payload: dto.MasterVerify, session_data=Depends(csrf_session)):
        return await handlers["master_verify"](
            {"password": payload.password.get_secret_value(), "credential_id": payload.credential_id}, session_data,
        )

    # WebAuthn response/options are standard JSON documents supplied by the
    # existing WebAuthn implementation; their algorithm-specific fields vary.
    @router.post("/auth/passkey/register/options", response_model=dict[str, Any], tags=["auth"])
    async def register_options(session_data=Depends(csrf_session)):
        return await handlers["register_options"](session_data)

    @router.post("/auth/passkey/register/verify", response_model=dto.AuthResult, tags=["auth"])
    async def register_verify(payload: dict[str, Any] = Body(...), session_data=Depends(csrf_session)):
        return await handlers["register_verify"](payload, session_data)

    @router.post("/auth/reveal/options", response_model=dict[str, Any], tags=["auth"])
    async def reveal_options(payload: dto.RevealOptions, session_data=Depends(csrf_session)):
        return await handlers["reveal_options"](payload.model_dump(), session_data)

    @router.post("/auth/reveal/verify", response_model=dto.AuthResult, tags=["auth"])
    async def reveal_verify(payload: dto.RevealVerify, session_data=Depends(csrf_session)):
        return await handlers["reveal_verify"](payload.model_dump(), session_data)

    @router.post("/credentials/{identifier}/reveal", response_model=dto.RevealedSecret, tags=["auth"])
    async def reveal(identifier: str, session_data=Depends(csrf_session)):
        # Reuse the same grant store as legacy; consuming through either route
        # makes the grant unavailable to the other route.
        response = await handlers["reveal"](identifier, session_data)
        return dto.RevealedSecret.model_validate_json(response.body)

    return router
