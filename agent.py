"""Metrics-driven PR review.

The model scores the PR across weighted quality dimensions and reports issues on a
single unified severity scale. It is always grounded in the repo's own
CONTRIBUTING.md. The headline *PR Health Score* and *verdict* are then computed
deterministically in Python from those sub-scores + severity gates — so the
number is reproducible and cannot be inflated by the model just asserting a good
overall grade.
"""

import os
import re
import json

from dotenv import load_dotenv
from huggingface_hub import AsyncInferenceClient

load_dotenv()

DEFAULT_MODEL = "Qwen/Qwen2.5-72B-Instruct"

# Rough open-model inference pricing for the cost-per-review estimate (USD per
# 1M tokens; typical hosted rates for a 72B-class open model). Estimates only.
PRICE_IN_PER_M = 0.40
PRICE_OUT_PER_M = 0.80
CLAUDE_REVIEW_PRICE = 25.0  # what Claude charges per PR review — our USP anchor

# --- Metrics framework ----------------------------------------------------

# Canonical scored dimensions. Weights sum to 1.0. If a dimension is not
# applicable to a given PR (e.g. no CONTRIBUTING.md), the model returns null and
# its weight is redistributed across the rest.
DIMENSIONS = [
    {"key": "correctness",     "label": "Correctness & Logic",      "weight": 0.20,
     "desc": "bugs, edge cases, null/None handling, race conditions, off-by-one, incorrect logic"},
    {"key": "security",        "label": "Security",                 "weight": 0.15,
     "desc": "injection, secrets/credentials, authz/authn, unsafe deserialization, SSRF, path traversal, input validation"},
    {"key": "testing",         "label": "Testing",                  "weight": 0.15,
     "desc": "behavioral coverage of the change, edge/negative/error-path tests, test quality"},
    {"key": "reliability",     "label": "Reliability & Errors",     "weight": 0.12,
     "desc": "error handling, silent failures, resource leaks, timeouts/retries, logging/observability, rollback safety"},
    {"key": "maintainability", "label": "Maintainability",          "weight": 0.12,
     "desc": "readability, naming, duplication (DRY), function/file length, nesting depth, complexity"},
    {"key": "guidelines",      "label": "Guideline Compliance",     "weight": 0.12,
     "desc": "adherence to the repo's CONTRIBUTING.md (commit/PR conventions, required tests, style, sign-off, etc.)"},
    {"key": "performance",     "label": "Performance",              "weight": 0.08,
     "desc": "algorithmic complexity, N+1 queries, unnecessary allocations, blocking calls on hot paths"},
    {"key": "documentation",   "label": "Documentation",            "weight": 0.06,
     "desc": "docstrings/comments accuracy, public API docs, changelog/README updates where warranted"},
]
DIMENSION_KEYS = [d["key"] for d in DIMENSIONS]

# Unified severity scale (replaces the old high/medium/low). Numeric weight is
# used for issue-density metrics; the gate is the hard cap it imposes on health.
SEVERITY = {
    "blocker":  {"weight": 5, "gate": 39},   # must not merge (data loss, security hole, broken build)
    "critical": {"weight": 4, "gate": 59},   # serious bug/security; breaks core functionality
    "major":    {"weight": 3, "gate": 84},   # important; should fix before merge
    "minor":    {"weight": 2, "gate": 100},  # small, non-blocking
    "info":     {"weight": 1, "gate": 100},  # nit / suggestion
}
SEVERITY_ORDER = ["blocker", "critical", "major", "minor", "info"]


def _grade(score: int) -> str:
    if score >= 90: return "A"
    if score >= 80: return "B"
    if score >= 70: return "C"
    if score >= 60: return "D"
    return "F"


def _normalize_scores(raw_scores: dict) -> dict:
    """Return {key: score|None} for every canonical dimension, clamped 0-100."""
    out = {}
    for key in DIMENSION_KEYS:
        val = (raw_scores or {}).get(key)
        if isinstance(val, dict):
            val = val.get("score")
        if val is None:
            out[key] = None
            continue
        try:
            out[key] = max(0, min(100, int(round(float(val)))))
        except (TypeError, ValueError):
            out[key] = None
    return out


def _compute_health(scores: dict, severity_counts: dict):
    """Weighted composite over applicable dimensions, capped by severity gates."""
    applicable = [(d, scores[d["key"]]) for d in DIMENSIONS if scores.get(d["key"]) is not None]
    total_weight = sum(d["weight"] for d, _ in applicable)
    if total_weight == 0:
        weighted = 0
    else:
        weighted = sum(s * d["weight"] for d, s in applicable) / total_weight

    cap = 100
    for sev in SEVERITY_ORDER:
        if severity_counts.get(sev, 0) > 0:
            cap = min(cap, SEVERITY[sev]["gate"])
    health = int(round(min(weighted, cap)))
    return health, round(weighted, 1), cap


