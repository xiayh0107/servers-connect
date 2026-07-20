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
    assert "Human + Agent SSH workspace" in WEBSITE_HTML
    assert "One shared SSH workspace." in WEBSITE_HTML
    assert "01 / INTENT" in WEBSITE_HTML
    assert "02 / ACTION" in WEBSITE_HTML
    assert "03 / REVIEW" in WEBSITE_HTML
    assert 'src="assets/human-agent-collaboration.webp"' in WEBSITE_HTML
    assert COLLABORATION_IMAGE.is_file()
    assert COLLABORATION_IMAGE.stat().st_size <= 100_000


def test_marketing_page_has_bilingual_scenario_demos():
    scenario_targets = set(re.findall(r'data-scenario-target="([^"]+)"', WEBSITE_HTML))
    scenario_panels = set(re.findall(r'data-scenario-panel="([^"]+)"', WEBSITE_HTML))
    scenarios = {"direct-connect", "incident-response", "research-compute", "multi-environment"}

    assert 'id="use-cases"' in WEBSITE_HTML
    assert 'aria-labelledby="useCaseTitle"' in WEBSITE_HTML
    assert scenario_targets == scenario_panels == scenarios
    for scenario in scenarios:
        panel = WEBSITE_HTML.split(f'data-scenario-panel="{scenario}"', 1)[1].split("</article>", 1)[0]
        assert 'lang="en"' in panel
        assert 'lang="zh-CN"' in panel
    scenario_section = WEBSITE_HTML.split('id="use-cases"', 1)[1].split('id="demo"', 1)[0]
    assert "serverctl " not in scenario_section
    assert "不讲命令" not in scenario_section
    assert "连接 atlas-prod" in scenario_section
    assert "data-scenario-target" in scenario_section
    assert "function setScenario" in WEBSITE_HTML
    assert 'scenarioSwitcher.addEventListener("click"' in WEBSITE_HTML


def test_marketing_page_is_an_interactive_product_demo():
    views = set(re.findall(r'data-demo-view="([^"]+)"', WEBSITE_HTML))
    panels = set(re.findall(r'data-demo-panel="([^"]+)"', WEBSITE_HTML))

    assert views == panels == {"workspace", "connections", "tags", "credentials"}
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


def test_language_switch_does_not_hide_the_document_root():
    assert '\n  [lang="zh-CN"] { display: none; }' not in WEBSITE_HTML
    assert 'body [lang="zh-CN"] { display: none; }' in WEBSITE_HTML
    assert 'root.lang = "zh-CN"' in WEBSITE_HTML


def test_marketing_page_uses_clearly_labeled_sample_data():
    host_block = WEBSITE_HTML.split("var initialHosts = [", 1)[1].split("];", 1)[0]

    assert host_block.count('{ id: "') == 11
    assert "Sample data" in WEBSITE_HTML
    assert "Nothing connects to a real machine." in WEBSITE_HTML
