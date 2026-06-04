from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent import (
    HF_TOKEN_MISSING_ERROR,
    LLM_PROVIDER_ERROR,
    MAX_DESCRIPTION_CHARS,
    MAX_DIFF_CHARS,
    MAX_TITLE_CHARS,
    normalize_review_payload,
    review_pr,
)
from github import INVALID_PR_URL_ERROR, fetch_pr_data, parse_github_pr_url
from main import app


def test_parse_github_pr_url_accepts_exact_pull_url():
    assert parse_github_pr_url("https://github.com/fastapi/fastapi/pull/123") == (
        "fastapi",
        "fastapi",
        "123",
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/fastapi/fastapi/pull/123",
        "https://github.com/fastapi/fastapi/issues/123",
        "https://evil.example.com/fastapi/fastapi/pull/123",
        "https://github.com/fastapi/fastapi/pull/not-a-number",
        "https://github.com/owner with spaces/repo/pull/1",
        "file:///etc/passwd",
    ],
)
def test_parse_github_pr_url_rejects_non_pr_urls(url):
    with pytest.raises(ValueError, match="Invalid GitHub PR URL"):
        parse_github_pr_url(url)


def test_parse_github_pr_url_does_not_echo_input():
    secret_url = "https://github.com/owner/repo/pull/not-a-number?token=redacted_secret"

    with pytest.raises(ValueError) as caught:
        parse_github_pr_url(secret_url)

    assert str(caught.value) == INVALID_PR_URL_ERROR
    assert "redacted_secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_fetch_pr_data_bounds_files_and_diff(monkeypatch):
    class Response:
        def __init__(self, payload, status_code=200):
            self._payload = payload
            self.status_code = status_code

        def json(self):
            return self._payload

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, headers):
            if url.endswith("/files"):
                return Response(
                    [
                        {
                            "filename": f"file_{i}.py",
                            "status": "modified",
                            "patch": "+" + ("x" * 1000),
                        }
                        for i in range(30)
                    ]
                )
            return Response(
                {
                    "title": "T",
                    "body": "B",
                    "user": {"login": "alice"},
                    "base": {"ref": "main"},
                    "head": {"ref": "feature"},
                    "commits": 1,
                    "changed_files": 30,
                    "additions": 500,
                    "deletions": 2,
                }
            )

    monkeypatch.setattr("github.httpx.AsyncClient", lambda **kwargs: Client())

    data = await fetch_pr_data("https://github.com/owner/repo/pull/7")

    assert len(data["files_summary"]) == 20
    assert len(data["diff"]) <= 12_030
    assert "[Diff truncated]" in data["diff"]


@pytest.mark.asyncio
async def test_fetch_pr_data_ignores_blank_github_token(monkeypatch):
    seen_headers = []

    class Response:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, headers):
            seen_headers.append(headers)
            if url.endswith("/files"):
                return Response([])
            return Response({"title": "T", "user": {"login": "alice"}})

    monkeypatch.setattr("github.httpx.AsyncClient", lambda **kwargs: Client())

    await fetch_pr_data("https://github.com/owner/repo/pull/7", "   ")

    assert all("Authorization" not in headers for headers in seen_headers)


@pytest.mark.asyncio
async def test_fetch_pr_data_strips_github_token(monkeypatch):
    seen_headers = []

    class Response:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, headers):
            seen_headers.append(headers)
            if url.endswith("/files"):
                return Response([])
            return Response({"title": "T", "user": {"login": "alice"}})

    monkeypatch.setattr("github.httpx.AsyncClient", lambda **kwargs: Client())

    await fetch_pr_data("https://github.com/owner/repo/pull/7", "  ghp_test  ")

    assert seen_headers[0]["Authorization"] == "Bearer ghp_test"


def test_normalize_review_payload_bounds_and_defaults():
    review = normalize_review_payload(
        {
            "summary": "ok",
            "issues": [
                {"severity": "critical", "file": "a.py", "comment": "x"},
                "not-object",
            ],
            "suggestions": ["ship"],
            "verdict": "maybe",
            "verdict_reason": "reason",
        }
    )

    assert review["issues"] == [{"severity": "low", "file": "a.py", "comment": "x"}]
    assert review["verdict"] == "REQUEST CHANGES"


@pytest.mark.asyncio
async def test_review_pr_sanitizes_provider_exception(monkeypatch):
    class Client:
        async def chat_completion(self, **kwargs):
            raise RuntimeError("provider failed with hf_secret_token")

    monkeypatch.setenv("HF_TOKEN", "test-token")
    monkeypatch.setattr("agent.AsyncInferenceClient", lambda token: Client())

    result = await review_pr({"title": "Demo", "diff": "+print('hello')"})

    assert result == {"error": LLM_PROVIDER_ERROR}
    assert "hf_secret_token" not in result["error"]


