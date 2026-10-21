"""Versioned HTTP DTOs. Output models deliberately allow only named fields."""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


# Matches the runner's per-directory probe cap; a listing never exceeds it.
MAX_ENRICHMENT_NAMES = 5000


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class GeoInput(RequestModel):
    country_code: str = Field(pattern=r"^[A-Za-z]{2}$")
    city: str | None = Field(default=None, max_length=120)


class GeoLocation(BaseModel):
    country_code: str
    city: str | None = None
    source: Literal["manual", "geoip"]


class HostFacts(BaseModel):
    observed_at: str
    os_logo: Literal["debian", "ubuntu", "rocky", "alpine", "rhel", "fedora", "arch", "macos", "freebsd", "windows", "unknown"] = "unknown"
    hostname: str | None = None
    os: str | None = None
    os_pretty: str | None = None
    os_id: str | None = None
    os_version: str | None = None
    os_family: str | None = None
    os_like: str | None = None
    kernel: str | None = None
    arch: str | None = None
    cpu_cores: str | None = None
    cpu_model: str | None = None
    memory: str | None = None
    disk: str | None = None
    uptime: str | None = None
    load: str | None = None
    package_manager: str | None = None


class HostKey(BaseModel):
    observed_at: str
    fingerprint: str | None = None
    state: Literal["verified", "changed", "revoked", "unknown"] = "unknown"


class RouteHop(BaseModel):
    alias: str
    external: bool
    id: str | None = None
    hostname: str | None = None
    port: int | None = None
    last_test_at: str | None = None
    last_test_status: str | None = None
    last_test_error_code: str | None = None
    last_success_at: str | None = None


class HostRoute(BaseModel):
    alias: str
    hops: list[RouteHop]
    blocked_by: str | None = None


class HostCreate(RequestModel):
    alias: str
    hostname: str
    username: str
    port: int = Field(default=22, ge=1, le=65535)
    credential_id: str | None = None
    proxy_jumps: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    notes: str = ""
    geo: GeoInput | None = None


class HostUpdate(RequestModel):
    alias: str | None = None
    hostname: str | None = None
    username: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    credential_id: str | None = None
    proxy_jumps: list[str] | None = None
    tags: list[str] | None = None
    notes: str | None = None
    geo: GeoInput | None = None

    @model_validator(mode="after")
    def reject_null_changes(self):
        if any(getattr(self, field) is None for field in self.model_fields_set - {"credential_id", "geo"}):
            raise ValueError("only credential_id and geo may be null; omit other fields to leave them unchanged")
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
    facts: HostFacts | None = None
    geo: GeoLocation | None = None
    address_scope: Literal["public", "private", "lan", "loopback", "unresolved"] = "unresolved"
    last_success_at: str | None = None
    last_resolved_ip: str | None = None
    host_key: HostKey | None = None
    proxy_jump_details: list[RouteHop] = Field(default_factory=list)
    jump_for_count: int = 0


class KeyPermissions(BaseModel):
    method: Literal["posix_mode", "windows_acl"]
    assessment: Literal["secure", "unsafe", "unknown"]
    mode: str | None = None


class KeyMetadata(BaseModel):
    path: str
    status: Literal["valid", "passphrase_required", "unverified"]
    format: Literal["openssh", "pem"] | None = None
    encrypted: bool | None = None
    algorithm: str | None = None
    fingerprint: str | None = None
    bits: int | None = None
    permissions: KeyPermissions
    observed_at: str


class CredentialProbeStage(BaseModel):
    type: Literal["stage"] = "stage"
    stage: Literal["prepare", "authenticate", "hop"]
    status: Literal["running", "ok", "failed"]
    message: str
    hop_index: int | None = None
    alias: str | None = None
    method: str | None = None
    error_code: str | None = None


class CredentialProbeResult(BaseModel):
    ok: bool
    error_code: str | None = None
    message: str
    latency_ms: int
    checked_at: str
    host_id: str
    host_alias: str
    credential_id: str
    credential_kind: Literal["password", "key", "agent"] | None = None
    credential_revision: int | None = None
    method: Literal["password", "publickey", "agent", "none"] | None = None
    stages: list[CredentialProbeStage] = Field(default_factory=list)


