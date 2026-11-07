# tests/models/test_no_payload_in_models.py
"""Models store values; they never read an interface.

The boundary this file guards:

* no model method is named ``*_from_remote`` -- translating an interface is a
  service's job, not a row's;
* no public model method takes a ``Mapping`` -- that type is how a payload
  arrives, and accepting one re-couples the row to GitHub's field names;
* no model module defines a **module-level** function -- domain logic belongs
  to an object, not to a file. ``__init__.py`` is the package entry point and is
  the one exempt file.

``states.py`` and ``mixins.py`` are exempt from the ``Mapping`` check for one
reason each: the former names the project's own status vocabulary, the latter
maps column names to values for ``find_one_or_create``; neither reads an
interface.
"""

from __future__ import annotations

import ast
from pathlib import Path

MODELS = (
    Path(__file__).resolve().parents[2] / "src" / "rhosocial" / "github_actions_tracker" / "models"
)

#: Files whose ``Mapping`` use is a value vocabulary, not an interface.
EXEMPT = {"states.py", "mixins.py"}
#: The package entry point may hold the one registration function.
ENTRY_POINT = "__init__.py"


class _AnnotationNames:
    """Collect the names an annotation mentions, however it is spelled."""

    @staticmethod
    def collect(node: ast.expr | None) -> set[str]:
        if node is None:
            return set()
        names: set[str] = set()
        for child in ast.walk(node):
            if isinstance(child, ast.Name):
                names.add(child.id)
            elif isinstance(child, ast.Attribute):
                names.add(child.attr)
        return names


class _ModelAudit:
    """Read one model module and report the boundary violations in it."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    def violations(self) -> list[str]:
        found: list[str] = []
        if self.path.name != ENTRY_POINT:
            for node in self.tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    found.append(
                        f"{self.path.name}:{node.lineno}: {node.name} is a module-level function"
                    )
        for node in ast.walk(self.tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name.endswith("_from_remote"):
                found.append(f"{self.path.name}:{node.lineno}: {node.name} reads a payload")
                continue
            if node.name.startswith("_"):
                continue
            annotations = _AnnotationNames.collect(node.returns)
            for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
                annotations |= _AnnotationNames.collect(argument.annotation)
            if "Mapping" in annotations:
                found.append(
                    f"{self.path.name}:{node.lineno}: {node.name} takes a Mapping parameter"
                )
        return found


def test_models_do_not_read_payloads() -> None:
    """Every model module passes the audit; adding a payload entry makes this red."""
    violations: list[str] = []
    for path in sorted(MODELS.glob("*.py")):
        if path.name in EXEMPT:
            continue
        violations.extend(_ModelAudit(path).violations())
    assert violations == [], "models must accept semantic values only:\n" + "\n".join(violations)
