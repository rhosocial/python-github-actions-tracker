# tests/app/test_docs_site.py
"""The documentation site is the repository's docs, rendered — not a second copy.

Two things are easy to get wrong once a site exists, and both are invisible until a reader
hits them: a navigation that drifts from the table of contents, and a page that publishes
something belonging to one deployment. This file guards both, plus the one property that
makes the two language builds one site instead of two.

The nav is written by hand in `mkdocs.<lang>.yml` because the alternative (having a plugin
derive it from SUMMARY.md) does not parse this repository's summary: literate-nav reads our
`## Section` headings as neither sections nor list items. A hand-written nav is fine as long
as something fails when it drifts, which is what the first test is for.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
CONFIGS = {"en_US": ROOT / "mkdocs.en.yml", "zh_CN": ROOT / "mkdocs.zh.yml"}

#: Infrastructure that belongs to one deployment, never to a public page.
PRIVATE = re.compile(
    r"(?i)"
    r"rho\.im"
    r"|db-dev-\d"
    r"|\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"
    r"|\b192\.168\.\d{1,3}\.\d{1,3}\b"
    r"|\b172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b"
    r"|[\w.-]+\.internal\b"
)

#: Names that appear in documentation as examples rather than as our infrastructure.
#: `host.docker.internal` is a Docker Desktop alias, and the proxy snippet needs a
#: database address to keep out of the proxy -- any LAN address does, so it picks one.
EXAMPLES = {"host.docker.internal", "192.168.1.3"}

#: Files a reader of the site actually sees, plus the code the site documents.
PUBLIC = ("README.md", "README.zh-CN.md", "docs", "src")


class _TolerantLoader(yaml.SafeLoader):
    """SafeLoader that ignores MkDocs' `!!python/object/apply` slugify tags."""


_TolerantLoader.add_multi_constructor(
    "tag:yaml.org,2002:python/object/apply", lambda loader, suffix, node: suffix
)


def _config(path: Path) -> dict:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=_TolerantLoader)


def _nav_pages(nav: list, prefix: str = "") -> list[str]:
    """Every page path a nav refers to, in document order."""
    pages: list[str] = []
    for entry in nav:
        if isinstance(entry, str):
            pages.append(f"{prefix}{entry}")
        elif isinstance(entry, dict):
            for _label, value in entry.items():
                if isinstance(value, str):
                    pages.append(f"{prefix}{value}")
                else:
                    pages.extend(_nav_pages(value, prefix))
    return pages


def _summary_pages(lang: str) -> list[str]:
    """The pages SUMMARY.md links to, in order."""
    text = (DOCS / lang / "SUMMARY.md").read_text(encoding="utf-8")
    return [target for _, target in re.findall(r"\[([^\]]+)\]\(([^)]+)\)", text)]


@pytest.mark.parametrize("lang", sorted(CONFIGS))
def test_the_nav_is_the_summary(lang: str) -> None:
    """A page the site does not link is a page only the repository has."""
    config = _config(CONFIGS[lang])
    assert _nav_pages(config["nav"]) == _summary_pages(lang), (
        f"{CONFIGS[lang].name} and docs/{lang}/SUMMARY.md disagree: the site would "
        "either hide a page or link one that does not exist"
    )


def test_both_languages_build_the_same_site() -> None:
    """Only three lines may differ; everything else is one site in two languages."""
    en = _config(CONFIGS["en_US"])
    zh = _config(CONFIGS["zh_CN"])
    for key in ("markdown_extensions", "plugins"):
        assert en[key] == zh[key], f"{key} differs between the two builds"


def test_the_analytics_script_is_one_file_twice() -> None:
    """Each language builds from its own docs root, so the script lives in both.

    Duplication is the price of two builds; a test that compares the bytes is the price of
    duplication. If the Baidu site ID changes it must change in both, or one language
    reports to a different site than the other.
    """
    copies = [
        DOCS / "en_US/javascripts/baidu-tongji.js",
        DOCS / "zh_CN/javascripts/baidu-tongji.js",
    ]
    assert (
        copies[0].read_bytes() == copies[1].read_bytes()
    ), "the two Baidu Tongji copies differ; they must stay byte-identical"


