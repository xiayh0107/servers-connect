#!/usr/bin/env python3
"""Install a distribution outside the checkout and exercise its CLI/Web assets.

No keychain access or SSH connections are needed. All paths, database and runtime
state are temporary; launch URLs and session credentials never enter output.
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import venv
from pathlib import Path


def run_smoke(distribution: Path, expected: str) -> None:
    with tempfile.TemporaryDirectory(prefix="ssm-package-") as temporary:
        root = Path(temporary)
        environment = root / "environment"
        venv.EnvBuilder(with_pip=True).create(environment)
        scripts = environment / ("Scripts" if os.name == "nt" else "bin")
        python = scripts / ("python.exe" if os.name == "nt" else "python")
        subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check",
                        str(distribution)], cwd=root, check=True, stdout=subprocess.DEVNULL)
        launcher = scripts / ("serverctl.exe" if os.name == "nt" else "serverctl")
        isolated = {**os.environ, "SSM_DATA_DIR": str(root / "data"),
                    "SSM_RUNTIME_DIR": str(root / "runtime"),
                    "SSM_ORIGINAL_SSH_CONFIG": str(root / "original.conf"),
                    "SSM_MANAGED_SSH_CONFIG": str(root / "managed.conf")}
        isolated.pop("PYTHONPATH", None)

        def cli(*arguments: str, check: bool = True):
            result = subprocess.run([str(launcher), *arguments], cwd=root, env=isolated,
                                    capture_output=True, text=True, timeout=30)
            if check and result.returncode:
                raise RuntimeError("Packaged CLI command failed: " + " ".join(arguments[:2]))
            return result

        assert cli("--version").stdout.strip().endswith(expected)
        assert json.loads(cli("server", "list", "--json").stdout) == []
        # Inspect package data through the newly installed interpreter, outside
        # the source tree, so an editable/source import cannot mask omissions.
        metadata = subprocess.run([str(python), "-c",
            "import json; from pathlib import Path; import ssh_server_manager as p; "
            "from ssh_server_manager.paths import asset_dir; "
            "from ssh_server_manager.ui_assets import resource_names; "
            "print(json.dumps({'package': str(Path(p.__file__).resolve()), "
            "'assets': sorted(resource_names(asset_dir()))}))"], cwd=root,
            env=isolated, capture_output=True, text=True, check=True, timeout=30)
        installed = json.loads(metadata.stdout)
        assert Path(installed["package"]).is_relative_to(environment.resolve())
        assert len(installed["assets"]) >= 7
        launch_file = root / "launch-url"
        process = subprocess.Popen([str(launcher), "web", "--no-open", "--url-file", str(launch_file)],
                                   cwd=root, env=isolated, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Reap the child concurrently; ui --stop must not mistake a zombie for
        # a live process on platforms where kill(pid, 0) observes zombies.
        reaper = threading.Thread(target=process.wait, daemon=True)
        reaper.start()
        try:
            deadline = time.monotonic() + 30
            while not launch_file.exists():
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("Packaged Web runtime did not start")
                time.sleep(.05)
            launch_url = launch_file.read_text(encoding="utf-8").strip()
            parsed = urllib.parse.urlsplit(launch_url)
            assert parsed.hostname in {"localhost", "127.0.0.1"}
            base = f"{parsed.scheme}://{parsed.netloc}"
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
            while True:
                try:
                    response = opener.open(launch_url, timeout=5)
                    break
                except (urllib.error.URLError, ConnectionError):
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Packaged Web login failed") from None
                    time.sleep(.05)
            html = response.read().decode("utf-8")
            assert "script-src 'self'" in response.headers["Content-Security-Policy"]
            assert not launch_file.exists()
            assert re.search(r'<script[^>]*src="/assets/[^\"]+\.js"', html)
            runtime = json.load(opener.open(base + "/api/v1/runtime", timeout=5))
            assert runtime["version"] == expected and runtime["ui"] == "modern"
            assert runtime["session"]["expires_at"] and runtime["server_time"]
            assert runtime["vault"]["status"] == "unknown"
            for route in ("hosts", "skills", "credentials", "contexts"):
                assert isinstance(json.load(opener.open(base + "/api/v1/" + route, timeout=5)), list)
            for asset in installed["assets"]:
                asset_response = opener.open(base + "/assets/" + asset, timeout=5)
                assert asset_response.read() and "immutable" in asset_response.headers["Cache-Control"]
            for forbidden in ("asset-manifest.json", "index.html", "missing.js"):
                try:
                    opener.open(base + "/assets/" + forbidden, timeout=5)
                    raise AssertionError("Unlisted package resource was served")
                except urllib.error.HTTPError as error:
                    assert error.code == 404
            # A real CSRF-protected write/read/delete verifies the installed v1
            # client contract without touching credentials or the OS keychain.
            request = urllib.request.Request(base + "/api/v1/contexts", method="POST",
                data=json.dumps({"name": "安装验证 tag"}).encode(),
                headers={"Content-Type": "application/json", "X-CSRF-Token": runtime["csrf"]})
            tag = json.load(opener.open(request, timeout=5))
            request = urllib.request.Request(base + "/api/v1/contexts/" + urllib.parse.quote(tag["name"]),
                method="DELETE", headers={"X-CSRF-Token": runtime["csrf"]})
            opener.open(request, timeout=5).close()
            assert json.loads(cli("web", "--status", "--json").stdout)["running"]
            assert not json.loads(cli("ui", "--stop", "--json").stdout)["running"]
            reaper.join(timeout=10)
            assert not reaper.is_alive()
            status = cli("web", "--status", "--json", check=False)
            assert not json.loads(status.stdout)["running"]
            assert not (root / "runtime" / "ui-state.json").exists()
        finally:
            if process.poll() is None:
                process.terminate()
                reaper.join(timeout=10)
                if reaper.is_alive():
                    process.kill()
                    reaper.join(timeout=5)
        print(f"{distribution.name}: clean install, CLI/Web, all manifest assets and stop cleanup passed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("distribution", type=Path)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    try:
        run_smoke(args.distribution.resolve(), args.version)
    except urllib.error.HTTPError as error:
        raise SystemExit(f"Packaged HTTP smoke failed: HTTP {error.code} at "
                         + urllib.parse.urlsplit(error.url).path) from None
    except urllib.error.URLError:
        # HTTPError contains the request URL, which may contain a launch token.
        raise SystemExit("Packaged HTTP smoke failed (request details withheld)") from None
