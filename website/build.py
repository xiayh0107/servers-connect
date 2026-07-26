#!/usr/bin/env python3
"""Build the published site into _site/.

Two things happen here:

1. website/index.html is authored headless (no doctype/html/head/body) so the
   same file can be published as a Claude artifact. It gets wrapped into a
   complete document. Everything up to and including </style> is head content,
   the rest is body.
2. The markdown under ssh-server-manager/docs and ssh-server-manager/references
   is rendered into a browsable docs section at /docs/ so the site never has to
   hand visitors off to a GitHub file listing.

Usage:  python3 website/build.py [--out _site] [--serve]
Needs:  pip install markdown
"""

from __future__ import annotations

import argparse
import html
import posixpath
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WEBSITE = REPO / "website"
PKG = REPO / "ssh-server-manager"
GITHUB = "https://github.com/xiayh0107/servers-connect"
BLOB = f"{GITHUB}/blob/main"
VERSION = "0.6.0"


@dataclass
class Page:
    """One markdown source rendered to one flat page under /docs/."""

    source: Path
    slug: str
    nav_label: str
    summary: str
    title: str = ""
    body: str = ""
    toc: list = field(default_factory=list)

    @property
    def out_name(self) -> str:
        return f"{self.slug}.html"


# Flat output namespace: every doc lives at /docs/<slug>.html, so cross-links
# between them are plain relative hrefs. references/security.md is renamed to
# avoid colliding with docs/security.md.
SECTIONS: list[tuple[str, list[Page]]] = [
    (
        "Getting started",
        [
            Page(PKG / "docs/quickstart.md", "quickstart", "Quickstart",
                 "Ten minutes from install to a managed fleet."),
            Page(PKG / "docs/installation.md", "installation", "Installation",
                 "Install with pipx, pip, or from a source checkout."),
            Page(PKG / "docs/platforms.md", "platforms", "Platforms",
                 "What the vault and OpenSSH integration look like per OS."),
        ],
    ),
    (
        "Guides",
        [
            Page(PKG / "docs/cli.md", "cli", "CLI",
                 "serverctl commands for hosts, credentials, and sessions."),
            Page(PKG / "docs/web-ui.md", "web-ui", "Web UI",
                 "The loopback management UI and its reveal protections."),
            Page(PKG / "docs/ai-agents.md", "ai-agents", "AI agents",
                 "How an agent uses the tool without ever seeing a secret."),
        ],
    ),
    (
        "Reference",
        [
            Page(PKG / "references/commands.md", "commands", "Command reference",
                 "Every subcommand, flag, and exit code."),
            Page(PKG / "references/data-model.md", "data-model", "Data model",
                 "Where profiles, tags, and vault entries are stored."),
            Page(PKG / "references/troubleshooting.md", "troubleshooting", "Troubleshooting",
                 "Diagnosing connection, vault, and config failures."),
            Page(PKG / "references/security.md", "agent-security", "Agent safety rules",
                 "The rules an agent must follow when driving serverctl."),
        ],
    ),
    (
        "Project",
        [
            Page(PKG / "docs/security.md", "security", "Security model",
                 "Threat model, credential handling, and what is out of scope."),
            Page(PKG / "docs/faq.md", "faq", "FAQ",
                 "Common questions about scope, safety, and behaviour."),
            Page(PKG / "docs/roadmap.md", "roadmap", "Roadmap",
                 "What is shipped and what is being considered next."),
            Page(PKG / "docs/ui-ux-research.md", "ui-ux-research", "UI/UX research",
                 "The evidence behind the host and tag interaction model."),
        ],
    ),
]

PAGES: list[Page] = [p for _, pages in SECTIONS for p in pages]
BY_SOURCE = {p.source.resolve(): p for p in PAGES}


# --------------------------------------------------------------------------- md

def render_markdown(text: str):
    try:
        import markdown
    except ModuleNotFoundError:
        sys.exit("build.py needs the 'markdown' package: pip install markdown")

    md = markdown.Markdown(
        extensions=["extra", "toc", "sane_lists", "admonition"],
        extension_configs={"toc": {"permalink": "#", "permalink_title": "Link to this section"}},
    )
    return md.convert(text), md.toc_tokens


LINK_RE = re.compile(r'(href=")([^"]+)(")')


