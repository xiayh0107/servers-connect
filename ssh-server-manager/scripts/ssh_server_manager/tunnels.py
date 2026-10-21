"""Saved port-forwarding profiles, opened as background ssh processes.

The profile itself is inventory (it lives in the database, and is what an agent
reads to learn that a host has a tunnel to a database). The *running* tunnel is
not inventory: it is a process on this machine, it dies with a reboot, and a
database copied to another computer must never claim it is up. So runtime state
lives in a mode-600 JSON file in the private runtime directory and is pruned the
moment a recorded process is gone.

Liveness is checked twice over: the pid must exist *and* the process's command
line must still carry this tunnel's unique ControlPath marker. Without the second
check a recycled pid would make a dead tunnel look alive, and "close" would kill
an unrelated process.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from . import audit
from .db import Database, NotFoundError
from .paths import ensure_private_dir, runtime_dir
from .ssh_runner import SSHError, SSHRunner, redact
from .validation import ValidationError, format_forward_spec

STATE_FILE = "tunnels.json"
LOCK_FILE = "tunnels.lock"
LOG_DIRECTORY = "tunnels"
# How long to watch a just-started ssh before calling the tunnel open. ssh forks
# into the background only after authentication, so a fast exit here is a real
# failure (unknown host key, refused forward, bad credential) and worth reporting
# with its own message instead of a bare pid.
STARTUP_GRACE_SECONDS = 3.0
TERMINATE_GRACE_SECONDS = 5.0


def _is_windows() -> bool:
    return os.name == "nt"


def _forward_flag(kind: str) -> str:
    return {"local": "-L", "remote": "-R", "dynamic": "-D"}[kind]


def _state_path():
    return runtime_dir() / STATE_FILE


def _lock_path():
    return runtime_dir() / LOCK_FILE


def tunnel_log_path(tunnel_id: str):
    directory = ensure_private_dir(runtime_dir() / LOG_DIRECTORY)
    return directory / f"{tunnel_id}.log"


def control_path(tunnel_id: str):
    """Unique per-tunnel marker that appears on the ssh command line.

    ControlMaster is never enabled for a tunnel, so this path is never created or
    used as a socket; it exists so the OS can prove which process is ours. The
    name is a short digest rather than the full id because OpenSSH rejects a
    ControlPath over 104 bytes, and a user-supplied SSM_RUNTIME_DIR is allowed to
    be deep.
    """
    return runtime_dir() / f"tun-{_control_token(tunnel_id)}.ctl"


def _control_token(tunnel_id: str) -> str:
    return hashlib.sha256(str(tunnel_id).encode("utf-8")).hexdigest()[:10]


# OpenSSH validates ControlPath as a Unix-domain socket path even when it is never
# bound. 104 bytes is the portable cap; stay under it with room to spare.
MAX_CONTROL_PATH = 100


@contextmanager
def _state_lock() -> Iterator[None]:
    """Serialize state-file read/modify/write across concurrent serverctl calls.

    O_EXCL is used rather than platform file locking because it behaves the same
    on every OS this tool ships for. A lock left behind by a killed process would
    block every later call, so the wait is bounded and the lock is behind a short
    lease that a stale file cannot hold forever.
    """
    path = _lock_path()
    deadline = time.monotonic() + 5.0
    while True:
        try:
            handle = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                age = time.time() - path.stat().st_mtime
            except OSError:
                age = 0.0
            if age > 30.0:
                try:
                    path.unlink()
                except OSError:
                    pass
                continue
            if time.monotonic() > deadline:
                raise SSHError(
                    f"another serverctl process is updating tunnel state ({path}); try again"
                )
            time.sleep(0.02)
            continue
        except OSError as exc:
            raise SSHError(f"cannot lock tunnel state at {path}: {exc}") from exc
        try:
            os.write(handle, str(os.getpid()).encode("ascii"))
            os.close(handle)
            yield
        finally:
            try:
                path.unlink()
            except OSError:
                pass
        return


def _read_state() -> dict[str, dict[str, Any]]:
    try:
        raw = _state_path().read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError:
        return {}
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    tunnels = payload.get("tunnels") if isinstance(payload, dict) else None
    if not isinstance(tunnels, dict):
        return {}
    return {str(key): value for key, value in tunnels.items() if isinstance(value, dict)}


def _write_state(tunnels: dict[str, dict[str, Any]]) -> None:
    path = _state_path()
    temporary = path.with_suffix(".tmp")
    text = json.dumps({"tunnels": tunnels}, indent=2, ensure_ascii=False)
    descriptor = os.open(str(temporary), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    os.replace(temporary, path)


# --- process inspection -----------------------------------------------------------


def process_alive(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if _is_windows():
        return _windows_process_alive(pid)
    if _reap_if_child(pid):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Somebody else's process: it exists, but it cannot be ours.
        return False
    except OSError:
        return False
    return True


def _reap_if_child(pid: int) -> bool:
    """True when pid is a child that has already exited but is still a zombie.

    A real tunnel outlives the serverctl process that started it and is reaped by
    init, so this only matters when the tunnel's parent is still around (tests,
    and a caller that opens a tunnel and closes it in the same process). Without
    it a zombie answers kill(pid, 0) forever and looks like a live tunnel.
    """
    if os.name == "nt":
        return False
    try:
        reaped, _ = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return False  # Not our child: the ordinary case for a live tunnel.
    except OSError:
        return False
    return reaped == pid


def _windows_process_alive(pid: int) -> bool:
    # os.kill(pid, 0) would *terminate* the process on Windows, so this goes
    # through the Win32 API instead and only ever asks for its exit code.
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def process_command_line(pid: int) -> str | None:
    """Best-effort command line for a pid; None when this OS/query cannot say.

    Read natively where the OS offers it: spawning `ps` for every status call is
    both slower and pointless on a machine that already exposes the same data
    through /proc or the kernel. `ps` stays as the last resort for other POSIX
    systems.
    """
    if _is_windows():
        return _windows_command_line(pid)
    if sys.platform.startswith("linux"):
        text = _procfs_command_line(pid)
        if text is not None:
            return text
    elif sys.platform == "darwin":
        text = _macos_command_line(pid)
        if text is not None:
            return text
    return _ps_command_line(pid)


def _procfs_command_line(pid: int) -> str | None:
    try:
        raw = Path(f"/proc/{int(pid)}/cmdline").read_bytes()
    except (OSError, ValueError):
        return None
    parts = [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]
    return " ".join(parts) or None


def _macos_command_line(pid: int) -> str | None:
    """KERN_PROCARGS2 returns argc, the executable path, then the argv strings.

    The buffer is allocated at ARG_MAX up front on purpose: a buffer that is too
    small is not reported as ENOMEM on macOS, it comes back "successful" with the
    argument block zeroed, which looks exactly like a process with no arguments.
    """
    import ctypes

    control_kern = 1
    kern_procargs2 = 49
    # sysctl() names ARG_MAX as the size it may need; 1 MiB covers the default.
    size = 1024 * 1024
    try:
        libc = ctypes.CDLL("libc.dylib", use_errno=True)
    except OSError:
        return None
    mib = (ctypes.c_int * 3)(control_kern, kern_procargs2, int(pid))
    buffer = ctypes.create_string_buffer(size)
    length = ctypes.c_size_t(size)
    if libc.sysctl(mib, 3, buffer, ctypes.byref(length), None, 0) != 0:
        return None
    raw = buffer.raw[: length.value]
    if len(raw) < 4:
        return None
    argc = int.from_bytes(raw[:4], sys.byteorder)
    if argc <= 0:
        return None
    index = raw.find(b"\0", 4)
    if index < 0:
        return None
    while index < len(raw) and raw[index] == 0:
        index += 1
    arguments: list[str] = []
    for _ in range(argc):
        end = raw.find(b"\0", index)
        if end < 0:
            break
        arguments.append(raw[index:end].decode("utf-8", "replace"))
        index = end + 1
    return " ".join(arguments) or None


def _ps_command_line(pid: int) -> str | None:
    try:
        result = subprocess.run(
            ["ps", "-o", "args=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _windows_command_line(pid: int) -> str | None:
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _record_is_running(record: dict[str, Any]) -> bool:
    pid = record.get("pid")
    if not isinstance(pid, int) or not process_alive(pid):
        return False
    command_line = process_command_line(pid)
    if command_line is None:
        # No way to confirm identity on this machine: trust the pid rather than
        # reporting a live tunnel as dead and leaving it orphaned.
        return True
    marker = str(control_path(str(record.get("id", ""))))
    return marker in command_line


# --- command construction ---------------------------------------------------------


def tunnel_command(
    database: Database,
    server: dict[str, Any],
    entry: dict[str, Any],
    *,
    timeout: int,
    ssh_binary: str | None = None,
) -> list[str]:
    """Build the `ssh -N` command that realises one saved profile."""
    runner = SSHRunner(database, ssh_binary=ssh_binary)
    command = runner._base_command(server, timeout=timeout)
    # _base_command ends with the destination alias; every option has to land
    # before it.
    command[-1:-1] = [
        "-N",
        "-o",
        # Fail loudly instead of connecting and silently not forwarding, which is
        # the failure mode that wastes the most time.
        "ExitOnForwardFailure=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "NumberOfPasswordPrompts=1",
        "-o",
        # A forwarding connection must not attach to a shared master: with -N the
        # master would never carry the forward, and the ControlPath below is a
        # liveness marker, not a socket to reuse.
        "ControlMaster=no",
        "-o",
        f"ControlPath={control_path(entry['id'])}",
        _forward_flag(entry["kind"]),
        format_forward_spec(entry),
    ]
    return command


def _redact_log(text: str) -> str:
    return redact(text)


def _log_tail(path, *, limit: int = 1600) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return _redact_log(text.strip()[-limit:])


def _spawn(command: list[str], environment: dict[str, str], log_path) -> subprocess.Popen:
    descriptor = os.open(str(log_path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    log_handle = os.fdopen(descriptor, "wb")
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": log_handle,
        "stderr": subprocess.STDOUT,
        "env": environment,
        "close_fds": True,
    }
    if _is_windows():
        kwargs["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        )
    else:
        # Its own session: the tunnel must outlive the shell that opened it and
        # must not take the terminal's signals when the user presses Ctrl+C.
        kwargs["start_new_session"] = True
    try:
        return subprocess.Popen(command, **kwargs)
    finally:
        log_handle.close()


# --- public operations ------------------------------------------------------------


def tunnel_state() -> list[dict[str, Any]]:
    """Every recorded tunnel that is still running; stale records are dropped."""
    with _state_lock():
        records = _read_state()
        alive: dict[str, dict[str, Any]] = {}
        for key, record in records.items():
            if _record_is_running(record):
                alive[key] = record
        if len(alive) != len(records):
            _write_state(alive)
        return [dict(record) for record in alive.values()]


def summarize(database: Database, *, server_identifier: str | None = None) -> list[dict[str, Any]]:
    """Saved profiles joined with this machine's live process state.

    A live record whose profile has been deleted is reported as an orphan instead
    of being hidden: the process is still holding ports, and somebody has to be
    able to close it.
    """
    profiles = database.list_tunnels(server_identifier)
    by_id = {entry["id"]: entry for entry in profiles}
    records = tunnel_state()
    running: dict[str, dict[str, Any]] = {record["id"]: record for record in records if record.get("id")}
    rows: list[dict[str, Any]] = []
    for entry in profiles:
        record = running.get(entry["id"])
        rows.append(
            {
                **entry,
                "running": record is not None,
                "pid": record.get("pid") if record else None,
                "started_at": record.get("started_at") if record else None,
                "uptime_seconds": _uptime(record) if record else None,
            }
        )
    for record in records:
        entry = by_id.get(record.get("id"))
        if entry is not None:
            continue
        rows.append(
            {
                "id": record.get("id"),
                "server": record.get("alias"),
                "name": record.get("name"),
                "kind": record.get("kind"),
                "forward": record.get("forward"),
                "running": True,
                "orphan": True,
                "pid": record.get("pid"),
                "started_at": record.get("started_at"),
                "uptime_seconds": _uptime(record),
            }
        )
    return rows


def _uptime(record: dict[str, Any] | None) -> int | None:
    if not record:
        return None
    started = record.get("started_at")
    if not isinstance(started, str):
        return None
    try:
        moment = datetime.fromisoformat(started)
    except ValueError:
        return None
    return max(0, int((datetime.now(moment.tzinfo) - moment).total_seconds()))


def open_tunnel(
    database: Database,
    identifier: str,
    *,
    timeout: int = 20,
    foreground: bool = False,
    ssh_binary: str | None = None,
    actor: str = "cli",
) -> dict[str, Any]:
    """Start a saved profile in the background (or hand the terminal to it)."""
    entry = database.get_tunnel(identifier)
    server = database.get_server(entry["server"])
    marker = str(control_path(entry["id"]))
    if len(marker) >= MAX_CONTROL_PATH:
        # Reported here rather than as ssh's "ControlPath too long", which says
        # nothing about the environment variable that actually caused it.
        raise SSHError(
            f"the tunnel state directory is too deep for OpenSSH ({len(marker)} characters); "
            f"point SSM_RUNTIME_DIR at a shorter directory than {runtime_dir()}"
        )
    command = tunnel_command(
        database, server, entry, timeout=timeout, ssh_binary=ssh_binary
    )
    runner = SSHRunner(database, ssh_binary=ssh_binary)
    environment = runner._environment(server)

    existing = next(
        (record for record in tunnel_state() if record.get("id") == entry["id"]), None
    )
    if existing is not None:
        return {
            **entry,
            "opened": False,
            "already_open": True,
            "pid": existing.get("pid"),
            "started_at": existing.get("started_at"),
        }

    log_path = tunnel_log_path(entry["id"])
    if foreground:
        code = subprocess.call(command, env=environment)
        if code != 0:
            raise SSHError(
                _log_tail(log_path) or f"ssh exited with status {code} while forwarding"
            )
        return {**entry, "opened": False, "foreground": True}

    try:
        process = _spawn(command, environment, log_path)
    except OSError as exc:
        raise SSHError(f"could not run ssh for tunnel '{entry['name']}': {exc}") from exc

    deadline = time.monotonic() + STARTUP_GRACE_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            detail = _log_tail(log_path)
            raise SSHError(
                detail
                or (
                    f"tunnel '{entry['name']}' exited immediately with status "
                    f"{process.returncode}; run 'serverctl server test {entry['server']}' "
                    "to confirm the host is reachable and its host key is known"
                )
            )
        time.sleep(0.05)

    record = {
        "id": entry["id"],
        "name": entry["name"],
        "alias": entry["server"],
        "kind": entry["kind"],
        "forward": entry["forward"],
        "pid": process.pid,
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "log": str(log_path),
        "control_path": str(control_path(entry["id"])),
    }
    with _state_lock():
        records = _read_state()
        records[entry["id"]] = record
        _write_state(records)

    # ssh may still die a moment after authentication; the caller gets one more
    # look so "opened" is not reported for a forward that immediately collapsed.
    time.sleep(0.15)
    if process.poll() is not None:
        with _state_lock():
            records = _read_state()
            records.pop(entry["id"], None)
            _write_state(records)
        raise SSHError(
            _log_tail(log_path)
            or f"tunnel '{entry['name']}' exited with status {process.returncode} after connecting"
        )

    audit.record(
        "tunnel_open",
        actor=actor,
        alias=entry["server"],
        tunnel=entry["name"],
        kind=entry["kind"],
        forward=entry["forward"],
        pid=process.pid,
    )
    return {
        **entry,
        "opened": True,
        "pid": process.pid,
        "started_at": record["started_at"],
        "log": str(log_path),
    }


def _terminate(pid: int) -> None:
    if _is_windows():
        # TerminateProcess: ssh has no console to receive a graceful signal from.
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError as exc:
        raise SSHError(f"could not signal tunnel process {pid}: {exc}") from exc


def _hard_kill(pid: int) -> None:
    if _is_windows():
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def close_tunnel(database: Database, identifier: str, *, actor: str = "cli") -> dict[str, Any]:
    """Stop one running tunnel and forget its runtime record."""
    text = str(identifier).strip()
    entry: dict[str, Any] | None
    try:
        entry = database.get_tunnel(text)
    except NotFoundError:
        entry = None

    with _state_lock():
        records = _read_state()
        record = records.get(entry["id"]) if entry is not None else None
        if record is None:
            record = _match_record(records, text, entry)
        if record is None:
            if entry is not None:
                # A saved profile that is simply not running: closing it again is
                # a no-op, not a failure, so a teardown script can be idempotent.
                return {
                    "name": entry["name"],
                    "server": entry["server"],
                    "kind": entry["kind"],
                    "forward": entry["forward"],
                    "closed": False,
                    "already_stopped": True,
                    "pid": None,
                }
            raise ValidationError(f"tunnel is not open: {identifier}")
        running = _record_is_running(record)
        records.pop(str(record.get("id")), None)
        _write_state(records)

    if not running:
        return {
            "name": record.get("name"),
            "server": record.get("alias"),
            "closed": False,
            "already_stopped": True,
            "pid": record.get("pid"),
        }

    pid = int(record["pid"])
    _terminate(pid)
    deadline = time.monotonic() + TERMINATE_GRACE_SECONDS
    while time.monotonic() < deadline:
        if not process_alive(pid):
            break
        time.sleep(0.05)
    else:
        _hard_kill(pid)

    audit.record(
        "tunnel_close",
        actor=actor,
        alias=record.get("alias"),
        tunnel=record.get("name"),
        forward=record.get("forward"),
        pid=pid,
    )
    return {
        "name": record.get("name"),
        "server": record.get("alias"),
        "kind": record.get("kind"),
        "forward": record.get("forward"),
        "closed": True,
        "pid": pid,
        "log": record.get("log"),
    }


def _match_record(
    records: dict[str, dict[str, Any]], text: str, entry: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Find a live record for a name, an id, or an existing profile.

    The profile lookup fails once a profile row is deleted, but its process is
    still holding a port; `status` reports that as an orphan and this makes it
    closeable by the name it was reported under.
    """
    direct = records.get(text)
    if direct is not None:
        return direct
    alias, separator, name = text.partition(":")
    wanted_name = (name if separator and name else text).casefold()
    wanted_alias = alias.strip().casefold() if separator and name else None
    for record in records.values():
        if str(record.get("name") or "").casefold() != wanted_name:
            continue
        if wanted_alias is not None and str(record.get("alias") or "").casefold() != wanted_alias:
            continue
        if entry is not None and record.get("id") != entry["id"]:
            continue
        return record
    return None


def close_all(database: Database) -> list[dict[str, Any]]:
    """Stop every recorded tunnel; used by teardown and tests."""
    results = []
    for record in tunnel_state():
        identifier = str(record.get("id"))
        try:
            results.append(close_tunnel(database, identifier))
        except (ValidationError, NotFoundError):
            continue
    return results


def close_for_server(database: Database, server_identifier: str) -> list[dict[str, Any]]:
    """Stop every open tunnel that belongs to one host.

    Called before a host is deleted: the profile rows cascade away with it, but
    nothing would stop the ssh processes still holding the ports.
    """
    server = database.get_server(server_identifier)
    profile_ids = {entry["id"] for entry in database.list_tunnels(server["id"])}
    results: list[dict[str, Any]] = []
    for record in tunnel_state():
        if record.get("id") not in profile_ids:
            continue
        try:
            results.append(close_tunnel(database, str(record.get("id"))))
        except (ValidationError, NotFoundError):
            continue
    return results
