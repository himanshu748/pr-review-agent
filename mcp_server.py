"""MCP endpoint (Streamable HTTP, stateless JSON mode): exposes ReviewCheap as an
agent-callable service. Built for the OKX.AI Genesis Hackathon (A2MCP, free tier):
a compliant free endpoint simply returns the result of each call.

Protocol: JSON-RPC 2.0 over POST /mcp. Supports initialize, ping, tools/list,
tools/call. Notifications get an empty 202. No sessions, no SSE, every call is
self-contained, which suits serverless hosts and the A2MCP per-call model.
"""
import json
import os
import time

from github import fetch_pr_data
from agent import review_pr, ALLOWED_MODELS, DEFAULT_MODEL

PROTOCOL_VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18"}
SERVER_INFO = {"name": "reviewcheap", "version": "2.0.0"}

TOOLS = [
    {
        "name": "review_pr",
        "description": (
            "Full senior-grade review of a GitHub pull request on open models for under a "
            "cent. Reads the repo's CONTRIBUTING.md first and grades the PR against the "
            "project's own rules, verifies linked-issue assignment, measures the diff "
            "objectively (churn, test-to-code ratio, new dependencies, blast radius), then "
            "scores 8 weighted dimensions (correctness, security, testing, reliability, "
            "maintainability, guideline compliance, performance, docs). Returns a 0-100 "
            "health score with letter grade, a four-tier verdict (APPROVE / APPROVE_WITH_NITS "
            "/ REQUEST_CHANGES / BLOCK), per-dimension scorecard, and every issue with "
            "severity, file:line, explanation and a concrete fix. Works on any public repo."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "pr_url": {"type": "string",
                           "description": "GitHub PR URL, e.g. https://github.com/owner/repo/pull/123"},
                "model": {"type": "string",
                          "description": ("Open model to review with. One of: "
                                          f"{', '.join(ALLOWED_MODELS)}. Default: {DEFAULT_MODEL}.")},
            },
            "required": ["pr_url"],
        },
    },
    {
        "name": "list_models",
        "description": "List the whitelisted open models available for reviews and the default.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _result(id_, result):
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _error(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _text(payload, is_error=False):
    return {"content": [{"type": "text", "text": json.dumps(payload, indent=1)}],
            "isError": is_error}


async def _call_tool(name, args):
    if name == "list_models":
        return _text({"models": ALLOWED_MODELS,
                      "default": os.getenv("HF_MODEL", DEFAULT_MODEL)})

    if name == "review_pr":
        started = time.monotonic()
        pr_data = await fetch_pr_data(args["pr_url"], None)
        review = await review_pr(pr_data, model=args.get("model"))
        if "error" in review:
            return _text({"error": review["error"]}, is_error=True)
        review["duration_ms"] = int((time.monotonic() - started) * 1000)
        return _text({
            "metadata": {
                "title": pr_data.get("title"),
                "author": pr_data.get("author"),
                "base_branch": pr_data.get("base_branch"),
                "head_branch": pr_data.get("head_branch"),
                "commits": pr_data.get("commits"),
                "changed_files": pr_data.get("changed_files"),
                "additions": pr_data.get("additions"),
                "deletions": pr_data.get("deletions"),
            },
            "signals": pr_data.get("signals"),
            "review": review,
        })

    raise ValueError(f"unknown tool: {name}")


async def handle(payload):
    """Process one JSON-RPC message. Returns a response dict, or None for notifications."""
    if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
        return _error(None, -32600, "invalid JSON-RPC 2.0 request")

    method, id_ = payload.get("method"), payload.get("id")
    if id_ is None:  # notification (e.g. notifications/initialized), no response
        return None

    params = payload.get("params") or {}
    if method == "initialize":
        requested = params.get("protocolVersion", "2025-06-18")
        version = requested if requested in PROTOCOL_VERSIONS else "2025-06-18"
        return _result(id_, {"protocolVersion": version,
                             "capabilities": {"tools": {}},
                             "serverInfo": SERVER_INFO})
    if method == "ping":
        return _result(id_, {})
    if method == "tools/list":
        return _result(id_, {"tools": TOOLS})
    if method == "tools/call":
        try:
            return _result(id_, await _call_tool(params.get("name"),
                                                 params.get("arguments") or {}))
        except (ValueError, KeyError) as e:
            return _result(id_, _text({"error": str(e)}, is_error=True))
        except Exception as e:
            return _result(id_, _text({"error": f"review failed: {e}"}, is_error=True))
    return _error(id_, -32601, f"method not found: {method}")
