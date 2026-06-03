import pytest
from fastapi.testclient import TestClient

from agent import normalize_review_payload
from github import fetch_pr_data, parse_github_pr_url
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
        "https://github.com/fastapi/fastapi/issues/123",
        "https://evil.example.com/fastapi/fastapi/pull/123",
        "https://github.com/fastapi/fastapi/pull/not-a-number",
        "file:///etc/passwd",
    ],
)
def test_parse_github_pr_url_rejects_non_pr_urls(url):
    with pytest.raises(ValueError):
        parse_github_pr_url(url)


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


def test_homepage_serves_static_ui():
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200
    assert "PR Review Agent" in response.text


def test_review_schema_rejects_oversized_url():
    client = TestClient(app)

    response = client.post("/review", json={"pr_url": "x" * 301})

    assert response.status_code == 422


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
