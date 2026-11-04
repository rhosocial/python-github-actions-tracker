#!/usr/bin/env python3
"""Does this fine-grained token do what the service asks of it?

Standalone: standard library only. It separates what can be **observed** (the token works,
and each permission was seen doing its job) from what cannot (Actions *write*: proving it
means dispatching a workflow, so it stays unproven rather than claimed green).

Exit codes: 0 the token works for what was probed, 2 it works for reads and something is
missing, 1 the token itself is dead.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

#: What is probed, and the tool that fails without it. Order is the report's order.
PROBES = (
    ("contents_read", "Contents: Read", "GET /repos/{repo}"),
    ("actions_read", "Actions: Read", "GET /repos/{repo}/actions/runs"),
    ("hooks_read", "Administration: repository hooks", "GET /repos/{repo}/hooks"),
)

#: Probed only when asked for, because it either spends CI minutes or is not observable.
WRITE_PROBES = {
    "actions_write": (
        "Actions: Write",
        "cannot be verified here without dispatching a run (trigger_workflow / "
        "rerun_run will be the first thing to fail if it is missing)",
    ),
}


def _get(url: str, token: str) -> tuple[int, object]:
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "citracker-deploy-check",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read() or b"{}")
        except Exception:  # noqa: BLE001 - a non-JSON error body is still a status
            body = {}
        return exc.code, body
    except Exception as exc:  # noqa: BLE001 - network failures are the answer
        return 0, {"message": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token", required=True, help="the token to check; prefer stdin")
    parser.add_argument("--repo", required=True, help="owner/repo the deployment will track")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    status, identity = _get("https://api.github.com/user", args.token)
    if status == 0:
        payload = {"ok": False, "code": "unreachable", "message": identity.get("message")}
        print(json.dumps(payload, indent=2) if args.json else payload["message"])
        return 1
    if status == 401:
        payload = {
            "ok": False,
            "code": "token_dead",
            "message": "GitHub says 401: the token is invalid, expired or revoked",
        }
        print(json.dumps(payload, indent=2) if args.json else payload["message"])
        return 1
    if status != 200:
        payload = {
            "ok": False,
            "code": "identity_unreadable",
            "message": f"GET /user answered {status}",
        }
        print(json.dumps(payload, indent=2) if args.json else payload["message"])
        return 1

    owner, _, name = args.repo.partition("/")
    results: list[dict[str, object]] = []
    for key, label, _template in PROBES:
        url = f"https://api.github.com/repos/{owner}/{name}"
        if key == "actions_read":
            url += "/actions/runs?per_page=1"
        if key == "hooks_read":
            url += "/hooks"
        code, _body = _get(url, args.token)
        results.append(
            {
                "capability": key,
                "label": label,
                "status": code,
                "ok": code == 200,
                "observed": code == 200,
                "note": None if code == 200 else f"answered {code}: {body_first(_body)}",
            }
        )

    unproven = [
        {"capability": key, "label": label, "observed": False, "note": note}
        for key, (label, note) in WRITE_PROBES.items()
    ]

    missing = [item for item in results if not item["ok"]]
    payload = {
        "ok": not missing,
        "identity": identity.get("login"),
        "token_scopes": identity.get("x-oauth-scopes", "fine-grained (not listed on /user)"),
        "verified": results,
        "unproven": unproven,
        "missing": [item["capability"] for item in missing],
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"token works as: {payload['identity']}")
        for item in results:
            verdict = "ok" if item["ok"] else f"MISSING ({item['status']})"
            print(f"  {item['label']}: {verdict}")
        for item in unproven:
            print(f"  {item['label']}: unproven -- {item['note']}")
    return 0 if not missing else 2


def body_first(body: object) -> str:
    if isinstance(body, dict):
        return str(body.get("message", ""))[:120]
    return str(body)[:120]


if __name__ == "__main__":
    sys.exit(main())
