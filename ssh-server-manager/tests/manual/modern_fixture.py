#!/usr/bin/env python3
"""Real loopback SSH/SFTP + Web fixture, with disposable state and trusted keys.

The only accepted remote commands are the product's fixed health/metadata probes.
No shell or PTY is available. Forwarding is restricted to this fixture's ports.
The standard one-use launch URL is written to a private file, never stdout.
"""
from __future__ import annotations

import argparse
import errno
import json
import logging
import os
import posixpath
import signal
import shlex
import socket
import stat
import struct
import sys
import tempfile
import threading
import time
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import paramiko
from paramiko import Message
from paramiko.sftp import CMD_EXTENDED, CMD_INIT, CMD_VERSION

from ssh_server_manager import application, webapp
from ssh_server_manager.db import Database
from ssh_server_manager.remote_files import METADATA_PREFIX, METADATA_SCRIPT
from ssh_server_manager.runtime import LocalRuntime
from ssh_server_manager.service import CredentialService, SkillService
from ssh_server_manager.ssh_config import quote_value, render_config
from ssh_server_manager.validation import ValidationError
from ssh_server_manager.vault import MemoryVault


def deny_local_file_access(*args, **kwargs):
    raise ValidationError("Local import and credential-file edits are disabled in the loopback fixture")


class FixtureSFTP(paramiko.SFTPServerInterface):
    """A chroot-like mapping that cannot resolve a path outside the temp tree."""

    def __init__(self, server, *args, root: Path, **kwargs):
        super().__init__(server, *args, **kwargs)
        self.root = root.resolve()

    def local(self, path: str, *, follow: bool = True) -> Path:
        if path == "~" or path.startswith("~/"):
            path = path[1:]
        remote = posixpath.normpath("/" + path.lstrip("/"))
        candidate = self.root / remote.lstrip("/")
        if candidate == self.root:
            return candidate
        checked = candidate.resolve() if follow else candidate.parent.resolve() / candidate.name
        if not checked.is_relative_to(self.root):
            raise PermissionError(errno.EACCES, "outside fixture")
        return checked

    @staticmethod
    def error(exc: OSError):
        return paramiko.SFTPServer.convert_errno(exc.errno or errno.EIO)

    def canonicalize(self, path):
        try:
            local = self.local(path)
            relative = local.relative_to(self.root).as_posix()
            return "/" if relative == "." else "/" + relative
        except OSError as exc:
            return self.error(exc)

    def list_folder(self, path):
        try:
            directory = self.local(path)
            entries = []
            for name in sorted(os.listdir(directory)):
                value = paramiko.SFTPAttributes.from_stat((directory / name).lstat())
                value.filename = name
                entries.append(value)
            return entries
        except OSError as exc:
            return self.error(exc)

    def stat(self, path):
        try:
            return paramiko.SFTPAttributes.from_stat(self.local(path).stat())
        except OSError as exc:
            return self.error(exc)

    def lstat(self, path):
        try:
            return paramiko.SFTPAttributes.from_stat(self.local(path, follow=False).lstat())
        except OSError as exc:
            return self.error(exc)

    def open(self, path, flags, attr):
        try:
            local = self.local(path)
            descriptor = os.open(local, flags, attr.st_mode or 0o600)
            handle = FixtureHandle(flags, local)
            writable = flags & (os.O_WRONLY | os.O_RDWR)
            mode = "r+b" if flags & os.O_RDWR else "wb" if writable else "rb"
            file = os.fdopen(descriptor, mode)
            if not flags & os.O_WRONLY:
                handle.readfile = file
            if writable:
                handle.writefile = file
            return handle
        except OSError as exc:
            return self.error(exc)

    def chattr(self, path, attr):
        try:
            paramiko.SFTPServer.set_file_attr(self.local(path), attr)
            return paramiko.SFTP_OK
        except OSError as exc:
            return self.error(exc)

    def remove(self, path):
        try:
            self.local(path, follow=False).unlink()
            return paramiko.SFTP_OK
        except OSError as exc:
            return self.error(exc)

    def mkdir(self, path, attr):
        try:
            self.local(path).mkdir(mode=attr.st_mode or 0o700)
            return paramiko.SFTP_OK
        except OSError as exc:
            return self.error(exc)

    def rmdir(self, path):
        try:
            self.local(path).rmdir()
            return paramiko.SFTP_OK
        except OSError as exc:
            return self.error(exc)

    def rename(self, oldpath, newpath):
        try:
            destination = self.local(newpath, follow=False)
            if destination.exists():
                return paramiko.SFTP_FAILURE
            self.local(oldpath, follow=False).rename(destination)
            return paramiko.SFTP_OK
        except OSError as exc:
            return self.error(exc)

    def posix_rename(self, oldpath, newpath):
        try:
            os.replace(self.local(oldpath, follow=False), self.local(newpath, follow=False))
            return paramiko.SFTP_OK
        except OSError as exc:
            return self.error(exc)

    def hardlink(self, oldpath, newpath):
        try:
            os.link(self.local(oldpath), self.local(newpath, follow=False))
            return paramiko.SFTP_OK
        except OSError as exc:
            return self.error(exc)

    def readlink(self, path):
        try:
            return os.readlink(self.local(path, follow=False))
        except OSError as exc:
            return self.error(exc)


