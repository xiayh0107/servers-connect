from __future__ import annotations

import gzip
import re
from pathlib import Path


UI_DIR = Path(__file__).resolve().parents[1] / "scripts" / "ssh_server_manager" / "assets" / "ui"
HTML = (UI_DIR / "index.html").read_text(encoding="utf-8")
JAVASCRIPT = (UI_DIR / "app.js").read_text(encoding="utf-8")
STYLES = (UI_DIR / "styles.css").read_text(encoding="utf-8")
CONTEXT_JAVASCRIPT = (UI_DIR / "contexts.js").read_text(encoding="utf-8")
CONTEXT_STYLES = (UI_DIR / "contexts.css").read_text(encoding="utf-8")


def html_attribute_values(name: str) -> list[str]:
    pattern = rf"\b{re.escape(name)}=(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))"
    return [next(value for value in match if value != "") for match in re.findall(pattern, HTML)]


def test_ui_stays_dependency_free_and_inside_performance_budget():
    assets = {
        "index.html": HTML.encode(),
        "styles.css": STYLES.encode(),
        "app.js": JAVASCRIPT.encode(),
    }

    assert sum(map(len, assets.values())) <= 100_000
    assert sum(len(gzip.compress(value, compresslevel=9, mtime=0)) for value in assets.values()) <= 25_000
    assert "https://" not in HTML + STYLES + JAVASCRIPT
    assert "http://" not in HTML + STYLES + JAVASCRIPT
    assert "@import" not in STYLES
    assert re.search(r"<script\b(?=[^>]*\bdefer\b)(?=[^>]*\bsrc=(?:\"/assets/app\.js\"|/assets/app\.js))", HTML)
    assert "backdrop-filter" not in STYLES


def test_context_tools_are_lazy_and_small():
    assets = [CONTEXT_JAVASCRIPT.encode(), CONTEXT_STYLES.encode()]

    assert sum(map(len, assets)) <= 48_000
    assert sum(len(gzip.compress(value, compresslevel=9, mtime=0)) for value in assets) <= 11_000
    assert 'import("/assets/contexts.js")' in JAVASCRIPT
    assert '<script src="/assets/contexts.js"' not in HTML
    assert "export function openContextManager" in CONTEXT_JAVASCRIPT
    assert "export function enhanceTagPicker" in CONTEXT_JAVASCRIPT
    assert "export function enhanceConnections" in CONTEXT_JAVASCRIPT
    assert "export function openHostContextPicker" in CONTEXT_JAVASCRIPT
    assert "export function focusContextCreation" in CONTEXT_JAVASCRIPT
    assert "https://" not in CONTEXT_JAVASCRIPT + CONTEXT_STYLES
    assert "http://" not in CONTEXT_JAVASCRIPT + CONTEXT_STYLES
    assert "@import" not in CONTEXT_STYLES


def test_context_workflows_are_resource_first_and_explicit():
    assert "manageContextsButton" not in HTML
    assert 'showView("contexts")' in CONTEXT_JAVASCRIPT
    assert "Edit tags" in CONTEXT_JAVASCRIPT
    assert "Context library" not in CONTEXT_JAVASCRIPT
    assert "Contexts for" not in CONTEXT_JAVASCRIPT
    assert ">Tags<" in HTML
    assert "Create “" in CONTEXT_JAVASCRIPT
    assert "data-edit-contexts" in JAVASCRIPT
    assert "data-sidebar-contexts" in JAVASCRIPT
    assert re.search(r"server_ids:\[\.\.\.[A-Za-z_$]", CONTEXT_JAVASCRIPT)
    assert "Select all visible hosts" in CONTEXT_JAVASCRIPT
    assert "Test selected" in CONTEXT_JAVASCRIPT
    assert "data-cm-rename" in CONTEXT_JAVASCRIPT
    assert "bulk-add" not in CONTEXT_JAVASCRIPT
    assert "bulk-remove" not in CONTEXT_JAVASCRIPT
    assert "showModal" not in CONTEXT_JAVASCRIPT


def test_periodic_status_refresh_restores_enhanced_connection_rows():
    interval_callback = re.search(r"setInterval\(([A-Za-z_$][A-Za-z0-9_$]*),3e4\)", JAVASCRIPT)

    assert interval_callback is not None
    callback_body = re.search(
        rf"function {re.escape(interval_callback.group(1))}\(\)\{{([^}}]+)\}}",
        JAVASCRIPT,
    )
    assert callback_body is not None
    assert "syncContextUI" in callback_body.group(1)


def test_ui_javascript_only_targets_existing_unique_ids():
    html_ids = html_attribute_values("id")
    javascript_ids = set(re.findall(r'["\']#([A-Za-z][A-Za-z0-9_-]*)["\']', JAVASCRIPT))

    assert len(html_ids) == len(set(html_ids))
    assert javascript_ids <= set(html_ids)


def test_workspace_first_shell_has_navigation_and_responsive_states():
    required_ids = {
        "hostNavigation",
        "labelNavigation",
        "sidebarContextList",
        "sidebarContextForm",
        "hostContextSelect",
        "contextFilterControl",
        "hostSearchInput",
        "workspaceHostList",
        "fileBrowserPanel",
        "fileServerSelect",
        "fileBreadcrumbs",
        "filePathInput",
        "fileSearchInput",
        "fileListMore",
        "fileLoadMoreButton",
        "fileRows",
        "fileShowHidden",
        "fileBrowserError",
        "copyVisibleReferencesButton",
        "emptyChooseHostButton",
        "emptyImportButton",
        "recentHostRows",
        "viewAllHostsButton",
        "serversView",
        "connectionToolbarMount",
        "summaryReachableHosts",
        "contextsView",
        "contextManagerMount",
        "credentialsView",
        "credentialSearchInput",
        "credentialKindFilter",
        "serverTags",
        "themeSelect",
    }

    assert required_ids <= set(html_attribute_values("id"))
    assert {'data-view="workspace"', 'data-view="servers"', 'data-view="contexts"', 'data-view="credentials"'} <= {
        f'data-view="{view}"' for view in html_attribute_values("data-view")
    }
    assert "name" in html_attribute_values("data-sort")
    assert "localeCompare" in JAVASCRIPT
    assert "last_test_at" in JAVASCRIPT and "12e4" in JAVASCRIPT
    assert "Last checked" in JAVASCRIPT
    assert "tag:" in JAVASCRIPT
    assert "contexts" in JAVASCRIPT
    assert re.search(r':root\[data-theme=(?:"dark"|dark)\]', STYLES)
    assert re.search(r':root\[data-theme=(?:"contrast"|contrast)\]', STYLES)
    assert "Owner:" in JAVASCRIPT and "Group:" in JAVASCRIPT
    assert re.search(r"@media\s*\((?:max-width:\s*820px|width<=820px)\)", STYLES)
    assert re.search(r"@media\s*\((?:max-width:\s*640px|width<=640px)\)", STYLES)
    assert re.search(r"@media\s*\(prefers-reduced-motion:\s*reduce\)", STYLES)