class CredentialProbeEvent(BaseModel):
    type: Literal["result"]
    result: CredentialProbeResult


class CredentialProbeRequest(RequestModel):
    host_id: str
    timeout: int = Field(default=8, ge=1, le=120)


class Credential(BaseModel):
    id: str
    label: str
    kind: Literal["password", "key", "agent"]
    key_path: str | None
    has_secret: bool
    has_passphrase: bool
    created_at: str
    updated_at: str
    revision: int = 1
    key_metadata: KeyMetadata | None = None
    last_test: CredentialProbeResult | None = None


class PasswordCreate(RequestModel):
    kind: Literal["password"]
    label: str
    secret: SecretStr


class KeyCreate(RequestModel):
    kind: Literal["key"]
    label: str
    key_path: str
    passphrase: SecretStr | None = None
    expected_key_revision: str | None = None


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
    expected_key_revision: str | None = None
    expected_updated_at: str | None = None

    @model_validator(mode="after")
    def reject_null_metadata(self):
        if any(getattr(self, field) is None for field in self.model_fields_set & {"label", "key_path", "expected_key_revision", "expected_updated_at"}):
            raise ValueError("omit metadata fields to leave them unchanged")
        if self.clear_passphrase and self.passphrase and self.passphrase.get_secret_value():
            raise ValueError("choose replacement or clearing of the passphrase")
        return self


class KeyInspectRequest(RequestModel):
    key_path: str


class KeyValidateRequest(KeyInspectRequest):
    passphrase: SecretStr | None = None
    credential_id: str | None = None
    use_stored_passphrase: bool = False
    expected_key_revision: str | None = None


class KeyInspection(BaseModel):
    key_metadata: KeyMetadata
    key_revision: str


class KeyBrowserScopeRequest(RequestModel):
    root_path: str | None = None


class KeyBrowserScope(BaseModel):
    scope_id: str
    root_path: str
    expires_at: str


class KeyBrowserListRequest(RequestModel):
    scope_id: str
    path: str | None = None
    offset: int = Field(default=0, ge=0, le=10000)


class KeyBrowserEntry(BaseModel):
    name: str
    path: str
    kind: Literal["file", "directory"]


class KeyBrowserListing(BaseModel):
    scope_id: str
    root_path: str
    path: str
    parent: str | None = None
    entries: list[KeyBrowserEntry]
    has_more: bool
    next_offset: int | None = None
    truncated: bool = False


class AgentIdentity(BaseModel):
    bits: int
    fingerprint: str
    algorithm: str
    comment: str


class AgentMetadata(BaseModel):
    status: Literal["unavailable", "timeout", "empty", "ready", "unknown"]
    identities: list[AgentIdentity]
    observed_at: str
    truncated: bool = False


class CredentialDeletionPreview(BaseModel):
    credential: Credential
    expected_updated_at: str
    expected_host_ids: list[str]
    hosts: list["HostReference"]


