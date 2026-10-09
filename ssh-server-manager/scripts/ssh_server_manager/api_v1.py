"""Typed transport adapter; all use cases go through ApplicationServices."""
from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Body, Depends, File, Form, Response, UploadFile

from . import __version__
from . import api_models as dto
from .application import ApplicationServices


def build_router(
    services: ApplicationServices,
    *,
    state: Any,
    browser_session: Callable,
    csrf_session: Callable,
    handlers: dict[str, Callable],
    transfer_limit: int,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1",
        dependencies=[Depends(browser_session)],
        responses={code: {"model": dto.ErrorResponse} for code in (400, 401, 403, 404, 409, 422, 502)},
    )

    @router.get("/runtime", response_model=dto.RuntimeInfo, tags=["runtime"])
    def runtime(session_data=Depends(browser_session)):
        return {
            "version": __version__,
            "csrf": session_data[1].csrf,
            "auth": state.auth.status(),
            "capabilities": {"max_web_transfer_bytes": transfer_limit},
        }

    @router.get("/hosts", response_model=list[dto.Host], tags=["hosts"])
    def hosts(query: str | None = None, tag: str | None = None):
        return services.hosts.list(query=query, tag=tag)

    @router.get("/hosts/{identifier}", response_model=dto.Host, tags=["hosts"])
    def host(identifier: str):
        return services.hosts.get(identifier)

    @router.get("/hosts/{identifier}/workspace", response_model=dto.Workspace, tags=["hosts"])
    def workspace(identifier: str):
        return services.hosts.workspace(identifier)

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

    @router.post("/hosts/{identifier}/diagnose", response_model=dto.Diagnosis, tags=["diagnostics"])
    def diagnose(identifier: str, payload: dto.ProbeRequest | None = None, _session=Depends(csrf_session)):
        return services.diagnostics.diagnose(identifier, timeout=payload.timeout if payload else 8)

    @router.get("/hosts/{identifier}/files", response_model=dto.DirectoryListing, tags=["files"])
    def files(identifier: str, path: str | None = None):
        return services.files.list_directory(identifier, path)

    @router.post(
        "/hosts/{identifier}/download", response_class=Response, tags=["files"],
        responses={200: {"content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}}}},
    )
    async def download(identifier: str, payload: dto.DownloadRequest, session_data=Depends(csrf_session)):
        return await handlers["download"](identifier, payload.model_dump(), session_data)

    @router.post("/hosts/{identifier}/upload", response_model=dto.TransferResult, tags=["files"])
    async def upload(
        identifier: str, file: UploadFile = File(...), path: str = Form(...),
        session_data=Depends(csrf_session),
    ):
        return await handlers["upload"](identifier, file, path, session_data)

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
            )
        return services.credentials.create_agent(payload.label)

    @router.patch("/credentials/{identifier}", response_model=dto.Credential, tags=["credentials"])
    def update_credential(identifier: str, payload: dto.CredentialUpdate, _session=Depends(csrf_session)):
        changes = payload.model_dump(exclude_unset=True)
        for field in ("secret", "passphrase"):
            value = getattr(payload, field)
            if value is not None:
                changes[field] = value.get_secret_value()
        return services.credentials.update(identifier, **changes)

    @router.delete("/credentials/{identifier}", response_model=dto.Credential, tags=["credentials"])
    def delete_credential(identifier: str, _session=Depends(csrf_session)):
        return services.credentials.delete(identifier)

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
