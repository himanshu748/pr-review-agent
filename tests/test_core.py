"""Core tests for the v2 metrics-driven PR review agent.

Covers PR URL validation, objective signal extraction, issue-ref parsing,
the deterministic health-score/severity-gate/verdict logic, cost estimation,
and the FastAPI surface. No network or LLM calls.
"""

import pytest
from fastapi.testclient import TestClient

import agent
from agent import (
    DIMENSIONS,
    SEVERITY,
    _assemble_review,
    _compute_health,
    _cost_estimate,
    _decide_verdict,
    _extract_json,
    _grade,
    _normalize_scores,
)
from github import compute_signals, extract_issue_refs, parse_pr_url
from main import app


# --- URL parsing -----------------------------------------------------------

def test_parse_pr_url_accepts_pull_url():
    assert parse_pr_url("https://github.com/fastapi/fastapi/pull/123") == (
        "fastapi", "fastapi", "123",
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/fastapi/fastapi/issues/123",
        "https://evil.example.com/fastapi/fastapi/pull/123",
        "https://github.com/fastapi/fastapi/pull/not-a-number",
        "file:///etc/passwd",
        "not a url",
    ],
)
def test_parse_pr_url_rejects_invalid(url):
    with pytest.raises(ValueError):
        parse_pr_url(url)


# --- issue-ref extraction ----------------------------------------------------

def test_extract_issue_refs_closing_and_mentions():
    closing, mentioned = extract_issue_refs(
        "This fixes #123 and relates to #456. Also closes https://github.com/o/r/issues/789"
    )
    assert closing == {123, 789}
    assert mentioned == {456}


def test_extract_issue_refs_empty_body():
    assert extract_issue_refs(None, "") == (set(), set())


# --- objective signals -------------------------------------------------------

def _mkfile(name, status="modified", add=10, rm=2, patch="@@\n+x = 1"):
    return {"filename": name, "status": status, "additions": add,
            "deletions": rm, "changes": add + rm, "patch": patch}


def test_compute_signals_classifies_tests_docs_deps():
    files = [
        _mkfile("src/app/main.py"),
        _mkfile("tests/test_main.py", status="added"),
        _mkfile("requirements.txt", patch="@@\n+requests==2.31.0"),
        _mkfile("docs/guide.md"),
    ]
    meta = {"changed_files": 4, "additions": 40, "deletions": 8, "commits": 2}
    s = compute_signals(files, meta, truncated=False)
    assert s["test_files"] == 1
    assert s["code_files"] == 1
    assert s["doc_files"] == 1
    assert s["new_dependencies"] == ["requests==2.31.0"]
    assert s["has_tests"] is True
    assert s["blast_radius"] == 4


def test_compute_signals_flags_missing_tests():
    s = compute_signals([_mkfile("src/x.py")], {"additions": 12, "deletions": 0}, False)
    assert any("no tests" in f.lower() for f in s["risk_flags"])


# --- scoring engine ----------------------------------------------------------

def test_dimension_weights_sum_to_one():
    assert round(sum(d["weight"] for d in DIMENSIONS), 6) == 1.0


def test_grade_bands():
    assert _grade(95) == "A"
    assert _grade(85) == "B"
    assert _grade(75) == "C"
    assert _grade(65) == "D"
    assert _grade(30) == "F"


def test_normalize_scores_clamps_and_defaults():
    raw = {"correctness": 150, "security": -5, "testing": "bad", "performance": None}
    scores = _normalize_scores(raw)
    assert scores["correctness"] == 100
    assert scores["security"] == 0
    assert scores["testing"] is None
    assert scores["performance"] is None
    assert set(scores) == {d["key"] for d in DIMENSIONS}


def _uniform_scores(v):
    return {d["key"]: v for d in DIMENSIONS}


def test_health_uncapped_when_no_issues():
    health, weighted, cap = _compute_health(_uniform_scores(90), {})
    assert health == 90 and cap == 100


def test_blocker_caps_health():
    health, _, cap = _compute_health(_uniform_scores(95), {"blocker": 1})
    assert cap == SEVERITY["blocker"]["gate"] == 39
    assert health <= 39


def test_major_caps_health():
    health, _, cap = _compute_health(_uniform_scores(95), {"major": 2})
    assert cap == 84 and health == 84


def test_weight_redistribution_with_na_dimension():
    scores = _uniform_scores(80)
    scores["guidelines"] = None
    health, _, _ = _compute_health(scores, {})
    assert health == 80  # remaining dims all 80 -> still 80 after renormalizing


def test_verdicts():
    assert _decide_verdict(95, {})["verdict"] == "APPROVE"
    assert _decide_verdict(85, {"minor": 1})["verdict"] == "APPROVE_WITH_NITS"
    assert _decide_verdict(80, {"major": 1})["verdict"] == "REQUEST_CHANGES"
    assert _decide_verdict(30, {"blocker": 1})["verdict"] == "BLOCK"
    assert _decide_verdict(45, {})["verdict"] == "BLOCK"


def test_assemble_review_forces_guidelines_na_without_contributing():
    parsed = {"summary": "s", "scores": _uniform_scores(90), "issues": []}
    review = _assemble_review(parsed, {"contributing": {"found": False}})
    gdim = next(d for d in review["scorecard"] if d["key"] == "guidelines")
    assert gdim["score"] is None and gdim["grade"] == "N/A"


def test_assemble_review_sorts_issues_by_severity():
    parsed = {
        "summary": "s",
        "scores": _uniform_scores(90),
        "issues": [
            {"severity": "minor", "file": "a", "comment": "c"},
            {"severity": "blocker", "file": "b", "comment": "c"},
            {"severity": "weird", "file": "c", "comment": "c"},  # -> minor
        ],
    }
    review = _assemble_review(parsed, {"contributing": {"found": True}})
    assert [i["severity"] for i in review["issues"]] == ["blocker", "minor", "minor"]
    assert review["verdict"] == "BLOCK"


def test_cost_estimate_floors_savings():
    cost = _cost_estimate(1400, 300, "test-model")
    assert 0 < cost["usd"] < 0.01
    assert cost["savings_pct"] < 100.0
    assert cost["claude_price_usd"] == agent.CLAUDE_REVIEW_PRICE


# --- JSON extraction ---------------------------------------------------------

def test_extract_json_plain_and_fenced():
    assert _extract_json('{"a": 1}') == {"a": 1}
    assert _extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert _extract_json('noise {"a": 1} trailing') == {"a": 1}
    assert _extract_json("not json at all") is None


# --- FastAPI surface ---------------------------------------------------------

client = TestClient(app)


def test_root_serves_ui():
    r = client.get("/")
    assert r.status_code == 200
    assert "PR" in r.text


def test_healthz():
    r = client.get("/healthz")
    assert r.status_code == 200
    assert "hf_token_configured" in r.json()


def test_review_rejects_invalid_url():
    r = client.post("/review", json={"pr_url": "https://example.com/nope"})
    assert r.status_code == 400
