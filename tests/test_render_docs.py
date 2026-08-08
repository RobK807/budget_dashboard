"""The HTML copies of the docs, and whether they still match their sources.

Worth a test because of how this went wrong the first time: the .html files were generated
once at the start of the project and never again, so README.html spent months describing a
dashboard that had moved on. Nothing failed, nothing warned -- it was simply wrong, and only
noticed by someone opening it.
"""

from __future__ import annotations

import pytest

from budget import render_docs

pytest.importorskip("markdown")


def test_every_markdown_file_has_a_page():
    for source in render_docs.sources():
        assert source.with_suffix(".html").exists(), (
            f"{source.name} has no .html beside it — run "
            "`python -m budget.render_docs`"
        )


def test_the_pages_are_not_stale():
    """Fails when a .md has been edited without regenerating its .html."""
    assert render_docs.main(["--check"]) == 0, (
        "A document has changed since its page was rendered — run "
        "`python -m budget.render_docs`"
    )


def test_the_title_comes_from_the_first_heading():
    assert render_docs.title_of("# Budget Dashboard\n\nText.", "x") == "Budget Dashboard"


def test_a_document_with_no_heading_falls_back_to_its_name():
    assert render_docs.title_of("Just text.", "NOTES") == "NOTES"


def test_tables_and_fenced_code_survive():
    """Both are used throughout and neither is in base Markdown, so both are extensions
    that have to be switched on rather than things that work by default."""
    source = next(p for p in render_docs.sources() if p.name == "DESIGN.md")
    page = render_docs.render(source)

    assert "<table>" in page
    # `<pre` rather than `<pre>`: codehilite highlights the block and puts the colours
    # inline, so the tag carries a style attribute.
    assert "<pre" in page


def test_the_page_is_self_contained():
    """No CDN and no sibling stylesheet: a page has to open the same from a folder, a NAS
    share or an email attachment."""
    page = render_docs.render(
        next(p for p in render_docs.sources() if p.name == "README.md")
    )

    assert "<style>" in page
    assert "http://" not in page.split("<body>")[0]
    assert "<link" not in page
