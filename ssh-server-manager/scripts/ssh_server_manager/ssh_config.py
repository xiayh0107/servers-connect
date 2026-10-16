from __future__ import annotations

import glob
import ntpath
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

from .db import Database
from .paths import managed_ssh_config_path, original_ssh_config_path


class SSHConfigError(RuntimeError):
    pass


def quote_value(value: str) -> str:
    if not value or any(ch.isspace() or ch in {'"', "'", "#", "\\"} for ch in value):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


def split_config_line(line: str) -> list[str]:
    """Tokenize one OpenSSH config line.

    On Windows a backslash is a path separator, not an escape character, so
    it is preserved before POSIX-style quote handling is applied.
    """
    if os.name == "nt":
        line = line.replace("\\", "\\\\")
    try:
        return shlex.split(line, comments=True, posix=True)
    except ValueError:
        return []


def _parse_include_tokens(line: str) -> list[str]:
    directive = re.match(r"^\s*include(?:\s*=\s*|\s+)", line, re.IGNORECASE)
    if directive is None:
        return []
    line = line[directive.end():]
    # OpenSSH only consumes backslash before a quote or another backslash.
    # POSIX shlex consumes more escapes; preserving every backslash on Windows
    # instead keeps the quoting layer of generated Include patterns intact.
    tokens: list[str] = []
    token: list[str] = []
    quote: str | None = None
    position = 0
    while position < len(line):
        char = line[position]
        if char == "\\" and position + 1 < len(line) and line[position + 1] in {"\\", "'", '"'}:
            position += 1
            token.append(line[position])
        elif char in {"'", '"'}:
            if quote == char:
                quote = None
            elif quote is None:
                quote = char
            else:
                token.append(char)
        elif quote is None and char == "#":
            break
        elif quote is None and char.isspace():
            if token:
                tokens.append("".join(token))
                token = []
        else:
            token.append(char)
        position += 1
    if quote is not None:
        return []
    if token:
        tokens.append("".join(token))
    return tokens


def _cygpath_for_client(ssh_binary: str | None) -> Path | None:
    """Use the selected MSYS/Cygwin client's own filesystem conversion."""
    selected = ssh_binary or shutil.which("ssh") or "ssh"
    executable = Path(shutil.which(selected) or selected).resolve()
    directory = executable.parent
    if not any((directory / name).is_file() for name in ("msys-2.0.dll", "cygwin1.dll")):
        return None
    converter = directory / "cygpath.exe"
    if not converter.is_file():
        raise SSHConfigError("this MSYS/Cygwin OpenSSH client needs its matching cygpath.exe for Include paths")
    return converter


class _IncludePaths:
    def __init__(self, ssh_binary: str | None = None, *, windows: bool | None = None):
        self.windows = os.name == "nt" if windows is None else windows
        self.ssh_binary = ssh_binary or shutil.which("ssh") or "ssh"
        self.cygpath = _cygpath_for_client(self.ssh_binary) if self.windows else None
        self._converted: dict[tuple[str, str], str] = {}
        self._expands_tokens: bool | None = None

    def _include_expands_tokens(self) -> bool:
        if self._expands_tokens is None:
            try:
                result = subprocess.run(
                    [self.ssh_binary, "-V"], stdin=subprocess.DEVNULL,
                    capture_output=True, encoding="utf-8", errors="strict", timeout=5,
                )
            except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
                raise SSHConfigError("could not determine this OpenSSH client's Include token support") from exc
            version = re.search(
                r"(?m)^OpenSSH(?:_for_Windows)?_(\d+)\.(\d+)(?:p\d+)?(?:,|\s|$)",
                result.stdout + "\n" + result.stderr,
            )
            if result.returncode or version is None:
                raise SSHConfigError("could not determine this OpenSSH client's Include token support")
            # OpenSSH 9.9 introduced percent-token and environment expansion
            # in Include. Older clients interpret a percent sign literally.
            self._expands_tokens = tuple(map(int, version.groups())) >= (9, 9)
        return self._expands_tokens

    def _convert(self, value: str, direction: str) -> str:
        assert self.cygpath is not None
        key = (direction, value)
        if key not in self._converted:
            # cygpath's Unix output and input use its locale; Windows output
            # also accepts an explicit codepage. Keep both UTF-8 without
            # changing the calling process or the SSH client's environment.
            environment = os.environ.copy()
            environment["LC_ALL"] = "C.UTF-8"
            command = [str(self.cygpath), direction]
            if direction == "-w":
                command.extend(["-C", "UTF8"])
            try:
                result = subprocess.run(
                    [*command, "--", value], stdin=subprocess.DEVNULL,
                    capture_output=True, encoding="utf-8", errors="strict", timeout=5,
                    env=environment,
                )
            except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
                raise SSHConfigError("could not convert the SSH Include path for this OpenSSH client") from exc
            converted = result.stdout.rstrip("\r\n")
            absolute = converted.startswith("/") if direction == "-u" else ntpath.isabs(converted)
            if result.returncode or not converted or not absolute or any(not char.isprintable() for char in converted):
                raise SSHConfigError("could not convert the SSH Include path for this OpenSSH client")
            self._converted[key] = converted
        return self._converted[key]

    def format(self, path: Path) -> str:
        value = self._convert(str(path), "-u") if self.cygpath else path.as_posix()
        if any(not char.isprintable() for char in value):
            raise SSHConfigError("SSH Include source must have a printable literal path")
        # Include expands tokens from OpenSSH 9.9 onward, then glob patterns.
        # Protect only the layers used by this client before config quoting.
        if "%" in value and self._include_expands_tokens():
            value = value.replace("%", "%%")
        # Native Windows OpenSSH normalizes backslashes into separators
        # before globbing. Single-character classes protect literal glob
        # characters without depending on backslash escape support.
        value = "".join(
            "[" + char + "]" if char in "$*?[" else "\\\\" if char == "\\" else char
            for char in value
        )
        return quote_value(value)

    def local(self, value: str) -> Path:
        if "%%" in value and self._include_expands_tokens():
            value = value.replace("%%", "%")
        if self.cygpath and value.startswith("/"):
            value = self._convert(value, "-w")
        elif value.startswith("~"):
            if self.cygpath and value.startswith("~/") and os.environ.get("HOME"):
                home = os.environ["HOME"]
                home = self._convert(home, "-w") if home.startswith("/") else home
                value = str(Path(home) / value[2:])
            else:
                value = os.path.expanduser(value)
        return Path(value)


