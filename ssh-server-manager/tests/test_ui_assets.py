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
SKILL_JAVASCRIPT = (UI_DIR / "skills.js").read_text(encoding="utf-8")
SKILL_STYLES = (UI_DIR / "skills.css").read_text(encoding="utf-8")
DIAGNOSTICS_JAVASCRIPT = (UI_DIR / "diagnostics.js").read_text(encoding="utf-8")
DIAGNOSTICS_STYLES = (UI_DIR / "diagnostics.css").read_text(encoding="utf-8")
NOTES_JAVASCRIPT = (UI_DIR / "notes.js").read_text(encoding="utf-8")
NOTES_STYLES = (UI_DIR / "notes.css").read_text(encoding="utf-8")
THEME_STYLES = (UI_DIR / "themes.css").read_text(encoding="utf-8")
ACCENTS = ("teal", "emerald", "amber", "rose", "violet", "graphite")


def html_attribute_values(name: str) -> list[str]:
    pattern = rf"\b{re.escape(name)}=(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))"
    return [next(value for value in match if value != "") for match in re.findall(pattern, HTML)]


def test_ui_stays_dependency_free_and_inside_performance_budget():
    assets = {
        "index.html": HTML.encode(),
        "styles.css": STYLES.encode(),
        "app.js": JAVASCRIPT.encode(),
    }

    assert sum(map(len, assets.values())) <= 104_000
    assert sum(len(gzip.compress(value, compresslevel=9, mtime=0)) for value in assets.values()) <= 26_000
    assert "https://" not in HTML + STYLES + JAVASCRIPT
    assert "http://" not in HTML + STYLES + JAVASCRIPT
    assert "@import" not in STYLES
    assert re.search(r"<script\b(?=[^>]*\bdefer\b)(?=[^>]*\bsrc=(?:\"/assets/app\.js\"|/assets/app\.js))", HTML)
    assert "backdrop-filter" not in STYLES


def test_context_tools_are_lazy_and_small():
    assets = [CONTEXT_JAVASCRIPT.encode(), CONTEXT_STYLES.encode()]

    assert sum(map(len, assets)) <= 48_000
    assert sum(len(gzip.compress(value, compresslevel=9, mtime=0)) for value in assets) <= 11_500
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


def test_host_skill_tools_are_scoped_lazy_and_small():
    assets = [SKILL_JAVASCRIPT.encode(), SKILL_STYLES.encode()]

    assert sum(map(len, assets)) <= 48_000
    assert sum(len(gzip.compress(value, compresslevel=9, mtime=0)) for value in assets) <= 11_500
    assert 'import("/assets/skills.js")' in JAVASCRIPT
    assert '<script src="/assets/skills.js"' not in HTML
    assert "export function openSkillManager" in SKILL_JAVASCRIPT
    assert "export function openHostSkillPicker" in SKILL_JAVASCRIPT
    assert "export function syncHostSkills" in SKILL_JAVASCRIPT
    assert "export function syncSkillUI" in SKILL_JAVASCRIPT
    assert "/api/skills/discover" in SKILL_JAVASCRIPT
    assert re.search(r"/api/skills/\$\{[^}]+\}/servers", SKILL_JAVASCRIPT)
    assert re.search(r"/api/servers/\$\{[^}]+\}/skills", SKILL_JAVASCRIPT)
    assert "server_ids" in SKILL_JAVASCRIPT and "skill_ids" in SKILL_JAVASCRIPT
    assert "nothing is installed on the remote host" in SKILL_JAVASCRIPT
    assert "manage-server-skills" in JAVASCRIPT
    assert "Agent Skills for" in SKILL_JAVASCRIPT
    assert "Assign skills" in JAVASCRIPT + SKILL_JAVASCRIPT
    assert "normal trigger rules" in SKILL_JAVASCRIPT
    assert "The Agent will use only the skills attached" not in SKILL_JAVASCRIPT
    assert "Skill Library" in HTML + JAVASCRIPT + SKILL_JAVASCRIPT
    assert "data-sk-empty-copy" in SKILL_JAVASCRIPT
    assert "aria-labelledby" in SKILL_JAVASCRIPT
    assert "Registered name no longer matches this file" in SKILL_JAVASCRIPT
    assert "Fix SKILL.md first" in SKILL_JAVASCRIPT
    assert "HOST CAPABILITIES" not in JAVASCRIPT + SKILL_JAVASCRIPT
    assert "https://" not in SKILL_JAVASCRIPT + SKILL_STYLES
    assert "http://" not in SKILL_JAVASCRIPT + SKILL_STYLES
    assert "@import" not in SKILL_STYLES
    assert "backdrop-filter" not in SKILL_STYLES
    assert re.search(r"@media\s*\((?:max-width:\s*820px|width<=820px)\)", SKILL_STYLES)
    assert re.search(r"@media\s*\((?:max-width:\s*640px|width<=640px)\)", SKILL_STYLES)
    assert re.search(r"@media\s*\(prefers-reduced-motion:\s*reduce\)", SKILL_STYLES)


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