class FixtureSFTPServer(paramiko.SFTPServer):
    """Advertise atomic hardlink publication used by no-overwrite uploads."""

    def _send_server_version(self):
        packet, data = self._read_packet()
        if packet != CMD_INIT:
            raise paramiko.SSHException("Expected SFTP initialization")
        message = Message()
        message.add_int(3)
        message.add("posix-rename@openssh.com", "1", "hardlink@openssh.com", "1")
        self._send_packet(CMD_VERSION, message)
        return struct.unpack(">I", data[:4])[0]

    def _process(self, packet, request, message):
        if packet == CMD_EXTENDED:
            original = message.asbytes()
            if message.get_text() == "hardlink@openssh.com":
                self._send_status(request, self.server.hardlink(message.get_text(), message.get_text()))
                return
            message = Message(original)
        super()._process(packet, request, message)


class FixtureHandle(paramiko.SFTPHandle):
    def __init__(self, flags, path):
        super().__init__(flags)
        self.path = path

    def stat(self):
        try:
            return paramiko.SFTPAttributes.from_stat(self.path.stat())
        except OSError as exc:
            return FixtureSFTP.error(exc)

    def chattr(self, attr):
        try:
            paramiko.SFTPServer.set_file_attr(self.path, attr)
            return paramiko.SFTP_OK
        except OSError as exc:
            return FixtureSFTP.error(exc)


class FixtureSSHServer(paramiko.ServerInterface):
    def __init__(self, owner):
        self.owner = owner
        self.destinations = {}

    def get_allowed_auths(self, username):
        return "publickey"

    def check_auth_publickey(self, username, key):
        accepted = username == "fixture" and key == self.owner.client_key
        self.owner.auth_attempts += 1
        return paramiko.AUTH_SUCCESSFUL if accepted else paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_direct_tcpip_request(self, chanid, origin, destination):
        if destination[0] != "127.0.0.1" or destination[1] not in self.owner.allowed_ports:
            return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
        self.destinations[chanid] = destination
        self.owner.forward_attempts += 1
        return paramiko.OPEN_SUCCEEDED

    def check_channel_exec_request(self, channel, command):
        text = command.decode("utf-8", "strict")
        permitted = text == "true" or "__ssm_hostname=" in text or text.startswith("python3 -c ")
        if not permitted:
            self.owner.rejected_commands += 1
            return False
        self.owner.exec_commands.append(text)
        self.owner.worker(self.owner.execute, channel, text)
        return True

    # The inherited subsystem callback starts the registered SFTP handler.
    # Shell, PTY, X11 and agent forwarding callbacks remain denied by default.


