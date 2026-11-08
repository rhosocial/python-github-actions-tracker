# tests/adapters/test_cli_formatters.py
"""The text formatter keeps one record's fields together.

Regression: a dictionary whose value is a list of dictionaries was rendered by
embedding the inner multi-line text after the bullet, so the second and later
fields of every item started at column 0. A reader then could not tell whether
``private`` belonged to the repository above it or was a top-level key.
"""

from __future__ import annotations

from rhosocial.github_actions_tracker.adapters.cli.formatters import format_text


def test_list_items_indent_continuation_lines() -> None:
    data = {
        "repositories": [
            {"full_name": "owner/one", "private": False, "default_branch": "main"},
            {"full_name": "owner/two", "private": True, "default_branch": "trunk"},
        ]
    }

    rendered = format_text(data)

    assert rendered.splitlines() == [
        "repositories:",
        "  - full_name: owner/one",
        "    private: False",
        "    default_branch: main",
        "  - full_name: owner/two",
        "    private: True",
        "    default_branch: trunk",
    ]


def test_nested_dictionary_indents_its_keys() -> None:
    data = {"config": {"content_type": "json", "insecure_ssl": 0}}

    rendered = format_text(data)

    assert rendered.splitlines() == [
        "config:",
        "  content_type: json",
        "  insecure_ssl: 0",
    ]
