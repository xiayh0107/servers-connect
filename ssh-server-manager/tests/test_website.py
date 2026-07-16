from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
WEBSITE_HTML = (REPO_ROOT / "website" / "index.html").read_text(encoding="utf-8")


def test_marketing_page_stays_on_the_simple_paint_path():
    assert "backdrop-filter" not in WEBSITE_HTML
    assert "color-mix(" not in WEBSITE_HTML


def test_marketing_page_does_not_intercept_context_menu():
    assert "contextmenu" not in WEBSITE_HTML
    assert "preventDefault" not in WEBSITE_HTML


def test_marketing_page_has_copyable_agent_prompt():
    assert 'id="agentInstallCommand"' in WEBSITE_HTML
    assert 'id="copyAgentCommand"' in WEBSITE_HTML
    assert "navigator.clipboard.writeText(text)" in WEBSITE_HTML


def test_marketing_page_is_a_single_focused_view():
    assert "<section" not in WEBSITE_HTML
    assert "<table" not in WEBSITE_HTML
    assert 'id="agentDialog"' in WEBSITE_HTML


def test_language_switch_does_not_hide_the_document_root():
    assert '\n  [lang="zh-CN"] { display: none; }' not in WEBSITE_HTML
    assert 'body [lang="zh-CN"] { display: none; }' in WEBSITE_HTML
    assert 'root.lang = "zh-CN"' in WEBSITE_HTML


def test_marketing_page_shows_real_ui_preview():
    screenshot = REPO_ROOT / "website" / "assets" / "ssh-server-manager-ui.png"

    assert screenshot.is_file()
    assert 'src="assets/ssh-server-manager-ui.png"' in WEBSITE_HTML