class LoopbackSSH:
    def __init__(self, root: Path, client_key, *, metadata: bool = True):
        self.root = root
        self.client_key = client_key
        self.metadata = metadata
        self.host_key = paramiko.RSAKey.generate(2048)
        self.allowed_ports: set[int] = set()
        self.auth_attempts = self.forward_attempts = self.rejected_commands = 0
        self.exec_commands: list[str] = []
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._transports = []
        self._workers = []
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(32)
        self._listener.settimeout(0.2)
        self.port = self._listener.getsockname()[1]
        self.worker(self._accept)

    def worker(self, function, *args):
        thread = threading.Thread(target=function, args=args, daemon=True)
        with self._lock:
            self._workers.append(thread)
        thread.start()

    def _accept(self):
        while not self._stop.is_set():
            try:
                client, _address = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.worker(self._serve, client)

    def _serve(self, client):
        transport = paramiko.Transport(client)
        with self._lock:
            self._transports.append(transport)
        transport.add_server_key(self.host_key)
        transport.set_subsystem_handler("sftp", FixtureSFTPServer, FixtureSFTP, root=self.root)
        server = FixtureSSHServer(self)
        channels = []
        try:
            transport.start_server(server=server)
            while transport.is_active() and not self._stop.is_set():
                channel = transport.accept(0.2)
                if channel is not None:
                    channels.append(channel)
                    destination = server.destinations.get(channel.get_id())
                    if destination is not None:
                        self.worker(self._forward, channel, destination)
                channels[:] = [channel for channel in channels if not channel.closed]
        except (EOFError, OSError, paramiko.SSHException):
            pass
        finally:
            transport.close()
            client.close()

    def _forward(self, channel, destination):
        try:
            connection = socket.create_connection(destination, timeout=3)
        except OSError:
            channel.close()
            return
        connection.settimeout(0.2)
        channel.settimeout(0.2)

        def pump(source, destination):
            try:
                while not self._stop.is_set():
                    try:
                        chunk = source.recv(65536)
                    except socket.timeout:
                        continue
                    if not chunk:
                        break
                    destination.sendall(chunk)
            except (EOFError, OSError):
                pass
            finally:
                for endpoint in (source, destination):
                    try:
                        endpoint.close()
                    except (EOFError, OSError):
                        pass

        self.worker(pump, channel, connection)
        pump(connection, channel)

    def _metadata(self, request):
        filesystem = FixtureSFTP(None, root=self.root)

        def value(path, follow=False):
            local = filesystem.local(path, follow=follow)
            item = local.stat() if follow else local.lstat()
            kind = ("file" if stat.S_ISREG(item.st_mode) else "directory" if stat.S_ISDIR(item.st_mode)
                    else "symlink" if stat.S_ISLNK(item.st_mode) else "special")
            return {"type": kind, "size": item.st_size, "uid": item.st_uid, "gid": item.st_gid,
                    "owner": "fixture", "group": "fixture", "link_target": os.readlink(local) if kind == "symlink" else None,
                    "modified_at": datetime.fromtimestamp(item.st_mtime, timezone.utc).isoformat()}

        try:
            if request["mode"] == "stat":
                return {"ok": True, "entry": value(request["path"], request.get("follow", True))}
            return {"ok": True, "entries": {
                name: value(posixpath.join(request["path"], name)) for name in request["names"][:5000]
                if isinstance(name, str) and name not in {".", ".."} and "/" not in name
            }}
        except OSError as exc:
            return {"ok": False, "error_code": {errno.ENOENT: "not_found", errno.EEXIST: "exists",
                    errno.ENOTDIR: "not_a_directory", errno.EACCES: "permission_denied"}.get(exc.errno, "unreachable")}

    def execute(self, channel, command):
        code = 0
        try:
            if command == "true":
                output = ""
            elif "__ssm_hostname=" in command:
                output = ("__ssm_hostname=fixture-node\n__ssm_os_pretty=Fixture Linux\n__ssm_os_id=ubuntu\n"
                          "__ssm_os_version=24.04\n__ssm_kernel_name=Linux\n__ssm_kernel=Linux 6.8 fixture\n"
                          "__ssm_arch=x86_64\n__ssm_cpu_cores=4\n__ssm_uptime=up 1 hour\n")
            elif command == "python3 -c " + shlex.quote(METADATA_SCRIPT) and self.metadata:
                channel.settimeout(5)
                chunks = bytearray()
                while len(chunks) <= 2 * 1024 * 1024:
                    chunk = channel.recv(65536)
                    if not chunk:
                        break
                    chunks.extend(chunk)
                output = METADATA_PREFIX + json.dumps(self._metadata(json.loads(chunks))) + "\n"
            else:
                output, code = "", 127
            if output:
                channel.sendall(output.encode())
            channel.send_exit_status(code)
        except (OSError, ValueError, KeyError):
            pass
        finally:
            try:
                channel.close()
            except (EOFError, OSError):
                pass

    def close(self):
        self._stop.set()
        self._listener.close()
        with self._lock:
            transports = list(self._transports)
        for transport in transports:
            transport.close()
        deadline = time.monotonic() + 3
        with self._lock:
            workers = list(self._workers)
        for thread in workers:
            thread.join(max(0, deadline - time.monotonic()))