def test_host_skill_summary_is_not_a_repeated_live_region():
    mount = re.search(r"<div\b[^>]*\bid=(?:\"hostSkillsMount\"|hostSkillsMount)[^>]*>", HTML)

    assert mount is not None
    assert "aria-live" not in mount.group(0)


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
        "skillsView",
        "skillManagerMount",
        "hostSkillsMount",
        "credentialsView",
        "credentialSearchInput",
        "credentialKindFilter",
        "serverTags",
        "themeSelect",
        "accentSelect",
    }

    assert required_ids <= set(html_attribute_values("id"))
    assert {'data-view="workspace"', 'data-view="servers"', 'data-view="contexts"', 'data-view="skills"', 'data-view="credentials"'} <= {
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


def test_diagnostics_assets_are_local_and_dependency_free():
    assets = [DIAGNOSTICS_JAVASCRIPT.encode(), DIAGNOSTICS_STYLES.encode()]

    assert sum(map(len, assets)) <= 20_000
    assert sum(len(gzip.compress(value, compresslevel=9, mtime=0)) for value in assets) <= 8_000
    assert "https://" not in DIAGNOSTICS_JAVASCRIPT + DIAGNOSTICS_STYLES
    assert "http://" not in DIAGNOSTICS_JAVASCRIPT + DIAGNOSTICS_STYLES
    assert "host-diagnostics" in DIAGNOSTICS_JAVASCRIPT
    assert "/diagnose" in DIAGNOSTICS_JAVASCRIPT
    assert "backdrop-filter" not in DIAGNOSTICS_STYLES


def test_accent_palettes_cover_every_theme_and_stay_small():
    encoded = THEME_STYLES.encode()

    assert len(encoded) <= 8_000
    assert len(gzip.compress(encoded, compresslevel=9, mtime=0)) <= 1_800
    assert "https://" not in THEME_STYLES
    assert "http://" not in THEME_STYLES
    assert "@import" not in THEME_STYLES
    assert "backdrop-filter" not in THEME_STYLES
    for accent in ACCENTS:
        assert re.search(rf':root\[data-accent=(?:"{accent}"|{accent})\]', THEME_STYLES)
        assert re.search(rf':root\[data-theme=(?:"dark"|dark)\]\[data-accent=(?:"{accent}"|{accent})\]', THEME_STYLES)
        assert re.search(
            rf':root\[data-theme=(?:"contrast"|contrast)\]\[data-accent=(?:"{accent}"|{accent})\]', THEME_STYLES
        )
    # the contrast theme keeps its high-visibility yellow focus ring for every accent
    assert re.search(r':root\[data-theme=(?:"contrast"|contrast)\]\[data-accent\]', THEME_STYLES)
    assert "ssh-manager-accent" in JAVASCRIPT
    assert "dataset.accent" in JAVASCRIPT
    for accent in ACCENTS:
        assert f"{accent}" in HTML


def test_notes_assets_are_local_and_dependency_free():
    assets = [NOTES_JAVASCRIPT.encode(), NOTES_STYLES.encode()]

    assert sum(map(len, assets)) <= 24_000
    assert sum(len(gzip.compress(value, compresslevel=9, mtime=0)) for value in assets) <= 9_000
    assert "https://" not in NOTES_JAVASCRIPT + NOTES_STYLES
    assert "http://" not in NOTES_JAVASCRIPT + NOTES_STYLES
    assert "/notes" in NOTES_JAVASCRIPT
    assert "host-note" in NOTES_JAVASCRIPT
    assert "backdrop-filter" not in NOTES_STYLES