def _decide_verdict(health: int, severity_counts: dict) -> dict:
    blockers = severity_counts.get("blocker", 0)
    criticals = severity_counts.get("critical", 0)
    majors = severity_counts.get("major", 0)

    if blockers > 0 or health < 50:
        v, reason = "BLOCK", "Contains blocking issues or a failing health score; do not merge."
    elif criticals > 0 or health < 70 or (majors > 0 and health < 85):
        v, reason = "REQUEST_CHANGES", "Has critical/major issues that should be resolved before merge."
    elif health < 90 or majors > 0:
        v, reason = "APPROVE_WITH_NITS", "Solid overall; only minor issues or nits remain."
    else:
        v, reason = "APPROVE", "High quality with no significant issues found."
    return {"verdict": v, "reason": reason}


def _count_severities(issues: list) -> dict:
    counts = {s: 0 for s in SEVERITY_ORDER}
    for issue in issues:
        sev = str(issue.get("severity", "minor")).lower().strip()
        if sev not in counts:
            sev = "minor"
        issue["severity"] = sev
        counts[sev] += 1
    return counts


def _issue_block(issue_link: dict) -> str:
    if not issue_link:
        return "Issue linkage: unknown."
    if not issue_link.get("has_linked_issue"):
        return (
            "ISSUE LINKAGE: This PR references NO issue (no 'Fixes #N' / 'Closes #N' and no "
            "#N mentions resolved to an issue). If the contribution guidelines require PRs to "
            "be tied to an issue, flag this as a guidelines violation."
        )
    lines = ["ISSUE LINKAGE (verified via GitHub API):"]
    for i in issue_link.get("issues", []):
        kind = "closes" if i["closing"] else "mentions"
        assignees = ", ".join(i["assignees"]) or "nobody"
        assigned = "PR author IS assigned" if i["author_assigned"] else "PR author is NOT assigned"
        lines.append(
            f"- {kind} #{i['number']} \"{i['title']}\" [{i['state']}] — assignees: {assignees}; {assigned}"
        )
    lines.append(
        "If the guidelines require contributors to be assigned to an issue before submitting a PR "
        "and the author is not assigned, flag it as a guidelines violation."
    )
    return "\n".join(lines)


def _build_prompt(pr_data: dict) -> str:
    s = pr_data.get("signals", {})
    contributing = pr_data.get("contributing", {}) or {}
    langs = ", ".join(f"{l['language']}({l['files']})" for l in s.get("languages", [])[:6]) or "n/a"

    guideline_block = (
        f"The repository's CONTRIBUTING.md ({contributing.get('path')}):\n"
        f"\"\"\"\n{contributing.get('content')}\n\"\"\"\n"
        "Judge the 'guidelines' dimension against THESE rules and list concrete violations."
        if contributing.get("found")
        else "No CONTRIBUTING.md was found in this repository. Set the 'guidelines' dimension "
             "score to null and leave guideline_violations empty."
    )

    dims_desc = "\n".join(f'- "{d["key"]}": {d["label"]} — {d["desc"]}' for d in DIMENSIONS)

    return f"""You are a rigorous senior engineer reviewing a GitHub pull request. Follow this
order: (1) the project's contribution guidelines, (2) issue linkage / assignment,
(3) the diff itself.

{guideline_block}

{_issue_block(pr_data.get('issue_link'))}

PR TITLE: {pr_data.get('title')}
AUTHOR: {pr_data.get('author')}
DESCRIPTION:
{(pr_data.get('description') or '(none)')[:1500]}

OBJECTIVE SIGNALS (measured, for context — do not recompute):
- files changed: {s.get('files_changed')} | +{s.get('additions')}/-{s.get('deletions')} lines | commits: {s.get('commits')}
- languages: {langs}
- tests: {s.get('test_files')} test file(s), test-to-code line ratio ~{s.get('test_to_code_ratio')}
- new dependencies: {s.get('new_dependencies') or 'none'}
- blast radius: {s.get('blast_radius')} module(s) — {', '.join(s.get('modules_touched', [])[:8])}
- flags: {'; '.join(s.get('risk_flags', [])) or 'none'}

DIFF:
{pr_data.get('diff')}
{'[Note: diff truncated — score conservatively on unseen files.]' if pr_data.get('diff_truncated') else ''}

Return ONLY a JSON object (no markdown fences, no prose) with exactly these keys:
{{
  "summary": "2-3 sentence plain-English summary of what this PR does",
  "scores": {{
     // integer 0-100 for each key below, or null if truly not applicable to this PR.
     // 90-100 excellent, 70-89 good, 50-69 needs work, 0-49 poor.
{dims_desc}
  }},
  "issues": [
    {{
      "severity": "blocker|critical|major|minor|info",
      "category": "one of: correctness|security|testing|reliability|maintainability|guidelines|performance|documentation",
      "file": "path/to/file",
      "line": 123,            // integer line number if identifiable, else null
      "comment": "what is wrong and why it matters",
      "suggestion": "concrete fix"
    }}
  ],
  "guideline_violations": ["specific CONTRIBUTING.md rules this PR breaks, empty if none/no file"],
  "highlights": ["things this PR does well"],
  "suggestions": ["optional non-blocking improvements"]
}}

Severity rules: blocker = must not merge (data loss, security hole, broken build);
critical = serious bug/security; major = important, fix before merge; minor = small;
info = nit. Be precise and avoid false positives. Every score must be justified by the diff."""