@contextmanager
def fixture_environment():
    """Only disposable paths and a MemoryVault are visible to the product."""
    # OpenSSH's Unix-domain ControlPath must fit its short platform limit.
    # macOS's default /var/folders temp directory is too long for cm-%C.
    with tempfile.TemporaryDirectory(prefix="ssm-modern-fixture-") as directory, \
            tempfile.TemporaryDirectory(prefix="ssm-r-", dir="/tmp" if os.name != "nt" else None) as runtime_directory, \
            ExitStack() as stack:
        root = Path(directory)
        if os.name != "nt":
            root.chmod(0o700)
        remote = root / "remote"
        remote.mkdir()
        (remote / "existing.txt").write_text("original fixture content\n")
        (remote / "readme.txt").write_text("loopback SFTP download\n")
        (remote / ".hidden.txt").write_text("hidden fixture\n")
        (remote / "unicode-部署.txt").write_text("Unicode fixture\n")
        (remote / "empty").mkdir()
        (remote / "large").mkdir()
        for index in range(5005):
            (remote / "large" / f"entry-{index:05d}.txt").touch()
        with (remote / "oversize.bin").open("wb") as file:
            file.truncate(webapp.MAX_WEB_TRANSFER_BYTES + 1)
        key = paramiko.RSAKey.generate(2048)
        wrong_key = paramiko.RSAKey.generate(2048)
        key_path, wrong_path = root / "client-key", root / "wrong-key"
        key.write_private_key_file(str(key_path))
        wrong_key.write_private_key_file(str(wrong_path))
        servers = [LoopbackSSH(remote, key) for _ in range(3)]
        servers.append(LoopbackSSH(remote, key, metadata=False))
        for server in servers:
            server.allowed_ports = {item.port for item in servers}
            stack.callback(server.close)
        known_hosts = root / "known_hosts"
        known_hosts.write_text("".join(f"[127.0.0.1]:{server.port} {server.host_key.get_name()} {server.host_key.get_base64()}\n"
                                      for server in servers))
        original = root / "original.conf"
        original.write_text(f"Host external\n HostName 127.0.0.1\n User fixture\n Port {servers[1].port}\n"
                            f" IdentityFile {quote_value(str(key_path))}\n IdentitiesOnly yes\n ProxyJump none\n"
                            f"Host *\n UserKnownHostsFile {quote_value(str(known_hosts))}\n GlobalKnownHostsFile none\n"
                            " StrictHostKeyChecking yes\n IdentityAgent none\n ForwardAgent no\n AddKeysToAgent no\n")
        stack.enter_context(patch.dict(os.environ, {
            "SSM_DATA_DIR": str(root / "data"), "SSM_RUNTIME_DIR": runtime_directory,
            "SSM_MANAGED_SSH_CONFIG": str(root / "managed.conf"), "SSM_ORIGINAL_SSH_CONFIG": str(original),
        }))
        database = Database(root / "data" / "manager.db")
        vault = MemoryVault()
        stack.enter_context(patch.object(webapp, "get_vault", return_value=vault))
        stack.enter_context(patch("ssh_server_manager.vault.get_vault", return_value=vault))
        stack.enter_context(patch.object(application, "preview_import", deny_local_file_access))
        for method in ("discover", "register", "refresh"):
            stack.enter_context(patch.object(SkillService, method, deny_local_file_access))
        for method in ("create_key", "update"):
            stack.enter_context(patch.object(CredentialService, method, deny_local_file_access))
        credential = database.create_credential(label="Loopback fixture key", kind="key", key_path=str(key_path), credential_id="fixture-key")
        wrong = database.create_credential(label="Rejected fixture key", kind="key", key_path=str(wrong_path), credential_id="fixture-wrong-key")
        specifications = [("direct", 0, [], credential), ("jump-one", 1, [], credential),
                          ("jump-two", 2, [], credential), ("multi-hop", 0, ["jump-one", "jump-two"], credential),
                          ("external-route", 0, ["external"], credential), ("bad-auth", 0, [], wrong),
                          ("metadata-fallback", 3, [], credential)]
        hosts = {}
        for alias, index, jumps, auth in specifications:
            hosts[alias] = database.create_server(id="fixture-" + alias, alias=alias, hostname="127.0.0.1",
                port=servers[index].port, username="fixture", credential_id=auth["id"], proxy_jumps=jumps,
                tags=["loopback"], notes="Disposable SSH/SFTP fixture; no real infrastructure.")
        database.create_server_path("direct", label="Fixture root", path="/")
        render_config(database)
        yield {"database": database, "vault": vault, "root": root, "remote": remote,
               "servers": servers, "hosts": hosts, "max_web_transfer_bytes": webapp.MAX_WEB_TRANSFER_BYTES}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url-file", required=True, type=Path)
    parser.add_argument("--ready-file", required=True, type=Path)
    parser.add_argument("--stdin-stop", action="store_true", help="Stop cleanly on stdin 'stop' or EOF, including Windows")
    args = parser.parse_args(argv)
    logging.getLogger("paramiko").setLevel(logging.CRITICAL)
    stopped = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stopped.set())
    if args.stdin_stop:
        def watch_stdin():
            for line in sys.stdin:
                if line.strip() == "stop":
                    break
            stopped.set()
        threading.Thread(target=watch_stdin, daemon=True).start()
    with fixture_environment() as fixture:
        runtime = LocalRuntime(fixture["database"], app_factory=webapp.create_app)
        try:
            # Use the standard launch-token/session path, including URL deletion.
            runtime._prepare(url_file=args.url_file)
            runtime._thread = threading.Thread(target=runtime._serve, daemon=True)
            runtime._thread.start()
            runtime._wait_ready(10)
            metadata = {"base_url": f"http://localhost:{runtime.port}",
                        "hosts": {alias: host["id"] for alias, host in fixture["hosts"].items()},
                        "max_web_transfer_bytes": fixture["max_web_transfer_bytes"]}
            args.ready_file.write_text(json.dumps(metadata))
            if os.name != "nt":
                args.ready_file.chmod(0o600)
            print("Isolated loopback SSH/SFTP fixture ready", flush=True)
            while not stopped.wait(0.2):
                if not runtime._thread.is_alive():
                    raise RuntimeError("Fixture web server stopped")
        finally:
            runtime.stop()
            args.ready_file.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