def _expand_include(pattern: str, paths: _IncludePaths) -> list[Path]:
    # Python glob uses bracket escapes rather than OpenSSH's backslash escapes.
    # Keep a literal candidate too: native Windows paths may use backslashes
    # as separators in existing hand-written configuration.
    candidates = [paths.local(pattern)]
    # A generated managed destination may not exist yet, so glob alone cannot
    # discover a cycle through a literal single-character class. Decode only
    # these exact singleton forms; retain the original glob candidate too.
    literal = re.sub(r"\[(\[|\*|\?|\$)\]", lambda match: match.group(1), pattern)
    candidates.append(paths.local(literal))
    escaped: list[str] = []
    position = 0
    while position < len(pattern):
        if pattern[position] == "\\" and position + 1 < len(pattern) and pattern[position + 1] in "\\*?[":
            position += 1
            escaped.append(glob.escape(pattern[position]))
        else:
            escaped.append(pattern[position])
        position += 1
    candidates.append(paths.local("".join(escaped)))
    base: Path | None = None
    included: set[Path] = set()
    for candidate in candidates:
        absolute = candidate.is_absolute()
        if paths.cygpath:
            # MSYS OpenSSH's path_absolute only accepts leading '/'. A drive
            # spelling in an existing Include is relative to ~/.ssh, even
            # though Python's WindowsPath considers it absolute.
            absolute = pattern.startswith(("/", "~"))
        if absolute:
            anchored = [candidate]
        else:
            if base is None:
                base = paths.local(os.environ["HOME"]) / ".ssh" if paths.cygpath and os.environ.get("HOME") else Path.home() / ".ssh"
            anchored = [Path(str(base) + "/" + str(candidate))]
        for path in anchored:
            try:
                included.add(path.resolve())
            except OSError:
                continue
            included.update(Path(item).resolve() for item in glob.glob(str(path)))
    return sorted(included)


def includes_target(
    config: Path, target: Path, seen: set[Path] | None = None, *,
    ssh_binary: str | None = None, _paths: _IncludePaths | None = None,
) -> bool:
    paths = _paths or _IncludePaths(ssh_binary)
    config = config.expanduser().resolve()
    target = target.expanduser().resolve()
    if not config.exists():
        return False
    seen = seen or set()
    if config in seen:
        return False
    seen.add(config)
    try:
        lines = config.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return False
    for line in lines:
        for token in _parse_include_tokens(line):
            for include in _expand_include(token, paths):
                if include == target or includes_target(include, target, seen, _paths=paths):
                    return True
    return False


