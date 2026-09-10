"""Page stylesheets must not redefine a class the shell uses.

Every screen extends one shell, which owns a set of component names. When a
page declares one of those names *unscoped*, the page's rule applies to the
shell's own elements on that screen - and since it usually sets a property the
shell never set, there is no specificity contest to lose. The shell simply
breaks, on one screen, silently.

It has happened twice. `.hint` was `label.field > .hint` in the shell and a
popover reused the name. `.mark` was the brand's inline SVG, and the evidence
viewer's `.mark { position: absolute }` took the logo out of flow so it
printed on top of the wordmark - visible only by looking at a screenshot.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "src" / "iep" / "api" / "templates"
SHELL = TEMPLATE_DIR / "_base.html"

# A selector that is nothing but a class, possibly with further classes or
# pseudo-classes attached: `.card`, `.pill.n`, `.btn:hover`. Anything with a
# space, a `>` or an element in front of it is scoped and cannot reach the
# shell's elements - `.brand .mark` and `label.field > .hint` are fine.
_BARE_CLASS = re.compile(r"^\.([A-Za-z][\w-]*)(?:[.:][\w()-]+)*$")
_ANY_CLASS = re.compile(r"\.([A-Za-z][\w-]*)")


def stylesheet(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    blocks = re.findall(r"<style>(.*?)</style>", text, flags=re.DOTALL)
    # A comment sits between a `}` and the next selector, so it has to go
    # before the stylesheet is split on braces.
    return re.sub(r"/\*.*?\*/", "", "\n".join(blocks), flags=re.DOTALL)


def declared_unscoped(css: str) -> set[str]:
    """Classes the stylesheet claims without scoping them to anything."""
    found: set[str] = set()
    for rule in css.split("}"):
        selector_list, _, _ = rule.partition("{")
        for selector in selector_list.split(","):
            match = _BARE_CLASS.match(selector.strip())
            if match is not None:
                found.add(match.group(1))
    return found


def shell_vocabulary() -> set[str]:
    """Every class name the shell uses, however it is scoped.

    Read from the shell rather than kept as a list here, so a component added
    to it is protected without anybody remembering to come back.
    """
    text = SHELL.read_text(encoding="utf-8")
    names = set(_ANY_CLASS.findall(stylesheet(SHELL)))
    for attribute in re.findall(r'class="([^"{}]+)"', text):
        names.update(attribute.split())
    return names


def pages() -> list[Path]:
    return sorted(p for p in TEMPLATE_DIR.glob("*.html") if p.name != "_base.html")


class TestNoPageRedefinesAShellClass:
    def test_the_shell_has_a_vocabulary_to_protect(self) -> None:
        """A guard on the guard: were this empty, the test below would pass by
        having nothing to collide with."""
        vocabulary = shell_vocabulary()
        for name in ("card", "pill", "btn", "info", "panel", "note", "mark", "brand"):
            assert name in vocabulary, name

    @pytest.mark.parametrize("page", pages(), ids=lambda p: p.name)
    def test_a_page_does_not_take_over_a_shell_class(self, page: Path) -> None:
        clashes = sorted(declared_unscoped(stylesheet(page)) & shell_vocabulary())
        assert clashes == [], (
            f"{page.name} declara {clashes} sin acotarlo, así que en esa "
            f"pantalla alcanza a los elementos del shell y lo rompe sólo ahí"
        )

    def test_every_screen_is_covered(self) -> None:
        assert {p.name for p in pages()} == {
            "queue.html",
            "new.html",
            "progress.html",
            "review.html",
            "evidence.html",
        }