def rewrite_links(body: str, source: Path) -> str:
    """Point .md links at their rendered page, or at GitHub when unrendered."""

    def sub(match: re.Match) -> str:
        target = match.group(2)
        if not target or target.startswith(("http://", "https://", "#", "mailto:")):
            return match.group(0)

        path, _, anchor = target.partition("#")
        anchor = f"#{anchor}" if anchor else ""
        if not path.endswith(".md"):
            return match.group(0)

        resolved = (source.parent / path).resolve()
        page = BY_SOURCE.get(resolved)
        if page:
            return f'{match.group(1)}{page.out_name}{anchor}{match.group(3)}'
        if resolved.exists():
            rel = resolved.relative_to(REPO).as_posix()
            return f'{match.group(1)}{BLOB}/{rel}{anchor}{match.group(3)}'
        return match.group(0)

    body = LINK_RE.sub(sub, body)
    return prettify_link_text(body)


# Docs cross-reference each other as "[installation.md](installation.md)", which
# reads like a file listing once rendered. Swap bare filename link text for the
# page's nav label.
FILENAME_LINK_RE = re.compile(r'(<a href="([a-z0-9-]+)\.html(?:#[^"]*)?">)([a-z0-9-]+\.md)(</a>)')
BY_SLUG = {p.slug: p for p in PAGES}


def prettify_link_text(body: str) -> str:
    def sub(match: re.Match) -> str:
        page = BY_SLUG.get(match.group(2))
        if not page:
            return match.group(0)
        return f"{match.group(1)}{html.escape(page.nav_label)}{match.group(4)}"

    return FILENAME_LINK_RE.sub(sub, body)


H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S)
TAG_RE = re.compile(r"<[^>]+>")


def load_pages() -> None:
    for page in PAGES:
        if not page.source.exists():
            sys.exit(f"missing doc source: {page.source}")
        body, toc = render_markdown(page.source.read_text(encoding="utf-8"))
        body = rewrite_links(body, page.source)
        match = H1_RE.search(body)
        page.title = TAG_RE.sub("", match.group(1)).strip() if match else page.nav_label
        page.body = body
        # A single top-level H1 is the page title; its children are the sections
        # worth listing in "On this page".
        page.toc = toc[0]["children"] if len(toc) == 1 and toc[0].get("children") else toc


# ------------------------------------------------------------------------ shell

def sidebar(current: str | None) -> str:
    out = ['<nav class="doc-nav" aria-label="Documentation">']
    for title, pages in SECTIONS:
        out.append(f"<p class=\"doc-nav-title\">{html.escape(title)}</p><ul>")
        for page in pages:
            cls = ' class="current"' if page.slug == current else ""
            aria = ' aria-current="page"' if page.slug == current else ""
            out.append(
                f'<li><a href="{page.out_name}"{cls}{aria}>{html.escape(page.nav_label)}</a></li>'
            )
        out.append("</ul>")
    out.append("</nav>")
    return "\n".join(out)


def on_this_page(page: Page) -> str:
    if len(page.toc) < 2:
        return ""
    items = "".join(
        f'<li><a href="#{t["id"]}">{html.escape(TAG_RE.sub("", t["name"]))}</a></li>'
        for t in page.toc
    )
    return f'<aside class="doc-toc"><p class="doc-nav-title">On this page</p><ul>{items}</ul></aside>'


def pager(page: Page) -> str:
    index = PAGES.index(page)
    prev_page = PAGES[index - 1] if index > 0 else None
    next_page = PAGES[index + 1] if index < len(PAGES) - 1 else None
    parts = []
    if prev_page:
        parts.append(
            f'<a class="pager-link" href="{prev_page.out_name}">'
            f'<small>Previous</small><strong>{html.escape(prev_page.nav_label)}</strong></a>'
        )
    else:
        parts.append("<span></span>")
    if next_page:
        parts.append(
            f'<a class="pager-link next" href="{next_page.out_name}">'
            f'<small>Next</small><strong>{html.escape(next_page.nav_label)}</strong></a>'
        )
    return f'<nav class="doc-pager">{"".join(parts)}</nav>'


