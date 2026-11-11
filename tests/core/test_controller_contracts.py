# tests/core/test_controller_contracts.py
"""A controller declares the capabilities it needs, not the classes that provide them.

Two controllers in one package are a dependency graph, and a graph of concrete
classes is one that only the composition root can read: "webhook handling needs
a RunController" says nothing about *why*, and the test then has to build a run
controller, its reader and its row layer to exercise one method.

The rule this file enforces: a ``*Controller`` may only name protocols, value
types and its own row layers in its constructor. Another ``*Controller`` is not
a dependency; the *behaviour* it offers is, and that behaviour gets a protocol in
``controllers/protocols.py``.

The row layers (``RunData``, ``RepositoryData``, ``DeliveryLedger``) are
deliberately outside this rule for now: they are the database seam, they are
constructed next to the controller that owns them, and narrowing them is a
separate change.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from rhosocial.github_actions_tracker.controllers import dispatch, protocols

CONTROLLERS = Path(__file__).resolve().parents[2] / "src" / "rhosocial" / "github_actions_tracker" / "controllers"

#: Names that may appear in a controller constructor even though they are classes.
ROW_LAYERS = {"RunData", "RepositoryData", "DeliveryLedger"}


def _controller_names() -> set[str]:
    names: set[str] = set()
    for path in CONTROLLERS.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name.endswith("Controller"):
                names.add(node.name)
    return names


def _init_annotations(classes: set[str]) -> list[tuple[str, str, str]]:
    """``(file, class, annotation)`` for every controller constructor argument."""
    found: list[tuple[str, str, str]] = []
    for path in sorted(CONTROLLERS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, ast.ClassDef) or node.name not in classes:
                continue
            init = next(
                (
                    item
                    for item in node.body
                    if isinstance(item, ast.FunctionDef) and item.name == "__init__"
                ),
                None,
            )
            if init is None:
                continue
            for arg in init.args.args + init.args.kwonlyargs:
                if arg.arg == "self" or arg.annotation is None:
                    continue
                found.append((path.name, node.name, ast.unparse(arg.annotation)))
    return found


def test_no_controller_names_another_controller() -> None:
    controllers = _controller_names()
    offenders = [
        (file, owner, annotation)
        for file, owner, annotation in _init_annotations(controllers)
        if any(
            name in controllers and name != owner and name not in ROW_LAYERS
            for name in _identifiers(annotation)
        )
    ]
    assert offenders == [], (
        "these constructors ask for a controller class instead of the behaviour it "
        "offers; declare a protocol in controllers/protocols.py: "
        + ", ".join(f"{owner}({annotation})" for _, owner, annotation in offenders)
    )


def _identifiers(annotation: str) -> set[str]:
    """Every bare name an annotation mentions, including inside ``X | None``."""
    tree = ast.parse(annotation, mode="eval")
    return {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}


def test_the_dispatch_defaults_match_the_protocol() -> None:
    """A protocol's defaults are part of its contract; they must not drift."""
    for name in ("dispatch", "rerun", "rerun_failed"):
        declared = inspect.signature(getattr(protocols.DispatchVerbs, name))
        implemented = inspect.signature(getattr(dispatch.DispatchController, name))
        assert (
            declared.parameters["resolve_attempts"].default
            == implemented.parameters["resolve_attempts"].default
            == dispatch.RESOLVE_ATTEMPTS
        )
        assert (
            declared.parameters["resolve_delay"].default
            == implemented.parameters["resolve_delay"].default
            == dispatch.RESOLVE_DELAY
        )
