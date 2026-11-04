#!/usr/bin/env python3
"""Is this deployment actually serving?

Standalone: standard library only. Ordered the way an operator would ask: process, liveness
(zero I/O), readiness (one database round trip plus the schema state), the build identity
against the image that was pulled, metrics answering without touching the database, and
finally a real MCP handshake over the socket a model would use -- because a probe that
passes while the tool loop fails is the failure this order exists to catch.

Exit codes: 0 everything answered, 1 something did not.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

MCP_PROTOCOL_VERSION = "2025-06-18"


def _request(
    url: str, *, method: str = "GET", headers: dict[str, str] | None = None, body: object = None
) -> tuple[int, bytes, object]:
    data = None
    request_headers = {"User-Agent": "citracker-deploy-check"}
    if headers:
        request_headers.update(headers)
    if body is not None:
        data = json.dumps(body).encode()
        request_headers.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30, context=_context()) as response:
            return response.status, response.read(), response.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers
    except Exception as exc:  # noqa: BLE001 - unreachable is the answer
        return 0, str(exc).encode(), {}


def _header(headers: object, name: str) -> str:
    """A header by name, case-insensitively.

    ASGI apps and the stdlib disagree about the case they send, and `dict(message)`
    keeps whatever arrived, so "Content-Type" can silently miss a lowercase
    "content-type" and every check that reads it fails for the wrong reason.
    """
    if not headers:
        return ""
    getter = getattr(headers, "get", None)
    if callable(getter):
        try:
            return str(getter(name) or getter(name.title()) or "")
        except Exception:  # noqa: BLE001 - a header is a string or nothing
            return ""
    try:
        return str(headers[name])
    except Exception:  # noqa: BLE001 - any mapping shape
        return ""


def _context():
    """A TLS context that accepts a self-signed listener.

    This is a *deployment* check: the question here is whether the port answers, and the
    certificate's own story is `check_certificate.py`'s to tell. Verification is switched
    off only for this script's own requests.
    """
    import ssl

    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def _json(data: bytes) -> object:
    try:
        return json.loads(data or b"{}")
    except Exception:  # noqa: BLE001 - not-JSON bodies are reported verbatim
        return data[:200].decode(errors="replace")


def _text(body: bytes) -> str:
    """A JSON body as text, bounded, so a long error does not flood the report."""
    return str(_json(body))[:400]


def _sse_json(body: bytes) -> object:
    """The JSON payload of a `text/event-stream` response.

    A Streamable HTTP reply is framed as `event: message` / `data: {...}`, so the body
    is not JSON until the framing is taken off -- and a check that parses the raw body
    reports "no tools" against a server that answered with all thirteen.
    """
    text = body.decode(errors="replace")
    for line in text.splitlines():
        if line.startswith("data:"):
            return _json(line[len("data:") :].strip().encode())
    return _json(body)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--webhook-url", default="http://127.0.0.1:18080")
    parser.add_argument("--base-url", default="http://127.0.0.1:8001", help="the MCP listener")
    parser.add_argument(
        "--api-key", default="", help="expected Bearer; empty means none configured"
    )
    parser.add_argument("--repo", default=None, help="owner/repo to list tools against")
    parser.add_argument(
        "--expect-revision", default=None, help="short SHA the pulled image carries"
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    steps: list[dict[str, object]] = []

    def record(name: str, ok: bool, detail: object) -> None:
        steps.append({"check": name, "ok": ok, "detail": detail})

    # The webhook listener may be on TLS (the recommended shape) or plain HTTP (a
    # listener without a certificate). Ask once, remember which one answered, and
    # say so: "your callback is not encrypted" is a finding, not a detail to hide.
    https_url = args.webhook_url.replace("http://", "https://", 1)
    status, body, _ = _request(f"{https_url}/livez")
    if status == 200:
        listener = https_url
        scheme = "https"
    else:
        listener = args.webhook_url
        scheme = "http"
        status, body, _ = _request(f"{listener}/livez")
    record(
        "livez answers",
        status == 200,
        {
            "status": status,
            "scheme": scheme,
            "body": _json(body),
            "note": None if scheme == "https" else "the listener answered on plain HTTP",
        },
    )

    headers = {"Authorization": f"Bearer {args.api_key}"} if args.api_key else {}

    # The MCP listener is TLS-terminated only when the operator put a certificate in
    # front of it (the recommended shape for anything public). /metrics needs no
    # authentication, so probing it learns which scheme answers before the handshake,
    # and reporting the answer is part of the report: an unencrypted listener a human
    # can reach is worth saying out loud.
    https_base = args.base_url.replace("http://", "https://", 1)
    status, _, _ = _request(f"{https_base}/metrics", headers=headers)
    if status == 200:
        base, base_scheme = https_base, "https"
    else:
        base = args.base_url
        status, _, _ = _request(f"{base}/metrics", headers=headers)
        base_scheme = "http"
    record(
        "MCP listener reachable",
        status == 200,
        {
            "scheme": base_scheme,
            "status": status,
            "note": None if base_scheme == "https" else "the MCP listener answered on plain HTTP",
        },
    )

    status, body, _ = _request(f"{listener}/readyz", headers=headers)
    ready = _json(body) if status == 200 else {}
    checks = ready.get("checks", {}) if isinstance(ready, dict) else {}
    record(
        "readyz answers, database and schema ok",
        status == 200
        and isinstance(ready, dict)
        and isinstance(checks, dict)
        and checks.get("database") == "ok",
        {"status": status, "body": ready},
    )

    revision = ""
    if isinstance(ready, dict):
        revision = str(ready.get("build", {}).get("short_revision") or "")
    record(
        "revision is the image that was pulled",
        not args.expect_revision or revision == args.expect_revision,
        {"reported": revision or None, "expected": args.expect_revision},
    )

    status, body, response_headers = _request(f"{base}/metrics", headers=headers)
    content_type = _header(response_headers, "content-type")
    record(
        "metrics answer in Prometheus text",
        status == 200 and "text/plain" in content_type and b"citracker_" in body,
        {"status": status, "content_type": content_type, "has_counters": b"citracker_" in body},
    )

    # A real MCP handshake: initialize, then tools/list. Both go over the same socket a
    # model uses, so a passing probe with a broken tool loop cannot hide here.
    session_headers = {
        **headers,
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
    }
    status, body, response_headers = _request(
        f"{base}/mcp",
        method="POST",
        headers=session_headers,
        body={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "deploy-check", "version": "1"},
            },
        },
    )
    session_id = _header(response_headers, "mcp-session-id")
    initialize_ok = status == 200 and b"serverInfo" in body
    record(
        "MCP initialize",
        initialize_ok,
        {"status": status, "session": bool(session_id), "body": _text(body)},
    )

    tools: list[str] = []
    if initialize_ok:
        # No session id is required: a stateless Streamable HTTP server answers every
        # request on its own, and the one this service runs does exactly that.
        follow_up = {**session_headers}
        if session_id:
            follow_up["mcp-session-id"] = session_id
        _request(
            f"{base}/mcp",
            method="POST",
            headers=follow_up,
            body={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        status, body, _ = _request(
            f"{base}/mcp",
            method="POST",
            headers=follow_up,
            body={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )
        try:
            payload = _sse_json(body)
            tools = sorted(tool["name"] for tool in payload["result"]["tools"])
        except Exception:  # noqa: BLE001 - reported as the detail below
            tools = []
        record(
            "MCP tools/list",
            bool(tools),
            {"status": status, "tools": tools},
        )
        expected = {"ci_status", "list_repos", "find_runs", "get_run", "get_job", "get_job_log"}
        record(
            "the tools a first question needs",
            expected.issubset(set(tools)),
            {"missing": sorted(expected - set(tools))},
        )

    ok = all(bool(item["ok"]) for item in steps)
    payload = {"ok": ok, "steps": steps}
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        for item in steps:
            print(f"[{'ok' if item['ok'] else 'FAIL'}] {item['check']} — {item['detail']}")
        print(f"verdict: {'ready' if ok else 'not ready'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
