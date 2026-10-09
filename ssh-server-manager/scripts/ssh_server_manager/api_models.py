"""Versioned HTTP DTOs. Output models deliberately allow only named fields."""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class HostCreate(RequestModel):
    alias: str
    hostname: str
    username: str
    port: int = Field(default=22, ge=1, le=65535)
    credential_id: str | None = None
    proxy_jumps: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    notes: str = ""


class HostUpdate(RequestModel):
    alias: str | None = None
    hostname: str | None = None
    username: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    credential_id: str | None = None
    proxy_jumps: list[str] | None = None
    tags: list[str] | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def reject_null_changes(self):
        if any(getattr(self, field) is None for field in self.model_fields_set - {"credential_id"}):
            raise ValueError("only credential_id may be null; omit fields to leave them unchanged")
        return self


class Host(BaseModel):
    id: str
    alias: str
    hostname: str
    username: str
    port: int
    credential_id: str | None
    credential_label: str | None
    credential_kind: str | None
    proxy_jumps: list[str]
    tags: list[str]
    notes: str
    source: str
    last_test_at: str | None
    last_test_status: str | None
    last_test_latency_ms: int | None
    last_test_error_code: str | None
    last_test_message: str | None
    created_at: str
    updated_at: str


class Credential(BaseModel):
    id: str
    label: str
    kind: Literal["password", "key", "agent"]
    key_path: str | None
    has_secret: bool
    has_passphrase: bool
    created_at: str
    updated_at: str


class PasswordCreate(RequestModel):
    kind: Literal["password"]
    label: str
    secret: SecretStr


class KeyCreate(RequestModel):
    kind: Literal["key"]
    label: str
    key_path: str
    passphrase: SecretStr | None = None


class AgentCreate(RequestModel):
    kind: Literal["agent"]
    label: str


CredentialCreate = Annotated[PasswordCreate | KeyCreate | AgentCreate, Field(discriminator="kind")]


class CredentialUpdate(RequestModel):
    label: str | None = None
    key_path: str | None = None
    secret: SecretStr | None = None
    passphrase: SecretStr | None = None
    clear_passphrase: bool = False


class PathCreate(RequestModel):
    label: str
    path: str
    notes: str = ""


class PathUpdate(RequestModel):
    label: str | None = None
    path: str | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def reject_null_changes(self):
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("omit fields to leave them unchanged")
        return self


class SavedPath(BaseModel):
    id: str
    server: str
    label: str
    path: str
    notes: str
    created_at: str
    updated_at: str
    last_used_at: str | None


class PathsReplace(RequestModel):
    paths: list[PathCreate]


class HostReference(BaseModel):
    id: str
    alias: str


class Skill(BaseModel):
    id: str
    name: str
    path: str
    description: str
    servers: list[HostReference]
    created_at: str
    updated_at: str
    status: str | None = None
    status_message: str | None = None


class SkillRegister(RequestModel):
    path: str
    server_ids: list[str] = Field(default_factory=list)


class SkillRefresh(RequestModel):
    path: str | None = None


class ServerBindings(RequestModel):
    server_ids: list[str]


class SkillBindings(RequestModel):
    skill_ids: list[str]


class SkillCandidate(BaseModel):
    path: str
    status: str
    name: str | None = None
    description: str | None = None
    registered_id: str | None = None
    error: str | None = None


class SkillConflict(BaseModel):
    type: Literal["name", "path"]
    name: str | None = None
    path: str | None = None
    paths: list[str] | None = None
    names: list[str] | None = None
    registered_id: str | None = None


class SkillDiscovery(BaseModel):
    candidates: list[SkillCandidate]
    conflicts: list[SkillConflict]


class Workspace(BaseModel):
    host: Host
    credential: Credential | None
    paths: list[SavedPath]
    skills: list[Skill]


class NotesUpdate(RequestModel):
    mode: Literal["append", "set", "clear"] = "append"
    text: str = ""

    @model_validator(mode="after")
    def require_append_text(self):
        if self.mode == "append" and "text" not in self.model_fields_set:
            raise ValueError("note text is required when appending")
        return self


class ProbeRequest(RequestModel):
    timeout: int = Field(default=8, ge=1, le=120)


class TestResult(BaseModel):
    alias: str
    ok: bool
    latency_ms: int
    error_code: str | None
    message: str


class DiagnosticCheck(BaseModel):
    id: str
    label: str
    status: Literal["ok", "failed", "warning", "skipped"]
    message: str
    latency_ms: int | None = None
    error_code: str | None = None
    details: dict[str, Any] | None = None


class DiagnosticSummary(BaseModel):
    total: int
    passed: int
    failed: int
    warnings: int
    skipped: int


class Diagnosis(BaseModel):
    alias: str
    checked_at: str
    overall: Literal["ok", "warning", "failed"]
    summary: DiagnosticSummary
    checks: list[DiagnosticCheck]


class DirectoryEntry(BaseModel):
    name: str
    path: str
    type: Literal["file", "directory", "symlink", "special"]
    size: int
    modified: str
    permissions: str
    owner: str
    group: str
    hidden: bool


class DirectoryListing(BaseModel):
    alias: str
    path: str
    parent: str | None
    entries: list[DirectoryEntry]
    total: int
    truncated: bool
    unparsed: int
    latency_ms: int
    connection_checked_at: str


class DownloadRequest(RequestModel):
    path: str = Field(min_length=1)


class TransferResult(BaseModel):
    alias: str
    direction: Literal["upload", "download"]
    remote_path: str
    bytes: int
    latency_ms: int


class Context(BaseModel):
    name: str
    count: int


class ContextCreate(RequestModel):
    name: str
    server_ids: list[str] | None = None


class ContextUpdate(RequestModel):
    new_name: str | None = None
    server_ids: list[str] | None = None


class ContextRemoved(BaseModel):
    name: str
    removed_from: int


class AuthStatus(BaseModel):
    passkeys: int
    master_password_enrolled: bool
    webauthn_available: bool


class Capabilities(BaseModel):
    hosts: bool = True
    files: bool = True
    credentials: bool = True
    skills: bool = True
    native_window: bool = False
    native_dialogs: bool = False
    max_web_transfer_bytes: int


class RuntimeInfo(BaseModel):
    api_version: Literal["1"] = "1"
    version: str
    shell: Literal["web"] = "web"
    ui: Literal["legacy"] = "legacy"
    csrf: str
    capabilities: Capabilities
    auth: AuthStatus


class MasterEnroll(RequestModel):
    password: SecretStr


class MasterVerify(MasterEnroll):
    credential_id: str


class RevealOptions(RequestModel):
    credential_id: str


class RevealVerify(RevealOptions):
    response: dict[str, Any]


class AuthResult(BaseModel):
    ok: bool
    credential_id: str | None = None


class RevealedSecret(BaseModel):
    credential_id: str
    slot: Literal["password", "passphrase"]
    value: str


class ErrorResponse(BaseModel):
    ok: Literal[False] = False
    error: str
    message: str


class ImportRequest(RequestModel):
    config: str | None = None
    overwrite: bool = False


class ImportCandidate(HostCreate):
    source: str


class ImportItem(BaseModel):
    action: Literal["add", "unchanged", "conflict"]
    server: ImportCandidate


class ImportIssue(BaseModel):
    alias: str
    message: str


class ImportPreview(BaseModel):
    config: str
    items: list[ImportItem]
    skipped_patterns: list[str]
    errors: list[ImportIssue]


class ImportResult(BaseModel):
    added: list[str]
    updated: list[str]
    unchanged: list[str]
    skipped: list[str]
    errors: list[ImportIssue]
