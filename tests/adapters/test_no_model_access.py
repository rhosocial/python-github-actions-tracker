# tests/adapters/test_no_model_access.py
"""The layer rule, as a test: adapters and services never read the models.

Two rules, both previously enforced only by review:

* an adapter talks to a controller; touching ``models`` from a protocol adapter
  is how a domain query ends up written three times, once per protocol;
* a service is a capability (the GitHub API), not a flow: a service importing
  ``models`` at runtime means a flow has leaked into the capability layer.

A ``TYPE_CHECKING`` import is allowed: naming a model in an annotation costs no
access. What is forbidden is an import that *executes*, at module level or inside
a function.

The exceptions are explicit and listed in :data:`MODEL_READERS`. ``controllers``
is where a flow's model operations live; ``credentials`` is the existing
cross-cutting package that resolves a PAT from its own table; ``authorization``
is the newest one, added for the request-authentication snapshot -- it reads the
webhook secrets a delivery is verified against, and only it. Everything else
stays out, so "which packages may read a row" is one list a reviewer can read
rather than a habit.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "rhosocial" / "github_actions_tracker"
PACKAGE = "rhosocial.github_actions_tracker"
MODELS = f"{PACKAGE}.models"

#: Top-level packages and modules whose files may import the models at runtime.
#: Anything not named here must reach a row through one of these.
#:
#: ``database`` binds the models to a backend -- it cannot avoid naming them.
#: ``views`` maps a state enum. Both are leaves; neither holds a flow.
MODEL_READERS = frozenset(
    {
        "models",
        "core",
        "database.py",
        "views.py",
        "controllers",
        "credentials",
        "authorization",
    }
)


def _absolute(node: ast.ImportFrom, package: str) -> str:
    """Resolve a ``from ...x import y`` to its absolute module name."""
    if node.level == 0:
        return node.module or ""
    kept = package.split(".")[: len(package.split(".")) - (node.level - 1)]
    if node.module:
        kept.append(node.module)
    return ".".join(kept)


def _is_type_checking(test: ast.expr) -> bool:
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"


class _ImportCollector(ast.NodeVisitor):
    """Collect runtime imports whose module matches, skipping type-only blocks."""

    def __init__(self, package: str, prefixes: tuple[str, ...]) -> None:
        self.package = package
        self.prefixes = prefixes
        self.found: list[str] = []

    def visit_If(self, node: ast.If) -> None:
        if _is_type_checking(node.test):
            # The body never runs in production; the else branch does.
            for child in node.orelse:
                self.visit(child)
            return
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = _absolute(node, self.package)
        if any(module == prefix or module.startswith(f"{prefix}.") for prefix in self.prefixes):
            self.found.append(f"line {node.lineno}: {module}")


def _imports_in(source: str, package: str, prefixes: tuple[str, ...]) -> list[str]:
    collector = _ImportCollector(package, prefixes)
    collector.visit(ast.parse(source))
    return collector.found


def _model_imports_in(source: str, package: str) -> list[str]:
    """The ``models`` imports of one file -- the question the original rules asked."""
    return _imports_in(source, package, (MODELS,))


def _imports_in_tree(root: Path, prefixes: tuple[str, ...]) -> dict[str, list[str]]:
    """Every file under ``root`` that imports one of ``prefixes`` at runtime."""
    offenders: dict[str, list[str]] = {}
    for path in sorted(root.rglob("*.py")):
        package = ".".join((PACKAGE, *path.relative_to(SRC).with_suffix("").parts[:-1]))
        found = _imports_in(path.read_text(encoding="utf-8"), package, prefixes)
        if found:
            offenders[str(path.relative_to(SRC))] = found
    return offenders


def test_no_adapter_imports_the_models() -> None:
    """A protocol adapter reaches data through a controller, never a model."""
    offenders = _imports_in_tree(SRC / "adapters", (MODELS,))

    assert not offenders, "adapters reach the models directly:\n" + "\n".join(
        f"  {path}: {', '.join(found)}" for path, found in offenders.items()
    )


def test_no_service_imports_the_models_at_runtime() -> None:
    """A service is a capability; a flow that reads rows lives in a controller."""
    offenders = _imports_in_tree(SRC / "services", (MODELS,))

    assert not offenders, "services reach the models directly:\n" + "\n".join(
        f"  {path}: {', '.join(found)}" for path, found in offenders.items()
    )


def test_only_the_listed_packages_import_the_models() -> None:
    """The allowlist is the whole list, or it is not an allowlist.

    Scanning every package turns "controllers may, services may not" into a
    decision that is written down: a new package that starts reading rows shows
    up here as an unexpected reader, and the answer is either "add it to
    :data:`MODEL_READERS` and say why" or "go through a controller".
    """
    offenders = _imports_in_tree(SRC, (MODELS,))
    unexpected = sorted(path for path in offenders if Path(path).parts[0] not in MODEL_READERS)

    assert (
        not unexpected
    ), "these packages read the models and are not in the allowlist:\n  " + "\n  ".join(
        f"{path}: {', '.join(offenders[path])}" for path in unexpected
    )


def test_the_scanner_sees_relative_and_lazy_imports() -> None:
    """Guard the guard: the check must catch what it exists to catch."""
    source = (
        "from __future__ import annotations\n"
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from ....models import AsyncWorkflowRun\n"
        "def load():\n"
        "    from ....models import AsyncJob\n"
        "    return AsyncJob\n"
    )

    found = _model_imports_in(source, "rhosocial.github_actions_tracker.adapters.mcp.tools")

    assert found == [
        "line 6: rhosocial.github_actions_tracker.models"
    ], "the type-only import must be allowed, the lazy one must not"


def test_the_scanner_accepts_an_indirect_import() -> None:
    source = "from ....controllers import QueryController\n"

    found = _model_imports_in(source, "rhosocial.github_actions_tracker.adapters.mcp.tools")

    assert found == []


def test_the_authorization_package_imports_neither_layer_above_it() -> None:
    """The snapshot package reads rows; it does not run flows or serve requests.

    ``authorization`` holds the capability every adapter calls before anything
    else, so it is the most widely imported package in the request path. If it
    reached back into a controller or an adapter, that import would close a cycle
    in the layer every protocol depends on.
    """
    offenders = _imports_in_tree(
        SRC / "authorization",
        (f"{PACKAGE}.controllers", f"{PACKAGE}.adapters"),
    )

    assert not offenders, "authorization reaches the layers above it:\n" + "\n".join(
        f"  {path}: {', '.join(found)}" for path, found in offenders.items()
    )
