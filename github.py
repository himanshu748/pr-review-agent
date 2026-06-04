from __future__ import annotations

import re
from urllib.parse import urlparse

import httpx


MAX_FILES = 20
MAX_DIFF_CHARS = 12_000
REQUEST_TIMEOUT = 20.0
PR_URL_PATTERN = re.compile(r"^/([^/]+)/([^/]+)/pull/(\d+)/?$")


def parse_github_pr_url(pr_url: str) -> tuple[str, str, str]:
    parsed = urlparse(pr_url.strip())
    if parsed.scheme not in {"https", "http"} or parsed.netloc.lower() != "github.com":
        raise ValueError(f"Invalid GitHub PR URL: {pr_url}")
    match = PR_URL_PATTERN.match(parsed.path)
    if not match:
        raise ValueError(f"Invalid GitHub PR URL: {pr_url}")
    return match.groups()


def github_error_message(response: httpx.Response, label: str) -> str:
    try:
        payload = response.json()
        message = payload.get("message") if isinstance(payload, dict) else None
    except ValueError:
        message = None
    safe_message = f": {message}" if message else ""
    return f"Failed to fetch PR {label} from GitHub (HTTP {response.status_code}){safe_message}"


async def fetch_pr_data(pr_url: str, github_token: str | None = None) -> dict:
    owner, repo, pr_number = parse_github_pr_url(pr_url)

    headers = {"Accept": "application/vnd.github.v3+json"}
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"
        
    base_url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}"
    files_url = f"{base_url}/files"
    
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=False) as client:
        pr_response = await client.get(base_url, headers=headers)
        if pr_response.status_code != 200:
            raise ValueError(github_error_message(pr_response, "metadata"))
        pr_metadata = pr_response.json()

        files_response = await client.get(files_url, headers=headers)
        if files_response.status_code != 200:
            raise ValueError(github_error_message(files_response, "files"))
        pr_files = files_response.json()

    files_summary = []
    diff_patches = []

    for file_info in pr_files[:MAX_FILES]:
        filename = file_info.get("filename", "")
        status = file_info.get("status", "")
        patch = file_info.get("patch", "")
        
        files_summary.append({"filename": filename, "status": status})
        if patch:
            diff_patches.append(f"--- a/{filename}\n+++ b/{filename}\n{patch}")
            
    diff = "\n\n".join(diff_patches)
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + "\n... [Diff truncated]"

    return {
        "title": pr_metadata.get("title", ""),
        "description": pr_metadata.get("body", ""),
        "author": pr_metadata.get("user", {}).get("login", ""),
        "base_branch": pr_metadata.get("base", {}).get("ref", ""),
        "head_branch": pr_metadata.get("head", {}).get("ref", ""),
        "commits": pr_metadata.get("commits", 0),
        "changed_files": pr_metadata.get("changed_files", 0),
        "additions": pr_metadata.get("additions", 0),
        "deletions": pr_metadata.get("deletions", 0),
        "files_summary": files_summary,
        "diff": diff
    }
