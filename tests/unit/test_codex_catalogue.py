"""The codex half of the picker, which used to be one entry saying "whichever".

The screen let a reviewer choose between Haiku, Sonnet and Opus by name, and
then offered "El configurado en Codex" - a choice between one thing, with no
way to tell what it was. The justification in the code was that the list was
not enumerable from here. It is: the CLI ships an app-server whose
`model/list` returns the same catalogue its interactive `/model` picker draws.

No subprocess runs in these tests. What is tested is the mapping and the
failure behaviour, because the failure behaviour is the part that matters -
a discovery that breaks must not remove a working backend from the screen.
"""

from __future__ import annotations

import pytest

from iep.retrieval import catalogue
from iep.retrieval.codex_appserver import CodexModel, _models


@pytest.fixture(autouse=True)
def _no_cached_answer() -> None:
    catalogue.forget_codex_models()


# What the app-server actually replied on this machine, trimmed to the fields
# that are read. Kept verbatim so a protocol change shows up as a test failure
# rather than as an empty dropdown.
REPLY = {
    "data": [
        {
            "id": "gpt-6-astra",
            "displayName": "GPT-6-Astra",
            "description": "Our most capable model for complex, demanding work.",
            "hidden": False,
            "defaultReasoningEffort": "low",
        },
        {
            "id": "gpt-5.6-luna",
            "displayName": "GPT-5.6-Luna",
            "description": "Fast and affordable agentic coding model.",
            "hidden": False,
            "defaultReasoningEffort": "medium",
        },
    ],
    "nextCursor": None,
}


class TestReadingTheReply:
    def test_the_documented_shape_is_parsed(self) -> None:
        models = _models(REPLY)
        assert [model.id for model in models] == ["gpt-6-astra", "gpt-5.6-luna"]
        assert models[0].display_name == "GPT-6-Astra"
        assert models[1].default_effort == "medium"

    def test_a_hidden_model_is_not_offered(self) -> None:
        """`hidden` exists because the vendor does not want it in a picker."""
        reply = {"data": [{**REPLY["data"][0], "hidden": True}, REPLY["data"][1]]}
        assert [model.id for model in _models(reply)] == ["gpt-5.6-luna"]

    def test_an_entry_without_an_id_is_skipped(self) -> None:
        reply = {"data": [{"displayName": "sin id"}, REPLY["data"][0]]}
        assert [model.id for model in _models(reply)] == ["gpt-6-astra"]

    def test_a_missing_display_name_falls_back_to_the_id(self) -> None:
        """Better a raw id than a blank row in a dropdown."""
        assert _models({"data": [{"id": "gpt-x"}]})[0].display_name == "gpt-x"

    def test_a_reply_that_is_not_the_documented_shape_yields_nothing(self) -> None:
        for reply in ({}, {"data": None}, {"data": "gpt-6"}, {"data": [None, 7]}):
            assert _models(reply) == []  # type: ignore[arg-type]


class TestWhatThePickerOffers:
    def test_every_discovered_model_becomes_a_choice(self) -> None:
        found = [
            CodexModel("gpt-5.6-luna", "GPT-5.6-Luna", "Fast and affordable.", "medium"),
            CodexModel("gpt-5.5", "GPT-5.5", "Previous generation.", "high"),
        ]
        choices = catalogue.codex_models(discover=lambda: found)

        assert [choice.id for choice in choices] == ["codex:gpt-5.6-luna", "codex:gpt-5.5"]
        assert choices[0].label == "GPT-5.6-Luna · codex"
        assert choices[0].model == "gpt-5.6-luna"
        # The note carries the vendor's own words plus the effort, in Spanish.
        assert "Fast and affordable." in choices[0].note
        assert "esfuerzo medio" in choices[0].note
        assert "esfuerzo alto" in choices[1].note
        # Nothing here runs locally, whatever the plan pays for.
        assert not any(choice.local for choice in choices)

    def test_an_unknown_effort_is_left_out_rather_than_guessed(self) -> None:
        found = [CodexModel("gpt-x", "GPT-X", "Una descripción.", "turbo")]
        note = catalogue.codex_models(discover=lambda: found)[0].note
        assert note == "Una descripción."

    def test_a_failed_discovery_keeps_the_backend_on_the_screen(self) -> None:
        """The CLI works whether or not we can read its inventory.

        Returning nothing would delete a usable backend from the picker
        because of a failure in a feature its authors call experimental.
        """
        choices = catalogue.codex_models(discover=lambda: [])

        assert len(choices) == 1
        assert choices[0].id == "codex"
        # No model id at all, so `--model` is never sent and the CLI uses its
        # own default. This entry cannot name a model that does not exist.
        assert choices[0].model == ""

    def test_a_discovery_that_raises_is_a_failed_discovery(self) -> None:
        def boom() -> list[CodexModel]:
            raise RuntimeError("the app-server is experimental")

        assert catalogue.codex_models(discover=boom)[0].id == "codex"

    def test_the_answer_is_remembered_rather_than_re_asked(self) -> None:
        """The status endpoint is polled, and each ask is a process and ~2 s."""
        calls = 0

        def counted() -> list[CodexModel]:
            nonlocal calls
            calls += 1
            return [CodexModel("gpt-5.5", "GPT-5.5", "", "medium")]

        catalogue.codex_models(discover=counted)
        catalogue.codex_models(discover=counted)
        assert calls == 1

        catalogue.forget_codex_models()
        catalogue.codex_models(discover=counted)
        assert calls == 2


class TestTheIdsAreStillTheSecurityBoundary:
    def test_a_discovered_id_resolves_and_reaches_the_model_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A client-supplied name ends up in `argv`, so it has to come back
        out of the catalogue rather than be trusted."""
        from iep.config import Settings
        from iep.retrieval.rag_cli import TOOLS

        monkeypatch.setattr(catalogue, "ollama_models", lambda _settings: [])
        monkeypatch.setattr(
            "iep.retrieval.rag_cli.available_tools",
            lambda: {"claude": False, "codex": True},
        )
        monkeypatch.setattr(
            catalogue,
            "list_models",
            lambda: [CodexModel("gpt-5.6-luna", "GPT-5.6-Luna", "", "medium")],
        )
        catalogue.forget_codex_models()

        chosen = catalogue.resolve(Settings(), "codex:gpt-5.6-luna")
        assert chosen is not None
        argv = TOOLS["codex"].argv(model=chosen.model, instructions="irrelevante")
        assert "--model" in argv
        assert argv[argv.index("--model") + 1] == "gpt-5.6-luna"

    def test_an_id_the_server_did_not_offer_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from iep.config import Settings

        monkeypatch.setattr(catalogue, "ollama_models", lambda _settings: [])
        monkeypatch.setattr(
            "iep.retrieval.rag_cli.available_tools",
            lambda: {"claude": False, "codex": True},
        )
        monkeypatch.setattr(
            catalogue,
            "list_models",
            lambda: [CodexModel("gpt-5.6-luna", "GPT-5.6-Luna", "", "medium")],
        )
        catalogue.forget_codex_models()

        assert catalogue.resolve(Settings(), "codex:gpt-9-does-not-exist") is None
        assert catalogue.resolve(Settings(), "codex:--dangerous-flag") is None
