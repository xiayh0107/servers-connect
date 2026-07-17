import gzip
import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
WEBSITE_HTML = (REPO_ROOT / "website" / "index.html").read_text(encoding="utf-8")
COLLABORATION_IMAGE = REPO_ROOT / "website" / "assets" / "human-agent-collaboration.webp"


def test_marketing_page_declares_utf8_before_localized_copy():
    assert WEBSITE_HTML.startswith('<meta charset="utf-8">')
    assert '<meta charset="utf-8">' in WEBSITE_HTML[:1024]


def test_marketing_page_stays_on_the_simple_paint_path():
    encoded = WEBSITE_HTML.encode()

    assert len(encoded) <= 70_000
    assert len(gzip.compress(encoded, compresslevel=9, mtime=0)) <= 18_000
    assert "backdrop-filter" not in WEBSITE_HTML
    assert "color-mix(" not in WEBSITE_HTML
    assert "<svg" not in WEBSITE_HTML


def test_marketing_page_does_not_intercept_context_menu():
    assert "contextmenu" not in WEBSITE_HTML


def test_marketing_page_has_copyable_agent_prompt():
    assert 'id="agentInstallCommand"' in WEBSITE_HTML
    assert 'id="copyAgentCommand"' in WEBSITE_HTML
    assert "navigator.clipboard.writeText(text)" in WEBSITE_HTML


def test_marketing_page_leads_with_the_human_agent_workflow():
    assert "Human + Agent SSH workspace" in WEBSITE_HTML
    assert "One shared SSH workspace." in WEBSITE_HTML
    assert "01 / INTENT" in WEBSITE_HTML
    assert "02 / ACTION" in WEBSITE_HTML
    assert "03 / REVIEW" in WEBSITE_HTML
    assert 'src="assets/human-agent-collaboration.webp"' in WEBSITE_HTML
    assert COLLABORATION_IMAGE.is_file()
    assert COLLABORATION_IMAGE.stat().st_size <= 100_000


def test_marketing_page_is_an_interactive_product_demo():
    views = set(re.findall(r'data-demo-view="([^"]+)"', WEBSITE_HTML))
    panels = set(re.findall(r'data-demo-panel="([^"]+)"', WEBSITE_HTML))

    assert views == panels == {"workspace", "connections", "tags", "credentials"}
    assert 'id="demoShell"' in WEBSITE_HTML
    assert 'id="demoConnectionRows"' in WEBSITE_HTML
    assert 'id="demoFileRows"' in WEBSITE_HTML
    assert 'id="tagCreateForm"' in WEBSITE_HTML
    assert 'id="agentDialog"' in WEBSITE_HTML


def test_language_switch_does_not_hide_the_document_root():
    assert '\n  [lang="zh-CN"] { display: none; }' not in WEBSITE_HTML
    assert 'body [lang="zh-CN"] { display: none; }' in WEBSITE_HTML
    assert 'root.lang = "zh-CN"' in WEBSITE_HTML


def test_marketing_page_uses_clearly_labeled_sample_data():
    host_block = WEBSITE_HTML.split("var initialHosts = [", 1)[1].split("];", 1)[0]

    assert host_block.count('{ id: "') == 11
    assert "Sample data" in WEBSITE_HTML
    assert "Nothing connects to a real machine." in WEBSITE_HTML
