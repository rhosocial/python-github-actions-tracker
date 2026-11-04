# tests/adapters/test_log_search.py
"""Whole-log search: one call, all matches, no paging.

``get_job_log`` answers "what does the log look like here"; ``search_job_log``
answers "where does X appear". The whole log is one stored row, so the second
question costs one database read instead of a paging loop.

The property that makes the two tools composable: the line numbers are
absolute. Any number this reports can be handed to ``get_job_log`` as
``from_line`` and lands on the same line, so a caller never has to count.
"""

from __future__ import annotations

from rhosocial.github_ci_tracker.adapters.mcp.logs import search_log

DOCUMENT = "\n".join(
    [
        "line 1 setup",
        "line 2 setup",
        "line 3 ModuleNotFoundError: rhosocial.activerecord.base.ddl",
        "line 4 setup",
        "line 5 failure",
        "line 6 ModuleNotFoundError: elsewhere",
        "line 7 failure",
        "line 8 setup",
        "line 9 done",
    ]
)


class TestSearchLog:
    def test_all_matches_are_reported_with_absolute_line_numbers(self) -> None:
        found = search_log(DOCUMENT, "ModuleNotFoundError", context=0)

        assert found.total_matches == 2
        assert found.matches == [3, 6]
        assert found.total_lines == 9

    def test_context_covers_the_last_matches_by_default(self) -> None:
        """All positions are reported; only the last few get context text.

        A log is appended to while it runs, so the conclusion is at the end and
        the setup noise at the beginning. Spending the whole context budget on
        the first matches of a common word is how a search for "FAILED" returns
        three hundred pytest summary lines and not the one that mattered.
        """
        long_document = "\n".join(
            [f"line {index}" for index in range(1, 41)]
            + [f"line {index} ModuleNotFoundError" for index in range(41, 61)]
        )

        found = search_log(long_document, "ModuleNotFoundError", context=1, max_matches=5)

        assert found.total_matches == 20
        # Every position reported (20 is below the reporting cap).
        assert found.matches == list(range(41, 61))
        assert found.truncated_matches is False
        # The context is the *end* of the document, not its beginning.
        assert "line 60 ModuleNotFoundError" in found.content
        assert "line 41 ModuleNotFoundError" not in found.content
        assert found.last_line == 60

    def test_head_reports_the_first_matches_instead(self) -> None:
        """The other end is one word away, for "when did this first appear"."""
        long_document = "\n".join(
            [f"line {index}" for index in range(1, 41)]
            + [f"line {index} ModuleNotFoundError" for index in range(41, 61)]
        )

        found = search_log(
            long_document, "ModuleNotFoundError", context=1, max_matches=5, prefer="head"
        )

        assert "line 41 ModuleNotFoundError" in found.content
        assert "line 60 ModuleNotFoundError" not in found.content
        # The position list still covers every match: only the text moves.
        assert found.matches == list(range(41, 61))

    def test_every_position_is_reported_past_the_context_cap(self) -> None:
        long_document = "\n".join(f"line {index} ModuleNotFoundError" for index in range(1, 251))

        found = search_log(long_document, "ModuleNotFoundError", context=0, max_matches=3)

        assert found.total_matches == 250
        # Positions capped at MAX_MATCHES_REPORTED, context at max_matches, and
        # both describe the same end of the document.
        assert len(found.matches) == 200
        assert found.truncated_matches is True
        assert found.matches[-5:] == [246, 247, 248, 249, 250]

    def test_a_leading_bom_does_not_shift_line_numbers(self) -> None:
        """A stored log starts with a BOM; it must not count as its own text."""
        found = search_log("\ufeff" + DOCUMENT, "line 3", context=0)

        assert found.matches == [3]
        # The first line's text carries no invisible prefix.
        assert "line 1 setup" in search_log("\ufeff" + DOCUMENT, "line 1", context=0).content

    def test_no_match_is_distinguished_from_an_empty_log(self) -> None:
        found = search_log(DOCUMENT, "NoSuchThing", context=2)
        assert found.empty_reason == "no_match"
        assert found.total_matches == 0

        empty = search_log("", "anything", context=2)
        assert empty.empty_reason == "log_empty"
        assert empty.total_lines == 0

    def test_an_invalid_regex_falls_back_to_literal(self) -> None:
        found = search_log("has a(b paren", "a(b", context=0)

        assert found.matches == [1]

    def test_case_insensitive_search(self) -> None:
        found = search_log(DOCUMENT, "modulenotfounderror", context=0, ignore_case=True)

        assert found.total_matches == 2

    def test_a_trailing_newline_does_not_add_a_line(self) -> None:
        """``splitlines`` semantics: the document ends where it ends."""
        assert search_log("a\nb\n", "b", context=0).total_lines == 2
        assert search_log("a\nb", "b", context=0).total_lines == 2
