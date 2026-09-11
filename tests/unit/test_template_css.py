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


REPORT = TEMPLATE_DIR.parent.parent / "reporting" / "templates" / "report.html"


class TestTheFiledReportPrintsWithItsColours:
    """A printed report came out flat: the confidence marker, the state tags
    and the severity bands all appeared as plain text.

    The cause is not in the stylesheet's colours - they are all defined for
    print - but in the print dialogue, which has "Background graphics" off by
    default and drops every fill. `print-color-adjust: exact` is what asks for
    them back, and it is the one declaration whose removal is invisible until
    somebody prints the thing.
    """

    def test_the_report_asks_for_its_backgrounds(self) -> None:
        rules = stylesheet(REPORT)
        assert "print-color-adjust: exact" in rules
        assert "-webkit-print-color-adjust: exact" in rules

    def test_the_field_tables_have_fixed_columns(self) -> None:
        """Without them the personnel section's JSON paths widen their column
        until "Estado" runs off the sheet - which is how a printed report
        ended up reading "Leído por la má"."""
        rules = stylesheet(REPORT)
        block = re.search(r"table\.fields \{(.*?)\}", rules, flags=re.DOTALL)
        assert block is not None, "las tablas de campos ya no se declaran"
        assert "table-layout: fixed" in block.group(1)
        for column in ("f-field", "f-value", "f-conf", "f-where", "f-state"):
            assert f"col.{column}" in rules, column

    def test_every_state_is_also_a_word(self) -> None:
        """So a genuinely monochrome printer still reads correctly: the tags
        carry text, not only a colour."""
        from iep.api.vocabulary import FIELD_STATUS

        for label, _ in FIELD_STATUS.values():
            assert label.strip(), FIELD_STATUS


class TestEveryStateTheVocabularyCanEmitIsStyled:
    """The defect this catches was invisible on screen for the whole project.

    `vocabulary.py` hands templates the long class name - `neutral`,
    `warning`, `ok`, `blocker`, `muted` - and the shell's stylesheet only
    defined the one-letter codes `n b w o`. So `<span class="pill warning">`
    matched `.pill` and nothing else: transparent background, transparent
    border, and a label that read as coloured text rather than a tag. The
    report's stylesheet happened to define both spellings, which is why the
    same state looked like a tag in the filed PDF and not on the screen.
    """

    def state_classes(self) -> set[str]:
        """The two tables whose second element is a class name.

        `DOSSIER_STATUS` and `DOCUMENT_STATUS` also hold pairs, but theirs is
        a description a screen prints - not a selector. Reading them as
        classes is how the first version of this test asked the stylesheet to
        define `.pill.Creado, todavía sin documentos`.
        """
        from iep.api.vocabulary import FIELD_STATUS, FINDING_STATUS

        return {
            str(value[1]) for table in (FIELD_STATUS, FINDING_STATUS) for value in table.values()
        }

    def test_the_vocabulary_names_some_states(self) -> None:
        """A guard on the guard."""
        names = self.state_classes()
        assert {"neutral", "warning", "ok"} <= names, names

    @pytest.mark.parametrize("sheet", ["shell", "report"])
    def test_each_one_has_a_rule(self, sheet: str) -> None:
        path = SHELL if sheet == "shell" else REPORT
        rules = stylesheet(path)
        declared = set(re.findall(r"\.pill\.([\w-]+)", rules))
        missing = sorted(self.state_classes() - declared)
        assert missing == [], (
            f"{path.name} no estiliza {missing}: esas píldoras salen "
            f"transparentes y el estado se lee como texto"
        )
