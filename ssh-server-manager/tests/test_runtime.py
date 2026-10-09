from __future__ import annotations

import asyncio
import os
import socket
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI

from ssh_server_manager.db import Database
from ssh_server_manager.runtime import LocalRuntime, UIError, _read_ui_state, ui_status


@pytest.fixture
def isolated_database(tmp_path, monkeypatch):
    monkeypatch.setenv("SSM_RUNTIME_DIR", str(tmp_path / "runtime"))
    return Database(tmp_path / "manager.db")


def test_background_runtime_is_ready_authenticated_and_cleans_up(isolated_database, capsys):
    runtime = LocalRuntime(isolated_database)
    with runtime:
        assert ui_status()["running"]
        assert _read_ui_state()["port"] == runtime.port
        assert runtime._thread.is_alive()
        with httpx.Client(base_url=f"http://localhost:{runtime.port}", trust_env=False) as client:
            assert client.get("/api/v1/runtime").status_code == 401
            assert client.get(runtime.launch_url).status_code == 200
            assert client.get("/api/v1/hosts").json() == []
            client.cookies.clear()
            assert client.get(runtime.launch_url).status_code == 401
        with pytest.raises(UIError, match="already running"):
            LocalRuntime(isolated_database).start()
    assert not runtime._thread.is_alive()
    assert _read_ui_state() is None
    assert not ui_status()["running"]
    with pytest.raises(UIError, match="not started"):
        _ = runtime.launch_url
    # No secret-bearing URL is written by the programmatic/native-shell path.
    assert "token=" not in capsys.readouterr().out
    runtime.stop()


def test_reserved_port_stays_bound_until_runtime_stops(isolated_database):
    runtime = LocalRuntime(isolated_database).start()
    port = runtime.port
    try:
        with socket.socket() as probe:
            with pytest.raises(OSError):
                probe.bind(("127.0.0.1", port))
    finally:
        runtime.stop()
    # The listener has been closed; its port is no longer serving requests.
    with socket.socket() as probe:
        assert probe.connect_ex(("127.0.0.1", port)) != 0


@pytest.mark.skipif(os.name == "nt", reason="SO_REUSEADDR is deliberately not set on Windows")
def test_fixed_port_restarts_immediately_after_stop_with_open_client(isolated_database):
    first = LocalRuntime(isolated_database).start()
    port = first.port
    # The client keeps its keep-alive connection open across the stop, which
    # leaves the server side in FIN_WAIT_2/TIME_WAIT on this exact port.
    with httpx.Client(base_url=f"http://localhost:{port}", trust_env=False) as client:
        assert client.get(first.launch_url).status_code == 200
        assert client.get("/api/v1/hosts").status_code == 200
        first.stop()
        second = LocalRuntime(isolated_database, port=port).start()
        try:
            assert second.port == port
        finally:
            second.stop()


def test_concurrent_launch_is_refused_even_when_status_check_races(isolated_database, monkeypatch):
    first = LocalRuntime(isolated_database).start()
    try:
        # Simulate a second launch that ran its status check before the first
        # runtime wrote its record: only the OS lock can stop it now.
        monkeypatch.setattr("ssh_server_manager.runtime.ui_status", lambda: {"running": False})
        second = LocalRuntime(isolated_database)
        with pytest.raises(UIError, match="already running"):
            second.start()
        assert second._socket is None
        assert _read_ui_state()["pid"] == os.getpid() and _read_ui_state()["port"] == first.port
    finally:
        first.stop()
    # The lock is released with the runtime, so the next launch succeeds.
    LocalRuntime(isolated_database).start().stop()


def test_runtime_record_exists_before_launch_url_is_written(isolated_database, tmp_path, monkeypatch):
    from ssh_server_manager import runtime as runtime_module

    seen = []
    real_write = runtime_module._write_private_url
    def observe(path, url):
        state = _read_ui_state()
        seen.append(state)
        return real_write(path, url)
    monkeypatch.setattr(runtime_module, "_write_private_url", observe)
    monkeypatch.setattr("uvicorn.Server.run", lambda _self, **_kwargs: None)
    url_file = tmp_path / "url"
    LocalRuntime(isolated_database).run_web(open_browser=False, url_file=url_file)
    assert seen and seen[0]["url_file"] == str(url_file)
    assert _read_ui_state() is None and not url_file.exists()


def test_startup_exception_cleans_url_socket_and_state(isolated_database, tmp_path):
    runtime = LocalRuntime(isolated_database, app_factory=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("fixture failed")))
    url_file = tmp_path / "url"
    with pytest.raises(RuntimeError, match="fixture failed"):
        runtime.run_web(open_browser=False, url_file=url_file)
    assert not url_file.exists()
    assert _read_ui_state() is None
    assert runtime._socket is None


def test_background_server_failure_is_reported_and_cleaned(isolated_database, monkeypatch):
    def fail(_server, **_kwargs):
        raise RuntimeError("fixture startup failed")
    monkeypatch.setattr("uvicorn.Server.run", fail)
    runtime = LocalRuntime(isolated_database)
    with pytest.raises(UIError, match="failed to start"):
        runtime.start()
    assert not runtime._thread.is_alive()
    assert _read_ui_state() is None


def test_startup_timeout_waits_for_shutdown_and_cleans_up(isolated_database):
    @asynccontextmanager
    async def lifespan(_app):
        await asyncio.sleep(0.1)
        yield
    runtime = LocalRuntime(isolated_database, app_factory=lambda *_args, **_kwargs: FastAPI(lifespan=lifespan))
    with pytest.raises(UIError, match="startup timeout"):
        runtime.start(timeout=0.02)
    assert not runtime._thread.is_alive()
    assert _read_ui_state() is None


def test_browser_opens_only_after_server_is_ready(isolated_database, monkeypatch, capsys):
    opened = []
    runtime = LocalRuntime(isolated_database)
    def open_browser(url):
        assert runtime._server.started
        with httpx.Client(trust_env=False) as client:
            assert client.get(url).status_code == 200
        opened.append(True)
        runtime._server.should_exit = True
    monkeypatch.setattr("ssh_server_manager.runtime.webbrowser.open", open_browser)
    runtime.run_web()
    assert opened == [True]
    assert _read_ui_state() is None
    assert "token=" not in capsys.readouterr().out
