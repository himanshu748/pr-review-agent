# AI PR Review Agent

A focused FastAPI app that fetches a GitHub pull request diff, sends a bounded review prompt to Hugging Face, and renders a structured review in a GitHub-style browser UI.

## Features

- **Automated PR Review**: Analyzes GitHub Pull Requests and provides a comprehensive review.
- **Detailed Insights**: Returns a summary, categorized issues (High/Medium/Low severity), and actionable suggestions.
- **Clear Verdicts**: Provides a final "APPROVE" or "REQUEST CHANGES" verdict with a clear reason.
- **Sleek UI**: A GitHub Dark Mode-inspired, fully responsive, single-page frontend.
- **Asynchronous & Fast**: Built with FastAPI and `httpx` for non-blocking API requests.
- **Safer Inputs**: Strict GitHub PR URL parsing, bounded request fields, capped file/diff payloads, and token-safe error messages.

## Prerequisites

- Python 3.8+
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
   HF_TOKEN=your_huggingface_token_here
   GITHUB_TOKEN=your_github_token_here_optional
   ```

## Usage

1. **Start the FastAPI server:**
   ```bash
   uvicorn main:app --reload --port 8000
   ```

2. **Access the UI:**
   Open your browser and navigate to `http://127.0.0.1:8000`.

3. **Review a PR:**
   - Enter a valid GitHub PR URL (e.g., `https://github.com/fastapi/fastapi/pull/1`).
   - Optionally, provide a GitHub token in the "Advanced Options" dropdown.
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

The tests cover PR URL validation, diff/file bounds, static UI serving, missing Hugging Face token behavior, provider-error sanitization, and review payload normalization.

## Technology Stack

- **Backend:** FastAPI, Uvicorn, httpx
- **AI/LLM:** Hugging Face Inference API (`Qwen/Qwen2.5-72B-Instruct`)
- **Frontend:** Pure HTML/CSS/JavaScript (No external UI libraries)

## License

This project is licensed under the MIT License.
