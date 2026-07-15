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
