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
    """The screens. A leading underscore marks the shell and its partials,
    which are included into a screen rather than served as one."""
    return sorted(p for p in TEMPLATE_DIR.glob("*.html") if not p.name.startswith("_"))


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


class TestAPopoverIsNotClippedByWhateverScrollsAboveIt:
    """The `(i)` panels were cut off in three places at once.

    They sat in a table that scrolls horizontally, in a drawer with its own
    overflow, and near the right edge of the window. `position: absolute`
    loses all three: a child is clipped by the nearest scrolling ancestor
    whatever its z-index, and a left-anchored panel runs off the viewport.
    Only taking it out of the flow fixes it, with the placement computed from
    the button's own rect - which is why this is asserted statically rather
    than left to a class somebody might "tidy up".
    """

    def test_the_panel_escapes_its_container(self) -> None:
        rules = stylesheet(SHELL)
        block = re.search(r"\.info > \.panel \{(.*?)\}", rules, flags=re.DOTALL)
        assert block is not None, "el panel del (i) ya no se declara"
        assert "position: fixed" in block.group(1), (
            "un panel `absolute` lo recorta la tabla con scroll y el cajón del copiloto"
        )

    def test_the_panel_sits_above_the_drawer(self) -> None:
        rules = stylesheet(SHELL)
        panel = re.search(r"\.info > \.panel \{(.*?)\}", rules, flags=re.DOTALL)
        drawer = re.search(r"\.copilot \{(.*?)\}", rules, flags=re.DOTALL)
        assert panel and drawer
        panel_z = int(re.search(r"z-index: (\d+)", panel.group(1)).group(1))
        drawer_z = int(re.search(r"z-index: (\d+)", drawer.group(1)).group(1))
        assert panel_z > drawer_z, "el panel quedaría por debajo del cajón"

    def test_only_one_panel_can_be_open(self) -> None:
        """Several open at once overlapped each other, which is what a reviewer
        reported first."""
        script = SHELL.read_text(encoding="utf-8")
        assert "function closePanels" in script
        assert "closePanels(details)" in script, "abrir uno no cierra los demás"

    def test_the_panel_states_its_own_text_flow(self) -> None:
        """The bug this catches took a measurement to find.

        One `(i)` button sits inside a `<label>` carrying
        `white-space: nowrap`, and the panel inherited it: its paragraphs
        could not wrap and the box ran 963px past its own right edge. A
        popover is placed anywhere in the page, so anything that decides how
        its text flows has to be declared, not inherited.
        """
        block = re.search(r"\.info > \.panel \{(.*?)\}", stylesheet(SHELL), flags=re.DOTALL)
        assert block is not None
        assert "white-space: normal" in block.group(1)

    def test_the_panel_has_no_scrollbars(self) -> None:
        """`overflow-y: auto` alone computes `overflow-x` to `auto` too, so the
        panel grew a horizontal bar - and dragging it scrolled the page, which
        closed the panel. It is a short explanation in a fixed-width box: it
        should be the whole box."""
        block = re.search(r"\.info > \.panel \{(.*?)\}", stylesheet(SHELL), flags=re.DOTALL)
        assert block is not None
        assert "overflow: visible" in block.group(1)
        assert "overflow-y: auto" not in block.group(1)