def test_analytics_is_configured_in_both_builds() -> None:
    """A site that is only measured in one language is measured wrong."""
    for lang, path in CONFIGS.items():
        config = _config(path)
        analytics = config["extra"]["analytics"]
        assert analytics["provider"] == "google"
        assert analytics["property"].startswith("G-"), f"{lang}: not a GA4 measurement id"
        assert (
            "javascripts/baidu-tongji.js" in config["extra_javascript"]
        ), f"{lang}: the Baidu Tongji script is not loaded"
        # Every page that carries tracking should say so.
        assert (DOCS / lang / "privacy.md").is_file()


def _repeated_keys(node: Any, trail: str = "") -> list[str]:
    """Every key that appears more than once inside one mapping, as a path.

    ``yaml.compose`` keeps both copies of a repeated key, which is the only reason
    this walk can see them at all -- ``safe_load`` above keeps the last one and
    reports nothing. The check is per mapping: the same key in two different
    mappings (two ``nav`` entries both titled *Overview*) is ordinary YAML, while
    the same key twice under one mapping deletes the first one silently.
    """
    found: list[str] = []
    if isinstance(node, yaml.MappingNode):
        counts: dict[str, int] = {}
        for key_node, _ in node.value:
            key = getattr(key_node, "value", None)
            if isinstance(key, str):
                counts[key] = counts.get(key, 0) + 1
        for key, count in counts.items():
            if count > 1:
                found.append(f"{trail}.{key}" if trail else key)
        for key_node, value_node in node.value:
            key = getattr(key_node, "value", None)
            found.extend(_repeated_keys(value_node, key if isinstance(key, str) else trail))
    elif isinstance(node, yaml.SequenceNode):
        for item in node.value:
            found.extend(_repeated_keys(item, trail))
    return found


def test_no_config_repeats_a_key() -> None:
    """A repeated key is the one YAML mistake a parser will not report.

    Seen here: the analytics provider was added as a *second* top-level ``extra:``
    block a few lines below the first. Both builds stayed green and `--strict`
    said nothing, because the loader kept the last block and dropped the first
    one whole -- and with it ``alternate:``, the language switcher, which is
    invisible in a build log and only shows up as a site that no longer offers
    the other language. The same class of bug is guarded in
    ``tests/app/test_workflow_files.py`` for the workflow files.
    """
    for path in CONFIGS.values():
        with path.open(encoding="utf-8") as handle:
            root = yaml.compose(handle, Loader=_TolerantLoader)
        repeats = _repeated_keys(root)
        assert repeats == [], (
            f"{path.name} repeats {repeats}; a loader keeps only the last value of "
            "a repeated key, so the rest of that configuration is dropped without "
            "a warning"
        )


def test_no_private_infrastructure_reaches_a_public_page() -> None:
    """The site is indexed. A development database address is not a documentation value.

    Seen once already: a private host and port were written into source defaults, tests,
    helper scripts and both documentation trees of a public repository.
    """
    offenders: dict[str, list[str]] = {}
    for relative in PUBLIC:
        target = ROOT / relative
        files = [target] if target.is_file() else sorted(target.rglob("*"))
        for path in files:
            if not path.is_file() or path.suffix not in {".md", ".py", ".yml", ".yaml"}:
                continue
            hits = sorted(
                {
                    match.group(0)
                    for match in PRIVATE.finditer(
                        path.read_text(encoding="utf-8", errors="replace")
                    )
                    if match.group(0).lower() not in EXAMPLES
                }
            )
            if hits:
                offenders[path.relative_to(ROOT).as_posix()] = hits
    assert not offenders, f"private infrastructure in published files: {offenders}"