class CredentialDetachDelete(RequestModel):
    operation_id: str = Field(pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
    expected_updated_at: str
    expected_host_ids: list[str]


class CredentialDeletion(BaseModel):
    operation_id: str
    credential_id: str
    credential: Credential
    detached_hosts: list["HostReference"]
    committed: Literal[True] = True
    cleanup_pending: bool
    status: Literal["pending", "complete"]
    remaining_slots: list[Literal["password", "passphrase", "config"]]
    attempts: int
    error_code: str | None
    created_at: str
    updated_at: str


class CredentialDeleteResult(Credential):
    operation_id: str
    committed: Literal[True] = True
    cleanup_pending: bool


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


class BulkProbeRequest(ProbeRequest):
    identifiers: list[str] = Field(min_length=1, max_length=64)
    concurrency: int = Field(default=4, ge=1, le=4)


class TestResult(BaseModel):
    alias: str
    ok: bool
    latency_ms: int
    error_code: str | None
    message: str


class BulkTestResult(BaseModel):
    results: list[TestResult]


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
    uid: int | None = None
    gid: int | None = None
    modified_at: str | None = None
    link_target: str | None = None


class DirectoryListing(BaseModel):
    alias: str
    path: str
    parent: str | None
    entries: list[DirectoryEntry]
    total: int
    matching_total: int | None = None
    truncated: bool
    unparsed: int
    latency_ms: int
    connection_checked_at: str
    # The optional precise-metadata probe is a separate request, so a web
    # listing reports it as skipped; CLI listings still report both phases.
    enrichment_ms: int | None = None
    total_ms: int | None = None
    # True when the listing already carries precise metadata (the one-exec
    # route), so the UI must not request the optional probe for it.
    enriched: bool = False


class DirectoryEnrichmentClaim(RequestModel):
    """The SFTP facts the caller is enriching, so the server can verify a match.

    The probe runs in a shell namespace that can differ from the SFTP one (a
    chroot, for example). Sending back what SFTP reported lets the server keep
    the same refusal it applied when both phases ran in one request, instead of
    trusting the caller to compare them correctly.
    """

    name: str = Field(min_length=1)
    type: Literal["file", "directory", "symlink", "special"]
    size: int = Field(ge=0)
    uid: int | None = Field(default=None, ge=0)
    gid: int | None = Field(default=None, ge=0)


class DirectoryEnrichmentRequest(RequestModel):
    path: str = Field(min_length=1)
    entries: list[DirectoryEnrichmentClaim] = Field(default_factory=list, max_length=MAX_ENRICHMENT_NAMES)


class DirectoryEnrichmentEntry(BaseModel):
    """Only the fields the probe may contribute; size/type stay SFTP's."""

    uid: int | None = None
    gid: int | None = None
    modified_at: str | None = None
    link_target: str | None = None
    owner: str | None = None
    group: str | None = None


class DirectoryEnrichment(BaseModel):
    path: str
    entries: dict[str, DirectoryEnrichmentEntry]


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
    bulk_checks: bool = True
    max_bulk_hosts: int = 64
    max_check_concurrency: int = 4
    host_routes: bool = True
    file_conflicts: bool = True
    credential_probe: bool = True
    credential_test_history: bool = True
    credential_detach_delete: bool = True
    private_key_validation: bool = True
    local_key_browser: bool = True
    ssh_agent_identities: bool = True


class RuntimeSession(BaseModel):
    expires_at: str


class RuntimeVault(BaseModel):
    name: str | None = None
    status: Literal["ready", "unavailable", "unknown"] = "unknown"


class RuntimeInfo(BaseModel):
    api_version: Literal["1"] = "1"
    version: str
    shell: Literal["web"] = "web"
    ui: Literal["modern"] = "modern"
    csrf: str
    capabilities: Capabilities
    auth: AuthStatus
    session: RuntimeSession
    server_time: str
    vault: RuntimeVault
    # Set when this process cannot write the host database or the managed SSH
    # config, so host actions would fail; the UI explains it instead of
    # retrying every view.
    config_problem: str | None = None


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


class FileConflictEntry(BaseModel):
    name: str
    path: str
    type: Literal["file", "directory", "symlink", "special"]
    size: int
    modified: str | None = None
    uid: int | None = None
    gid: int | None = None
    owner: str | None = None
    group: str | None = None
    modified_at: str | None = None
    link_target: str | None = None


class ErrorResponse(BaseModel):
    ok: Literal[False] = False
    error: str
    message: str
    error_code: Literal["permission_denied", "not_found", "not_a_directory", "unreachable", "auth_failed", "timeout", "too_large", "exists", "unsupported"] | None = None
    existing: FileConflictEntry | None = None


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


class ImportWarning(ImportIssue):
    code: Literal["authentication-not-imported"]
    source_config: str
    identity_files: list[str]
    fallback_config: str | None


class ImportPreview(BaseModel):
    config: str
    items: list[ImportItem]
    skipped_patterns: list[str]
    errors: list[ImportIssue]
    warnings: list[ImportWarning] = Field(default_factory=list)


class ImportResult(BaseModel):
    added: list[str]
    updated: list[str]
    unchanged: list[str]
    skipped: list[str]
    errors: list[ImportIssue]
    warnings: list[ImportWarning] = Field(default_factory=list)
