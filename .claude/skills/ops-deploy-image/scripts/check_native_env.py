#!/usr/bin/env python3
"""Can this machine run citracker without Docker?

Standalone: standard library only, so it runs before the package is installed (and tells
you to install it when that is the missing piece). It checks the three things the native
deployment needs and nothing else: an interpreter in range, the package and its two
entry points, and the database driver the controllers use.

Exit codes: 0 ready, 2 usable with a warning, 1 it cannot run here.
"""

from __future__ import annotations

import importlib.util
import json
import platform
import shutil
import sys

#: requires-python says 3.10; 3.15 is what CI and the image run. Both are fine, and a
#: version outside that window is reported rather than guessed at.
MINIMUM = (3, 10)
TESTED = (3, 15)


def _entry_point(name: str) -> tuple[bool, str]:
    """Where an entry point lives: PATH first, then beside the interpreter.

    A virtual environment that has not been activated is the normal case for this
    check — the operator runs one interpreter by path — and in that state `which`
    finds nothing while the script is sitting in `Scripts/` next to the interpreter.
    """
    path = shutil.which(name)
    if path is not None:
        return True, path
    from pathlib import Path

    for suffix in ("", ".exe", ".cmd", ".bat"):
        candidate = Path(sys.executable).parent / f"{name}{suffix}"
        if candidate.is_file():
            return True, str(candidate)
    return False, "not on PATH, and not beside this interpreter"


def _module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def main() -> int:
    findings: list[dict[str, str]] = []
    failures: list[dict[str, str]] = []
    facts: dict[str, object] = {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "executable": sys.executable,
    }

    version = sys.version_info
    facts["in_supported_range"] = (version.major, version.minor) >= MINIMUM
    if not facts["in_supported_range"]:
        failures.append(
            {
                "code": "python_too_old",
                "message": f"Python {platform.python_version()} is older than "
                f"{MINIMUM[0]}.{MINIMUM[1]}, the floor in pyproject.toml.",
            }
        )
    elif (version.major, version.minor) != TESTED:
        findings.append(
            {
                "code": "python_not_the_tested_version",
                "message": f"Python {platform.python_version()} works, but CI and the "
                f"image run {TESTED[0]}.{TESTED[1]}; differences tend to appear in the "
                "database driver rather than the service.",
            }
        )

    for name in ("rhosocial-github-ci-tracker", "rhosocial-github-ci-tracker-db"):
        found, detail = _entry_point(name)
        facts[name] = detail
        if not found:
            failures.append(
                {
                    "code": "entry_point_missing",
                    "message": f"{name} is not on PATH: install the package "
                    "(pip install rhosocial-github-ci-tracker) in the environment this "
                    "process will run from.",
                }
            )

    # The controllers reach PostgreSQL through psycopg 3. Its absence is the most common
    # half-installed state, and the failure it produces later names a module, not a step.
    driver = _module("psycopg")
    facts["psycopg"] = driver
    if not driver:
        failures.append(
            {
                "code": "driver_missing",
                "message": "psycopg (v3) is not importable here; the database layer needs "
                "it. pip install psycopg[binary]",
            }
        )
    else:
        try:
            import psycopg

            facts["psycopg_version"] = getattr(psycopg, "__version__", "unknown")
        except Exception as exc:  # noqa: BLE001 - a broken install is the finding
            failures.append(
                {
                    "code": "driver_unusable",
                    "message": f"psycopg is present but cannot be imported: "
                    f"{type(exc).__name__}: {exc}",
                }
            )

    # On Windows the async psycopg path needs the selector loop, which the package's CLI
    # installs for its own processes. Anything started outside that CLI (a systemd-style
    # runner, an IDE) has to do the same, so say it here rather than at the first query.
    if platform.system() == "Windows":
        findings.append(
            {
                "code": "windows_event_loop",
                "message": "on Windows, psycopg's async mode needs "
                "asyncio.SelectorEventLoop; the CLI installs it for its own processes, and "
                "anything else driving the controllers must do the same.",
            }
        )

    ok = not failures
    payload = {
        "ok": ok,
        "verdict": (
            "ready" if ok and not findings else "usable with warnings" if ok else "cannot run"
        ),
        "facts": facts,
        "failures": failures,
        "warnings": findings,
    }
    if "--json" in sys.argv:
        print(json.dumps(payload, indent=2))
    else:
        for item in failures + findings:
            print(f"[{item['code']}] {item['message']}")
        print(f"verdict: {payload['verdict']}")
    return 0 if ok and not findings else 2 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
