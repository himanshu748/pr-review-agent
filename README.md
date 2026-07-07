# ReviewCheap — AI PR Review Agent

Senior-grade, metrics-driven PR reviews on **open models** for **less than a cent per review** — Claude charges **$25 per PR review** for the same job. Built with FastAPI and powered by Hugging Face's Inference API; every review displays its actual token cost next to the $25 anchor.

## Review pipeline (process-first)

1. **Read CONTRIBUTING.md first** — located via the community-profile API (including org-level `.github` repos) and fed to the model; the PR is graded against the project's own rules.
2. **Verify issue linkage** — parses `Fixes/Closes #N` refs, resolves them via the GitHub API, and checks whether the **PR author is assigned** to the linked issue.
3. **Measure the diff** — objective signals, no LLM.
4. **Score 8 weighted dimensions** — composite Health Score computed deterministically in Python with severity hard-caps.

## What it does

For any GitHub PR URL, the agent produces a full review dashboard:

- **PR Health Score (0–100) + letter grade** — a weighted composite of eight quality dimensions, computed deterministically in Python (not asserted by the model), and **hard-capped by open issues** so a good-looking diff can't outscore a blocker.
- **Dimension Scorecard** — each dimension scored 0–100 by the LLM with its weight shown:

  | Dimension | Weight |
  |---|---|
  | Correctness & Logic | 20% |
  | Security | 15% |
  | Testing | 15% |
  | Reliability & Errors | 12% |
  | Maintainability | 12% |
  | Guideline Compliance (CONTRIBUTING.md) | 12% |
  | Performance | 8% |
  | Documentation | 6% |

  Dimensions that don't apply return `N/A` and their weight is redistributed.
- **Contribution-guideline grounding** — the repo's CONTRIBUTING.md is located first (community-profile API, org-level `.github` repos, and conventional paths), fed to the model, and violations are listed explicitly.
- **Objective signals** (measured, no LLM): lines +/-, churn, languages, test-to-code ratio, new dependencies parsed from manifests, blast radius (modules touched), CI/migration changes, and risk flags.
- **Unified severity scale** — `blocker / critical / major / minor / info`, each capping the health score (blocker → ≤39, critical → ≤59, major → ≤84).
- **Four-tier verdict** — `APPROVE`, `APPROVE_WITH_NITS`, `REQUEST_CHANGES`, `BLOCK`, derived from health score + severity gates.
- **Issues with fixes** — every issue carries severity, category, file:line, explanation, and a concrete suggestion.

## Prerequisites

- Python 3.9+
- A Hugging Face account and API token
- A GitHub token (optional — for private repos and higher rate limits)

## Setup & Installation

1. **Clone the repository:**
   ```bash
   git clone https://github.com/himanshu748/pr-review-agent.git
   cd pr-review-agent
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure environment variables** in a `.env` file:
   ```env
   HF_TOKEN=your_huggingface_token_here
   GITHUB_TOKEN=your_github_token_here_optional
   HF_MODEL=Qwen/Qwen2.5-72B-Instruct   # optional override
   ```

## Usage

1. **Start the server:**
   ```bash
   uvicorn main:app --reload --port 8000
   ```

2. **Open** `http://127.0.0.1:8000`, paste a PR URL (e.g. `https://github.com/fastapi/fastapi/pull/1`), and click **Review PR**.

## API

### `POST /review`
```json
{
  "pr_url": "https://github.com/owner/repo/pull/123",
  "github_token": "optional_token_string"
}
```

Response shape:
```json
{
  "metadata":  { "title": "...", "author": "...", "commits": 3, "...": "..." },
  "signals":   { "net_churn": 240, "test_to_code_ratio": 0.6, "new_dependencies": [], "blast_radius": 3, "risk_flags": [], "...": "..." },
  "review": {
    "health_score": 84, "health_grade": "B",
    "verdict": "REQUEST_CHANGES", "verdict_reason": "...",
    "scorecard": [ { "key": "correctness", "label": "Correctness & Logic", "weight": 0.2, "score": 90, "grade": "A" } ],
    "severity_counts": { "blocker": 0, "critical": 0, "major": 1, "minor": 1, "info": 0 },
    "issues": [ { "severity": "major", "category": "guidelines", "file": "x.py", "line": 30, "comment": "...", "suggestion": "..." } ],
    "guideline": { "found": true, "path": "CONTRIBUTING.md", "url": "...", "violations": ["..."] },
    "highlights": ["..."], "suggestions": ["..."]
  }
}
```

### `GET /healthz`
Liveness + whether an HF token is configured.

## How the score is computed

1. The LLM scores each dimension 0–100 from the diff (guided by CONTRIBUTING.md and the objective signals) and reports issues on the unified severity scale.
2. Python computes `health = Σ(score × weight) / Σ(weights of applicable dimensions)`.
3. The worst open severity caps the result (`blocker`→39, `critical`→59, `major`→84).
4. The verdict follows from health + severity counts — so the model cannot "talk its way" into an APPROVE.

## Technology Stack

- **Backend:** FastAPI, Uvicorn, httpx
- **AI/LLM:** Hugging Face Inference API (`Qwen/Qwen2.5-72B-Instruct` by default, override with `HF_MODEL`)
- **Frontend:** Pure HTML/CSS/JavaScript (no external UI libraries)

## License

MIT
