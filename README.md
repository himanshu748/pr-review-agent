# AI PR Review Agent

A focused FastAPI app that fetches a GitHub pull request diff, sends a bounded review prompt to Hugging Face, and renders a structured review in a GitHub-style browser UI.

## Features

- **Automated PR Review**: Analyzes GitHub Pull Requests and provides a comprehensive review.
- **Detailed Insights**: Returns a summary, categorized issues (High/Medium/Low severity), and actionable suggestions.
- **Clear Verdicts**: Provides a final "APPROVE" or "REQUEST CHANGES" verdict with a clear reason.
- **Sleek UI**: A GitHub Dark Mode-inspired, fully responsive, single-page frontend.
- **Asynchronous & Fast**: Built with FastAPI and `httpx` for non-blocking API requests.
- **Safer Inputs**: Strict GitHub PR URL parsing, bounded request fields, capped file/diff payloads, bounded LLM prompt fields, and token-safe error messages.

## Prerequisites

- Python 3.10+
- A Hugging Face account and API Token
- A GitHub account and Personal Access Token (Optional, but recommended for higher rate limits and accessing private repositories)

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

3. **Configure Environment Variables:**
   Create a `.env` file in the root directory of the project and add your API tokens:
   ```env
   HF_TOKEN=
   # Optional aliases/settings:
   HF_API_KEY=
   HF_MODEL=Qwen/Qwen2.5-72B-Instruct
   GITHUB_TOKEN=
   ```

## Usage

1. **Start the FastAPI server:**
   ```bash
   uvicorn main:app --reload --port 8000
   ```

2. **Access the UI:**
   Open your browser and navigate to `http://127.0.0.1:8000`.

3. **Review a PR:**
   - Enter a valid HTTPS GitHub PR URL (e.g., `https://github.com/fastapi/fastapi/pull/1`).
   - Optionally, provide a GitHub token in the "Advanced Options" dropdown. The token is sent only with that request and is not stored by the app.
   - Click **Review PR** and wait for the AI's analysis.

## API Endpoints

### `GET /`
Serves the static frontend UI.

### `POST /review`
Accepts a JSON payload to trigger the PR review process.
- **Request Body:**
  ```json
  {
    "pr_url": "https://github.com/owner/repo/pull/123",
    "github_token": "optional_token_string"
}
  ```

## Verification

```bash
python -m pytest
python -m compileall agent.py github.py main.py tests
```

The tests cover PR URL validation, diff/file bounds, static UI serving, Hugging Face token/model configuration, provider-error sanitization, and review payload normalization.

Optional live smoke checks use local environment variables only and should never print token values:

```bash
python - <<'PY'
from huggingface_hub import whoami
print({"hf": "pass", "name": whoami().get("name")})
PY
```

```bash
python - <<'PY'
import asyncio
from agent import review_pr

async def main():
    result = await review_pr({
        "title": "Tiny safety check",
        "author": "local",
        "description": "A tiny PR smoke test.",
        "commits": 1,
        "changed_files": 1,
        "additions": 1,
        "deletions": 0,
        "diff": "--- a/demo.py\n+++ b/demo.py\n@@\n+print('hello')",
    })
    print({"hf_completion": "pass" if "error" not in result else result["error"]})

asyncio.run(main())
PY
```

## Security Notes

- Invalid PR URL errors are generic and do not echo user input, so accidental token-like strings in submitted URLs are not reflected back to the browser.
- Only `https://github.com/{owner}/{repo}/pull/{number}` URLs are accepted.
- `HF_TOKEN` or `HF_API_KEY` may be used for Hugging Face. `HF_MODEL` is optional and defaults to `Qwen/Qwen2.5-72B-Instruct`.
- Optional GitHub tokens are passed as Authorization headers only when provided and are not displayed in UI errors.

## Technology Stack

- **Backend:** FastAPI, Uvicorn, httpx
- **AI/LLM:** Hugging Face Inference API (`Qwen/Qwen2.5-72B-Instruct`)
- **Frontend:** Pure HTML/CSS/JavaScript (No external UI libraries)

## License

This project is licensed under the MIT License.