def shell(*, title: str, description: str, current: str | None, main: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<meta name="description" content="{html.escape(description)}">
<meta name="theme-color" content="#000000">
<link rel="stylesheet" href="docs.css">
</head>
<body>
<header class="header">
  <div class="doc-shell">
    <nav class="nav">
      <a class="brand" href="../">
        <span class="brand-icon">&gt;_</span>
        <span>SSH Server Manager</span>
      </a>
      <div class="nav-links">
        <a href="index.html">Docs</a>
        <a href="{GITHUB}">GitHub</a>
        <a class="btn btn-primary" href="quickstart.html">Get started</a>
      </div>
    </nav>
  </div>
</header>
<div class="doc-shell doc-layout">
{sidebar(current)}
{main}
</div>
<footer class="footer">
  <div class="doc-shell">
    <div class="footer-links">
      <a href="index.html">Documentation</a>
      <a href="../">Home</a>
      <a href="{GITHUB}">GitHub</a>
      <a href="{BLOB}/LICENSE">License</a>
    </div>
    <p>© 2026 SSH Server Manager · v{VERSION} · MIT</p>
  </div>
</footer>
</body>
</html>
"""


def doc_page(page: Page) -> str:
    rel = page.source.relative_to(REPO).as_posix()
    main = f"""<main class="doc-main">
  <article class="doc-article">
{page.body}
  </article>
  {pager(page)}
  <p class="doc-source"><a href="{BLOB}/{rel}">Edit this page on GitHub</a></p>
</main>
{on_this_page(page)}"""
    return shell(
        title=f"{page.title} — SSH Server Manager docs",
        description=page.summary,
        current=page.slug,
        main=main,
    )


def index_page() -> str:
    cards = []
    for title, pages in SECTIONS:
        items = "".join(
            f'<a class="doc-card" href="{p.out_name}">'
            f"<strong>{html.escape(p.nav_label)}</strong>"
            f"<span>{html.escape(p.summary)}</span></a>"
            for p in pages
        )
        cards.append(
            f'<section class="doc-group"><h2>{html.escape(title)}</h2>'
            f'<div class="doc-cards">{items}</div></section>'
        )
    main = f"""<main class="doc-main">
  <div class="doc-hero">
    <p class="eyebrow">Documentation</p>
    <h1>SSH Server Manager</h1>
    <p>A local-first SSH host and credential manager for people and their AI agents.
       Credentials live in the OS keychain; agents get connection context, never secrets.</p>
    <p class="doc-hero-actions">
      <a class="btn btn-primary" href="quickstart.html">Start the quickstart <span aria-hidden="true">→</span></a>
      <a class="btn" href="installation.html">Install</a>
    </p>
  </div>
  {"".join(cards)}
</main>"""
    return shell(
        title="Documentation — SSH Server Manager",
        description="Guides, CLI reference, and the security model for SSH Server Manager.",
        current=None,
        main=main,
    )


# ----------------------------------------------------------------------- output

def build_landing(out: Path) -> None:
    raw = WEBSITE / "index.html"
    text = raw.read_text(encoding="utf-8")
    marker = "</style>"
    split = text.find(marker)
    if split == -1:
        sys.exit("website/index.html has no </style>; cannot split head from body")
    split += len(marker)
    head, body = text[:split], text[split:]
    if "<!doctype" in head.lower() or "<html" in head.lower():
        sys.exit("website/index.html must be headless (no doctype/html/head/body tags)")
    (out / "index.html").write_text(
        f"<!doctype html>\n<html lang=\"en\">\n<head>\n{head}\n</head>\n<body>\n{body}\n</body>\n</html>\n",
        encoding="utf-8",
    )


def build(out: Path) -> None:
    if out.exists():
        shutil.rmtree(out)
    (out / "docs").mkdir(parents=True)

    build_landing(out)
    shutil.copytree(WEBSITE / "assets", out / "assets")

    load_pages()
    docs = out / "docs"
    shutil.copyfile(WEBSITE / "docs.css", docs / "docs.css")
    (docs / "index.html").write_text(index_page(), encoding="utf-8")
    for page in PAGES:
        (docs / page.out_name).write_text(doc_page(page), encoding="utf-8")

    print(f"built {out}/ — 1 landing page, {len(PAGES) + 1} doc pages")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(REPO / "_site"), help="output directory")
    parser.add_argument("--serve", action="store_true", help="serve the result locally")
    parser.add_argument("--port", type=int, default=8000, help="port for --serve (default 8000)")
    args = parser.parse_args()

    out = Path(args.out).resolve()
    build(out)

    if args.serve:
        import functools
        import http.server

        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(out))
        print(f"serving http://localhost:{args.port}/ — Ctrl-C to stop")
        http.server.ThreadingHTTPServer(("127.0.0.1", args.port), handler).serve_forever()


if __name__ == "__main__":
    main()
