import gzip
import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
WEBSITE_HTML = (REPO_ROOT / "website" / "index.html").read_text(encoding="utf-8")


def test_marketing_page_declares_utf8_before_localized_copy():
    assert WEBSITE_HTML.startswith('<meta charset="utf-8">')
    assert '<meta charset="utf-8">' in WEBSITE_HTML[:1024]


def test_marketing_page_stays_on_the_simple_paint_path():
    encoded = WEBSITE_HTML.encode()

    # The scenario demo stays lightweight: no framework, SVG, or additional asset.
    assert len(encoded) <= 90_000
    assert len(gzip.compress(encoded, compresslevel=9, mtime=0)) <= 21_000
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
    hero = WEBSITE_HTML.split('<section class="hero"', 1)[1].split("</section>", 1)[0]

    assert "Human + Agent SSH workspace" in hero
    assert "SSH workspace." in hero
    assert "You and your Agent." in hero
    # The hero sells the split of duties, not a feature list.
    assert "not secrets" in hero
    # It routes to the three things a visitor can do next: read, install, try.
    assert 'href="docs/quickstart.html"' in hero
    assert "open-agent-dialog" in hero
    assert 'href="#demo"' in hero


def test_marketing_page_is_bilingual_throughout():
    # Every section a visitor reads carries both languages; the demo shell is
    # sample data and stays English.
    for marker in ('<section class="hero"', 'class="demo-intro"', '<section class="features"', '<section class="cta"'):
        section = WEBSITE_HTML.split(marker, 1)[1].split("</section>", 1)[0]
        assert 'lang="en"' in section, marker
        assert 'lang="zh-CN"' in section, marker

    dialog = WEBSITE_HTML.split("<dialog", 1)[1].split("</dialog>", 1)[0]
    assert 'lang="en"' in dialog
    assert 'lang="zh-CN"' in dialog

    assert 'id="langToggle"' in WEBSITE_HTML
    assert 'localStorage.setItem("ssm-lang"' in WEBSITE_HTML


def test_marketing_page_is_an_interactive_product_demo():
    views = set(re.findall(r'data-demo-view="([^"]+)"', WEBSITE_HTML))
    panels = set(re.findall(r'data-demo-panel="([^"]+)"', WEBSITE_HTML))

    assert views == panels == {"workspace", "connections", "tags", "skills", "credentials"}
    assert 'id="demoShell"' in WEBSITE_HTML
    assert 'id="demoConnectionRows"' in WEBSITE_HTML
    assert 'id="demoFileRows"' in WEBSITE_HTML
    assert 'id="tagCreateForm"' in WEBSITE_HTML
    # v0.5.0 visuals: per-tag categorical colors and the accent switcher
    assert 'id="demoAccentButton"' in WEBSITE_HTML
    assert "function tagHue" in WEBSITE_HTML
    for hue in range(6):
        assert f".tg-{hue}" in WEBSITE_HTML
    assert 'id="agentDialog"' in WEBSITE_HTML


def test_marketing_demo_shows_host_scoped_skill_workflow():
    assert "＋ Assign skills" in WEBSITE_HTML
    assert 'data-edit-host-skills="' in WEBSITE_HTML
    assert 'id="workspaceSkills"' in WEBSITE_HTML
    assert "Agent Skills for " in WEBSITE_HTML
    assert 'id="demoSkillLibrary"' in WEBSITE_HTML
    assert "Discover and register reusable skills" in WEBSITE_HTML
    assert "data-register-skill" in WEBSITE_HTML
    assert "yulab-gpu-node" in WEBSITE_HTML


def test_language_switch_does_not_hide_the_document_root():
    assert '\n  [lang="zh-CN"] { display: none; }' not in WEBSITE_HTML
    assert 'body [lang="zh-CN"] { display: none; }' in WEBSITE_HTML
    assert 'root.lang = "zh-CN"' in WEBSITE_HTML


def test_marketing_page_uses_clearly_labeled_sample_data():
    host_block = WEBSITE_HTML.split("var initialHosts = [", 1)[1].split("];", 1)[0]

    assert host_block.count('{ id: "') == 11
    assert "Sample data" in WEBSITE_HTML
    assert "Nothing connects to a real machine." in WEBSITE_HTML