async def review_pr(pr_data: dict) -> dict:
    hf_token = os.getenv("HF_TOKEN")
    if not hf_token or hf_token == "your_huggingface_token_here":
        return {"error": "HF_TOKEN is not set in .env"}

    model = os.getenv("HF_MODEL", DEFAULT_MODEL)
    client = AsyncInferenceClient(token=hf_token)

    messages = [
        {"role": "system", "content": "You are a senior software engineer performing a thorough, metrics-driven PR review. You return only valid JSON."},
        {"role": "user", "content": _build_prompt(pr_data)},
    ]

    try:
        response = await client.chat_completion(model=model, messages=messages, max_tokens=3000, temperature=0.2)
        content = response.choices[0].message.content.strip()
    except Exception as e:
        return {"error": f"LLM error: {str(e)}"}

    parsed = _extract_json(content)
    if parsed is None:
        return {"error": "Failed to parse JSON response from LLM.", "raw_content": content[:2000]}

    usage = getattr(response, "usage", None)
    tokens_in = getattr(usage, "prompt_tokens", None) or len(messages[1]["content"]) // 4
    tokens_out = getattr(usage, "completion_tokens", None) or len(content) // 4
    cost = _cost_estimate(tokens_in, tokens_out, model)

    return _assemble_review(parsed, pr_data, cost)


def _cost_estimate(tokens_in: int, tokens_out: int, model: str) -> dict:
    usd = tokens_in / 1e6 * PRICE_IN_PER_M + tokens_out / 1e6 * PRICE_OUT_PER_M
    usd = round(max(usd, 0.0001), 4)
    return {
        "model": model,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "usd": usd,
        "claude_price_usd": CLAUDE_REVIEW_PRICE,
        # floor, not round — 99.9968% must show 99.99%, never a false "100%"
        "savings_pct": int((1 - usd / CLAUDE_REVIEW_PRICE) * 10000) / 100,
        "note": "estimated from token counts at typical open-model hosting rates",
    }


def _extract_json(content: str):
    """Robustly pull a JSON object out of the model response."""
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                return None
    return None


def _assemble_review(parsed: dict, pr_data: dict, cost: dict = None) -> dict:
    contributing = pr_data.get("contributing", {}) or {}
    issue_link = pr_data.get("issue_link", {}) or {}
    scores = _normalize_scores(parsed.get("scores", {}))

    # Force guidelines N/A when no CONTRIBUTING.md exists (don't trust the model).
    if not contributing.get("found"):
        scores["guidelines"] = None

    issues = parsed.get("issues") or []
    if not isinstance(issues, list):
        issues = []
    severity_counts = _count_severities(issues)
    # Sort issues most-severe first.
    issues.sort(key=lambda i: SEVERITY_ORDER.index(i.get("severity", "minor")))

    health, weighted_raw, cap = _compute_health(scores, severity_counts)
    verdict = _decide_verdict(health, severity_counts)

    scorecard = [
        {
            "key": d["key"],
            "label": d["label"],
            "weight": d["weight"],
            "score": scores[d["key"]],
            "grade": _grade(scores[d["key"]]) if scores[d["key"]] is not None else "N/A",
        }
        for d in DIMENSIONS
    ]

    return {
        "summary": parsed.get("summary", ""),
        "health_score": health,
        "health_grade": _grade(health),
        "weighted_raw": weighted_raw,
        "score_cap": cap,
        "capped": weighted_raw > cap,  # cap actually binding (not just rounding)
        "verdict": verdict["verdict"],
        "verdict_reason": verdict["reason"],
        "scorecard": scorecard,
        "severity_counts": severity_counts,
        "total_issues": len(issues),
        "issues": issues,
        "guideline": {
            "found": bool(contributing.get("found")),
            "path": contributing.get("path"),
            "url": contributing.get("html_url"),
            "violations": parsed.get("guideline_violations") or [],
        },
        "process": {
            "contributing_checked": True,
            "contributing_found": bool(contributing.get("found")),
            "has_linked_issue": bool(issue_link.get("has_linked_issue")),
            "author_assigned": bool(issue_link.get("author_assigned_to_any")),
            "issues": issue_link.get("issues", []),
            "pr_author": issue_link.get("pr_author", ""),
        },
        "cost": cost,
        "highlights": parsed.get("highlights") or [],
        "suggestions": parsed.get("suggestions") or [],
    }