def _validate_include_source(source: Path, destination: Path, paths: _IncludePaths) -> str:
    formatted = paths.format(source)
    if source == destination or includes_target(source, destination, _paths=paths):
        raise SSHConfigError(
            f"refusing to create an Include cycle: {source} includes the managed config {destination}"
        )
    return formatted


def validate_include_source(
    source: str | Path, *, destination: str | Path | None = None,
    ssh_binary: str | None = None,
) -> Path:
    """Validate an import source before committing its host metadata."""
    source_path = Path(source).expanduser().resolve()
    destination_path = Path(destination).expanduser().resolve() if destination else managed_ssh_config_path().resolve()
    _validate_include_source(source_path, destination_path, _IncludePaths(ssh_binary))
    return source_path


def _render_server(server: dict, credential: dict | None) -> list[str]:
    lines = [
        f"Host {server['alias']}",
        f"  HostName {quote_value(server['hostname'])}",
        f"  Port {server['port']}",
        f"  User {quote_value(server['username'])}",
    ]
    jumps = server["proxy_jumps"]
    lines.append(f"  ProxyJump {','.join(jumps) if jumps else 'none'}")
    if credential and credential["kind"] == "password":
        lines.extend(
            [
                "  IdentityFile none",
                "  IdentitiesOnly yes",
                "  PubkeyAuthentication no",
                "  PasswordAuthentication yes",
                "  KbdInteractiveAuthentication yes",
                "  PreferredAuthentications keyboard-interactive,password",
            ]
        )
    elif credential and credential["kind"] == "key":
        lines.extend(
            [
                f"  IdentityFile {quote_value(credential['key_path'])}",
                "  IdentitiesOnly yes",
                "  PubkeyAuthentication yes",
                "  PreferredAuthentications publickey",
            ]
        )
    lines.append("")
    return lines


def render_config(
    database: Database,
    *,
    destination: str | Path | None = None,
    original: str | Path | None = None,
    ssh_binary: str | None = None,
) -> Path:
    # Serialize the snapshot and replacement with mutations and other renders,
    # including CLI processes. Atomic replace alone cannot stop an older
    # snapshot from overwriting a newer configuration after a concurrent edit.
    with database.transaction():
        return _render_config(database, destination=destination, original=original, ssh_binary=ssh_binary)


def _render_config(
    database: Database,
    *,
    destination: str | Path | None = None,
    original: str | Path | None = None,
    ssh_binary: str | None = None,
) -> Path:
    destination_path = Path(destination) if destination else managed_ssh_config_path()
    original_path = Path(original) if original else original_ssh_config_path()
    destination_path = destination_path.expanduser().resolve()
    original_path = original_path.expanduser().resolve()
    paths = _IncludePaths(ssh_binary)
    servers = database.list_servers()
    registered = database.get_ssh_config_sources()
    sources = {
        server["id"]: Path(registered[server["id"]]).expanduser().resolve()
        for server in servers if server["id"] in registered
    }
    formatted_sources = {
        source: _validate_include_source(source, destination_path, paths)
        for source in set(sources.values()) | ({original_path} if original_path != destination_path else set())
    }
    lines = [
        "# Generated by ssh-server-manager. Do not edit this file directly.",
        "# Edit hosts with serverctl or the local UI.",
        "",
    ]
    credentials = {item["id"]: item for item in database.list_credentials()}
    custom_aliases: list[str] = []
    for server in servers:
        lines.extend(_render_server(server, credentials.get(server["credential_id"]))[:-1])
        source = sources.get(server["id"])
        if source and source != original_path:
            custom_aliases.append(server["alias"])
            lines.append(f"  Include {formatted_sources[source]}")
        lines.append("")
    if original_path.exists() and original_path != destination_path:
        # IdentityFile accumulates. A custom source must not inherit another
        # source's identities even though endpoint directives are first-wins.
        condition = "Host *" + "".join(f" !{alias}" for alias in custom_aliases)
        lines.extend([condition, f"  Include {formatted_sources[original_path]}", ""])
    condition = "Host *" + "".join(f" !{server['alias']}" for server in servers)
    for source in sorted(set(sources.values()) - {original_path}):
        # Imported configs also define external jumps, but must not contribute
        # authentication to any managed host belonging to another source.
        lines.extend([condition, f"  Include {formatted_sources[source]}", ""])
    content = "\n".join(lines)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        destination_path.parent.chmod(stat.S_IRWXU)
    file_descriptor, temporary = tempfile.mkstemp(prefix=".ssh-server-manager-", dir=destination_path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            temporary_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temporary_path, destination_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return destination_path


def effective_config_args(database: Database) -> list[str]:
    return ["-F", str(render_config(database))]