@pytest.mark.asyncio
async def test_review_pr_treats_blank_hf_token_as_missing(monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "   ")

    result = await review_pr({"title": "Demo", "diff": "+print('hello')"})

    assert result == {"error": HF_TOKEN_MISSING_ERROR}


@pytest.mark.asyncio
async def test_review_pr_bounds_prompt_fields_before_hf_call(monkeypatch):
    captured = {}

    class Message:
        content = """{
            "summary": "ok",
            "issues": [],
            "suggestions": [],
            "verdict": "APPROVE",
            "verdict_reason": "safe"
        }"""

    class Choice:
        message = Message()

    class Response:
        choices = [Choice()]

    class Client:
        async def chat_completion(self, **kwargs):
            captured.update(kwargs)
            return Response()

    monkeypatch.setenv("HF_TOKEN", "test-token")
    monkeypatch.setattr("agent.AsyncInferenceClient", lambda token: Client())

    result = await review_pr(
        {
            "title": "T" * (MAX_TITLE_CHARS + 50),
            "author": "alice",
            "description": "D" * (MAX_DESCRIPTION_CHARS + 50),
            "commits": 1,
            "changed_files": 1,
            "additions": 1,
            "deletions": 0,
            "diff": "X" * (MAX_DIFF_CHARS + 50),
        }
    )

    user_prompt = captured["messages"][1]["content"]
    assert result["verdict"] == "APPROVE"
    assert "T" * (MAX_TITLE_CHARS + 1) not in user_prompt
    assert "D" * (MAX_DESCRIPTION_CHARS + 1) not in user_prompt
    assert "X" * (MAX_DIFF_CHARS + 1) not in user_prompt


def test_homepage_serves_static_ui():
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200
    assert "PR Review Agent" in response.text


def test_static_ui_normalizes_class_bound_review_fields():
    html = Path("static/index.html").read_text(encoding="utf-8")

    assert "placeholder=\"ghp_" not in html
    assert "function sanitizeSeverity" in html
    assert "const sev = sanitizeSeverity(issue.severity);" in html


def test_review_schema_rejects_oversized_url():
    client = TestClient(app)

    response = client.post("/review", json={"pr_url": "x" * 301})

    assert response.status_code == 422


def test_review_rejects_bad_url_without_echoing_secret():
    client = TestClient(app)

    response = client.post(
        "/review",
        json={"pr_url": "https://github.com/owner/repo/pull/not-a-number?token=redacted_secret"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == INVALID_PR_URL_ERROR
    assert "redacted_secret" not in response.text


def test_review_returns_503_when_hf_token_missing(monkeypatch):
    async def fake_fetch_pr_data(pr_url, github_token=None):
        return {"title": "Demo", "author": "alice"}

    async def fake_review_pr(pr_data):
        return {"error": "HF_TOKEN is not set in .env"}

    monkeypatch.setattr("main.fetch_pr_data", fake_fetch_pr_data)
    monkeypatch.setattr("main.review_pr", fake_review_pr)
    client = TestClient(app)

    response = client.post(
        "/review",
        json={"pr_url": "https://github.com/owner/repo/pull/1"},
    )

    assert response.status_code == 503


def test_review_normalizes_blank_request_github_token(monkeypatch):
    captured = {}

    async def fake_fetch_pr_data(pr_url, github_token=None):
        captured["github_token"] = github_token
        return {"title": "Demo", "author": "alice"}

    async def fake_review_pr(pr_data):
        return {
            "summary": "ok",
            "issues": [],
            "suggestions": [],
            "verdict": "APPROVE",
            "verdict_reason": "safe",
        }

    monkeypatch.setattr("main.fetch_pr_data", fake_fetch_pr_data)
    monkeypatch.setattr("main.review_pr", fake_review_pr)
    client = TestClient(app)

    response = client.post(
        "/review",
        json={
            "pr_url": "https://github.com/owner/repo/pull/1",
            "github_token": "   ",
        },
    )

    assert response.status_code == 200
    assert captured["github_token"] is None


def test_review_returns_502_for_provider_error(monkeypatch):
    async def fake_fetch_pr_data(pr_url, github_token=None):
        return {"title": "Demo", "author": "alice"}

    async def fake_review_pr(pr_data):
        return {"error": LLM_PROVIDER_ERROR}

    monkeypatch.setattr("main.fetch_pr_data", fake_fetch_pr_data)
    monkeypatch.setattr("main.review_pr", fake_review_pr)
    client = TestClient(app)

    response = client.post(
        "/review",
        json={"pr_url": "https://github.com/owner/repo/pull/1"},
    )

    assert response.status_code == 502
    assert response.json()["detail"] == LLM_PROVIDER_ERROR
