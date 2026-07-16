import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
WEBSITE_HTML = (REPO_ROOT / "website" / "index.html").read_text(encoding="utf-8")


def test_sticky_header_stays_on_normal_paint_path():
    header_rule = re.search(r"\bheader\s*\{(?P<body>.*?)\}", WEBSITE_HTML, re.DOTALL)

    assert header_rule is not None
    declarations = header_rule.group("body")
    assert "backdrop-filter" not in declarations
    assert "color-mix(" not in declarations


def test_marketing_page_does_not_intercept_context_menu():
    assert "contextmenu" not in WEBSITE_HTML
    assert "preventDefault" not in WEBSITE_HTML


def test_marketing_page_has_copyable_agent_install_command():
    assert 'id="agentInstallCommand"' in WEBSITE_HTML
    assert 'id="copyAgentCommand"' in WEBSITE_HTML
    assert "navigator.clipboard.writeText(installCommand)" in WEBSITE_HTML


def test_marketing_page_shows_real_ui_preview():
    screenshot = REPO_ROOT / "website" / "assets" / "ssh-server-manager-ui.png"

    assert screenshot.is_file()
    assert 'src="assets/ssh-server-manager-ui.png"' in WEBSITE_HTML
