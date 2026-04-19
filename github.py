import re
import httpx

async def fetch_pr_data(pr_url: str, github_token: str = None) -> dict:
    # Parses a GitHub PR URL to extract owner, repo, pr_number using regex
    pattern = r"https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)"
    match = re.match(pattern, pr_url)
    if not match:
        raise ValueError(f"Invalid GitHub PR URL: {pr_url}")
        
    owner, repo, pr_number = match.groups()
    
    headers = {"Accept": "application/vnd.github.v3+json"}
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"
        
    base_url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}"
    files_url = f"{base_url}/files"
    
    # Calls https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number} for PR metadata
    async with httpx.AsyncClient() as client:
        pr_response = await client.get(base_url, headers=headers)
        if pr_response.status_code != 200:
            raise ValueError(f"Failed to fetch PR metadata: {pr_response.text}")
        pr_metadata = pr_response.json()
        
        # Calls the /files endpoint for changed files and diffs
        files_response = await client.get(files_url, headers=headers)
        if files_response.status_code != 200:
            raise ValueError(f"Failed to fetch PR files: {files_response.text}")
        pr_files = files_response.json()
        
    files_summary = []
    diff_patches = []
    
    # Process files (max 20 files)
    for file_info in pr_files[:20]:
        filename = file_info.get("filename", "")
        status = file_info.get("status", "")
        patch = file_info.get("patch", "")
        
        files_summary.append({"filename": filename, "status": status})
        if patch:
            diff_patches.append(f"--- a/{filename}\n+++ b/{filename}\n{patch}")
            
    # Concatenated patches, capped at 12000 chars
    diff = "\n\n".join(diff_patches)
    if len(diff) > 12000:
        diff = diff[:12000] + "\n... [Diff truncated]"
        
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
