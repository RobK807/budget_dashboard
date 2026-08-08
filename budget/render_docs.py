"""Render the project's Markdown docs to standalone HTML.

The .html files beside the .md ones were generated with pandoc at the start of the project
and never regenerated, so by the time anyone noticed, README.html described a dashboard that
had moved on considerably. A one-off conversion has that failure built into it; this exists
so refreshing them is a command rather than a job.

Run with:  python -m budget.render_docs          # rewrite every .md as .html
           python -m budget.render_docs --check  # report what is stale, change nothing

`--check` is the useful one in a hurry: it says which pages have drifted without touching
the disk, so a stale file is something you find rather than something you trip over.

Styling is deliberately plain and self-contained -- no CDN, no external stylesheet -- so a
page opens the same from a folder, a NAS share or an email attachment.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Tables and fenced code are both used heavily in these documents, and neither is in the
# base Markdown syntax. 'sane_lists' stops a numbered list restarting at 1 when a paragraph
# interrupts it, which the setup instructions do throughout.
EXTENSIONS = ["extra", "sane_lists", "tables", "fenced_code", "toc", "codehilite"]
EXTENSION_CONFIG = {
    # Inline styles rather than a class-and-stylesheet pair, so the page stays one file.
    "codehilite": {"noclasses": True, "pygments_style": "friendly"},
}

TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{
    font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    line-height: 1.6; max-width: 46rem; margin: 2rem auto; padding: 0 1.25rem;
    color: #1a1a1a; background: #fff;
  }}
  h1, h2, h3, h4 {{ line-height: 1.25; margin-top: 2rem; }}
  h1 {{ border-bottom: 2px solid #e5e5e5; padding-bottom: .3rem; }}
  h2 {{ border-bottom: 1px solid #ececec; padding-bottom: .2rem; }}
  code {{
    font-family: "Cascadia Mono", Consolas, "SF Mono", Menlo, monospace;
    font-size: .9em; background: #f4f4f4; padding: .1em .35em; border-radius: 3px;
  }}
  pre {{ background: #f7f7f7; padding: .85rem 1rem; overflow-x: auto; border-radius: 5px; }}
  pre code {{ background: none; padding: 0; }}
  table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; }}
  th, td {{ border: 1px solid #ddd; padding: .4rem .6rem; text-align: left; }}
  th {{ background: #f4f4f4; }}
  blockquote {{
    border-left: 3px solid #ccc; margin-left: 0; padding-left: 1rem; color: #555;
  }}
  hr {{ border: none; border-top: 1px solid #e5e5e5; margin: 2rem 0; }}
  a {{ color: #0b62c4; }}
  .generated {{ margin-top: 3rem; font-size: .85em; color: #777; }}
  @media (prefers-color-scheme: dark) {{
    body {{ color: #ddd; background: #1b1b1b; }}
    code {{ background: #2a2a2a; }}
    pre {{ background: #222; }}
    th {{ background: #262626; }}
    th, td {{ border-color: #3a3a3a; }}
    h1 {{ border-bottom-color: #3a3a3a; }}
    h2 {{ border-bottom-color: #303030; }}
    a {{ color: #6fb3ff; }}
  }}
</style>
</head>
<body>
{body}
<p class="generated">Generated from <code>{source}</code> on {when}
 — <code>python -m budget.render_docs</code> to refresh.</p>
</body>
</html>
"""


def title_of(text: str, fallback: str) -> str:
    """The first level-one heading, or the filename if the document has none."""
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


def render(path: Path) -> str:
    import markdown

    text = path.read_text(encoding="utf-8")
    body = markdown.markdown(
        text, extensions=EXTENSIONS, extension_configs=EXTENSION_CONFIG
    )
    return TEMPLATE.format(
        title=html.escape(title_of(text, path.stem)),
        body=body,
        source=html.escape(path.name),
        when=f"{dt.date.today():%d %B %Y}",
    )


def sources() -> list[Path]:
    return sorted(p for p in ROOT.glob("*.md") if p.name != "MEMORY.md")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check", action="store_true",
        help="report which pages are missing or out of date, and write nothing",
    )
    args = parser.parse_args(argv)

    try:
        import markdown  # noqa: F401
    except ModuleNotFoundError:
        print(
            "The 'markdown' package is not installed. It is a dev dependency:\n"
            "    .venv\\Scripts\\python.exe -m pip install -r requirements.txt"
        )
        return 1

    stale: list[str] = []
    for source in sources():
        target = source.with_suffix(".html")
        # The generated-on line changes every day, so comparing whole files would call
        # every page stale every day. What matters is whether the *source* has moved since
        # the page was written.
        outdated = (
            not target.exists()
            or target.stat().st_mtime < source.stat().st_mtime
        )
        if args.check:
            if outdated:
                stale.append(source.name)
            continue
        target.write_text(render(source), encoding="utf-8")
        print(f"  {source.name:<24} -> {target.name}")

    if args.check:
        if stale:
            print(f"{len(stale)} page(s) out of date: " + ", ".join(stale))
            return 1
        print(f"All {len(sources())} page(s) up to date.")
        return 0

    print(f"\n{len(sources())} page(s) written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
