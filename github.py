"""GitHub PR ingestion + objective signal extraction.

This module fetches a pull request from the GitHub REST API and derives a set of
*objective*, deterministic metrics ("signals") straight from the file list — no
LLM involved. These signals (churn, language mix, test-to-code ratio, new
dependencies, blast radius, ...) are trustworthy because they are measured, not
guessed, and they feed both the model prompt (as grounding context) and the
frontend dashboard.
"""

import re
import os
import base64
from collections import defaultdict

import httpx

# How many files to pull for signal analysis (GitHub returns <=100 per page).
MAX_FILES_FOR_SIGNALS = 300
# How many file patches to include in the diff sent to the model, and the char cap.
MAX_DIFF_FILES = 50
DIFF_CHAR_CAP = 16000
# Cap on the contribution-guidelines excerpt fed to the model.
CONTRIBUTING_CHAR_CAP = 6000

# Where a repo's contribution guidelines commonly live (checked in order as a
# fallback; the community-profile endpoint is tried first and finds it anywhere).
CONTRIBUTING_CANDIDATES = [
    "CONTRIBUTING.md", ".github/CONTRIBUTING.md", "docs/CONTRIBUTING.md",
    "CONTRIBUTING.rst", "CONTRIBUTING", "CONTRIBUTING.txt", "contributing.md",
]

PR_URL_RE = re.compile(r"https?://github\.com/([^/\s]+)/([^/\s]+)/pull/(\d+)")

# --- classification tables ------------------------------------------------

LANGUAGE_BY_EXT = {
    "py": "Python", "js": "JavaScript", "jsx": "JavaScript", "ts": "TypeScript",
    "tsx": "TypeScript", "go": "Go", "rs": "Rust", "java": "Java", "kt": "Kotlin",
    "rb": "Ruby", "php": "PHP", "cs": "C#", "cpp": "C++", "cc": "C++", "cxx": "C++",
    "c": "C", "h": "C/C++ Header", "hpp": "C/C++ Header", "swift": "Swift",
    "scala": "Scala", "m": "Objective-C", "sh": "Shell", "bash": "Shell",
    "sql": "SQL", "html": "HTML", "css": "CSS", "scss": "CSS", "vue": "Vue",
    "svelte": "Svelte", "dart": "Dart", "ex": "Elixir", "exs": "Elixir",
    "yml": "YAML", "yaml": "YAML", "json": "JSON", "toml": "TOML", "md": "Markdown",
    "rst": "reStructuredText", "tf": "Terraform",
}

DOC_EXTS = {"md", "rst", "txt", "adoc"}
CONFIG_EXTS = {"yml", "yaml", "json", "toml", "ini", "cfg", "env", "lock"}

TEST_PATH_RE = re.compile(r"(^|/)(tests?|__tests__|spec|specs)(/|$)", re.IGNORECASE)
TEST_FILE_RE = re.compile(
    r"(_test\.|\.test\.|\.spec\.|_spec\.|(^|/)test_[^/]+\.py$|_test\.go$)",
    re.IGNORECASE,
)

# Dependency manifests -> the ones we parse for *added* deps (locks are generated).
DEP_MANIFESTS = {
    "requirements.txt", "requirements-dev.txt", "pyproject.toml", "setup.py",
    "package.json", "go.mod", "cargo.toml", "gemfile", "pom.xml",
    "build.gradle", "composer.json",
}
LOCKFILES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "cargo.lock", "gemfile.lock", "go.sum", "composer.lock",
}
# Sensitive / high-blast-radius paths worth flagging objectively.
CI_PATH_RE = re.compile(r"(^|/)\.github/workflows/|(^|/)\.gitlab-ci|Dockerfile|(^|/)\.circleci/", re.IGNORECASE)
MIGRATION_PATH_RE = re.compile(r"(^|/)migrations?/|(^|/)alembic/", re.IGNORECASE)


def parse_pr_url(pr_url: str):
    match = PR_URL_RE.match(pr_url.strip())
    if not match:
        raise ValueError(f"Invalid GitHub PR URL: {pr_url}")
    return match.groups()  # owner, repo, pr_number


def _ext(filename: str) -> str:
    base = filename.rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[-1].lower() if "." in base else ""


def _is_test(filename: str) -> bool:
    return bool(TEST_PATH_RE.search(filename) or TEST_FILE_RE.search(filename))


def _is_doc(filename: str) -> bool:
    if filename.rsplit("/", 1)[-1].lower() in DEP_MANIFESTS:
        return False  # requirements.txt etc. are dependency manifests, not docs
    return _ext(filename) in DOC_EXTS or "/docs/" in f"/{filename}"


def _top_module(filename: str) -> str:
    parts = filename.split("/")
    return parts[0] if len(parts) > 1 else "(root)"


def _added_lines(patch: str):
    """Yield added source lines (leading '+' stripped), skipping the '+++' header."""
    if not patch:
        return
    for line in patch.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            yield line[1:]


def _extract_new_deps(filename: str, patch: str):
    """Best-effort parse of newly added dependencies from a manifest patch."""
    name = filename.rsplit("/", 1)[-1].lower()
    deps = []
    for line in _added_lines(patch):
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("//"):
            continue
        if name in ("requirements.txt", "requirements-dev.txt"):
            m = re.match(r"^([A-Za-z0-9_.\-]+)\s*([=<>!~].*)?$", s)
            if m:
                deps.append(m.group(1) + (m.group(2) or ""))
        elif name in ("package.json", "composer.json"):
            m = re.match(r'^"([^"]+)"\s*:\s*"([^"]+)"', s)
            if m and m.group(1) not in ("name", "version", "description", "license"):
                deps.append(f"{m.group(1)}@{m.group(2)}")
        elif name == "go.mod":
            m = re.match(r"^([^\s]+/[^\s]+)\s+v[0-9]", s)
            if m:
                deps.append(m.group(1))
        elif name in ("cargo.toml", "pyproject.toml"):
            m = re.match(r'^([A-Za-z0-9_.\-]+)\s*=\s*[\{"]', s)
            if m:
                deps.append(m.group(1))
    return deps


def compute_signals(pr_files: list, pr_metadata: dict, truncated: bool) -> dict:
    """Derive objective, deterministic metrics from the PR file list."""
    by_status = defaultdict(int)
    lang_files = defaultdict(int)
    lang_changes = defaultdict(int)
    modules = set()

    test_files = code_files = doc_files = 0
    test_lines = code_lines = doc_lines = 0
    binary_generated = 0
    new_deps = []
    dep_files_touched = []
    largest = {"filename": None, "changes": 0}
    ci_touched = migration_touched = False

    for f in pr_files:
        filename = f.get("filename", "")
        status = f.get("status", "modified")
        additions = f.get("additions", 0) or 0
        deletions = f.get("deletions", 0) or 0
        changes = f.get("changes", additions + deletions) or 0
        patch = f.get("patch", "")

        by_status[status] += 1
        modules.add(_top_module(filename))
        ext = _ext(filename)
        lang = LANGUAGE_BY_EXT.get(ext, "Other")
        lang_files[lang] += 1
        lang_changes[lang] += changes

        if changes > largest["changes"]:
            largest = {"filename": filename, "changes": changes}

        base = filename.rsplit("/", 1)[-1].lower()
        if base in LOCKFILES or (not patch and status != "removed"):
            binary_generated += 1
        if base in DEP_MANIFESTS:
            dep_files_touched.append(filename)
            new_deps.extend(_extract_new_deps(filename, patch))
        if CI_PATH_RE.search(filename):
            ci_touched = True
        if MIGRATION_PATH_RE.search(filename):
            migration_touched = True

        if _is_test(filename):
            test_files += 1
            test_lines += changes
        elif _is_doc(filename):
            doc_files += 1
            doc_lines += changes
        elif ext not in CONFIG_EXTS and lang not in ("Other", "JSON", "YAML", "TOML"):
            code_files += 1
            code_lines += changes

    additions = pr_metadata.get("additions", 0) or 0
    deletions = pr_metadata.get("deletions", 0) or 0
    ratio = round(test_lines / code_lines, 2) if code_lines else (0.0 if test_lines == 0 else None)

    languages = sorted(
        ({"language": k, "files": lang_files[k], "changes": lang_changes[k]} for k in lang_files),
        key=lambda x: x["changes"], reverse=True,
    )

    # Objective, non-scored risk flags (heuristics on measured data).
    flags = []
    if code_lines > 0 and test_lines == 0:
        flags.append("Code changed but no tests were added or modified")
    if additions + deletions >= 1000:
        flags.append("Large diff (>=1000 lines) — harder to review thoroughly")
    if len(modules) >= 6:
        flags.append(f"Wide blast radius — touches {len(modules)} top-level modules")
    if new_deps:
        flags.append(f"Adds/updates {len(new_deps)} dependenc{'y' if len(new_deps) == 1 else 'ies'}")
    if ci_touched:
        flags.append("Modifies CI / build / container configuration")
    if migration_touched:
        flags.append("Includes database migrations — verify backward compatibility")

    return {
        "files_changed": pr_metadata.get("changed_files", len(pr_files)),
        "files_analyzed": len(pr_files),
        "truncated": truncated,
        "additions": additions,
        "deletions": deletions,
        "net_churn": additions + deletions,
        "commits": pr_metadata.get("commits", 0),
        "by_status": dict(by_status),
        "languages": languages,
        "primary_language": languages[0]["language"] if languages else None,
        "test_files": test_files,
        "code_files": code_files,
        "doc_files": doc_files,
        "test_lines_changed": test_lines,
        "code_lines_changed": code_lines,
        "doc_lines_changed": doc_lines,
        "test_to_code_ratio": ratio,
        "has_tests": test_files > 0,
        "docs_updated": doc_files > 0,
        "new_dependencies": new_deps,
        "dependency_files_touched": dep_files_touched,
        "blast_radius": len(modules),
        "modules_touched": sorted(modules),
        "largest_file": largest,
        "binary_or_generated_files": binary_generated,
        "touches_ci": ci_touched,
        "touches_migrations": migration_touched,
        "risk_flags": flags,
    }


# Closing keywords GitHub recognizes for linking PRs to issues.
ISSUE_CLOSE_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+(?:https://github\.com/([\w.-]+)/([\w.-]+)/issues/(\d+)|#(\d+))",
    re.IGNORECASE,
)
# Bare "#123" references (weaker signal than closing keywords).
ISSUE_REF_RE = re.compile(r"(?<![\w/&])#(\d{1,6})\b")


def extract_issue_refs(pr_body: str, pr_title: str = ""):
    """Return ({closing issue numbers}, {mentioned issue numbers}) from PR text."""
    text = f"{pr_title}\n{pr_body or ''}"
    closing, mentioned = set(), set()
    for m in ISSUE_CLOSE_RE.finditer(text):
        num = m.group(3) or m.group(4)
        if num:
            closing.add(int(num))
    for m in ISSUE_REF_RE.finditer(text):
        mentioned.add(int(m.group(1)))
    return closing, mentioned - closing


async def fetch_linked_issues(client, owner, repo, pr_metadata, headers) -> dict:
    """Resolve issues the PR claims to close/reference and check assignee status.

    Review process step 2 (after CONTRIBUTING.md): is this PR anchored to an
    issue, and is the PR author actually assigned to it?
    """
    author = pr_metadata.get("user", {}).get("login", "")
    closing, mentioned = extract_issue_refs(pr_metadata.get("body", "") or "", pr_metadata.get("title", ""))

    issues = []
    # Cap lookups; closing refs first, then plain mentions.
    for num in (sorted(closing) + sorted(mentioned))[:5]:
        try:
            resp = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/issues/{num}", headers=headers
            )
            if resp.status_code != 200:
                continue
            js = resp.json()
            if "pull_request" in js:  # the ref pointed at another PR, not an issue
                continue
            assignees = [a.get("login") for a in js.get("assignees", []) if a.get("login")]
            issues.append({
                "number": num,
                "title": js.get("title", ""),
                "state": js.get("state", ""),
                "url": js.get("html_url", ""),
                "closing": num in closing,
                "assignees": assignees,
                "author_assigned": author in assignees,
                "labels": [l.get("name") for l in js.get("labels", [])][:6],
            })
        except Exception:
            continue

    linked = [i for i in issues if i["closing"]] or issues
    return {
        "has_linked_issue": bool(issues),
        "closing_refs": sorted(closing),
        "mentioned_refs": sorted(mentioned),
        "issues": issues,
        "author_assigned_to_any": any(i["author_assigned"] for i in linked),
        "pr_author": author,
    }


def _decode_contents(js: dict, html_url: str = None) -> dict:
    content = js.get("content", "") or ""
    if js.get("encoding") == "base64":
        try:
            content = base64.b64decode(content).decode("utf-8", "replace")
        except Exception:
            content = ""
    truncated = False
    if len(content) > CONTRIBUTING_CHAR_CAP:
        content = content[:CONTRIBUTING_CHAR_CAP] + "\n... [contribution guidelines truncated]"
        truncated = True
    return {
        "found": bool(content.strip()),
        "path": js.get("path"),
        "content": content,
        "html_url": html_url or js.get("html_url"),
        "truncated": truncated,
    }


async def fetch_contributing(client, owner, repo, ref, headers) -> dict:
    """Locate and fetch the repo's contribution guidelines (CONTRIBUTING.md).

    The review is always grounded in the project's own contribution rules, so we
    look for them first: the community-profile endpoint locates the file wherever
    it lives, and a list of conventional paths acts as a fallback.
    """
    empty = {"found": False, "path": None, "content": "", "html_url": None, "truncated": False}

    # 1) Community profile — finds CONTRIBUTING regardless of location/case (public repos).
    try:
        prof = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}/community/profile", headers=headers
        )
        if prof.status_code == 200:
            contributing = (prof.json().get("files") or {}).get("contributing")
            if contributing and contributing.get("url"):
                fr = await client.get(contributing["url"], headers=headers)
                if fr.status_code == 200:
                    return _decode_contents(fr.json(), contributing.get("html_url"))
                # Org-level guidelines live in the owner's `.github` repo; the API
                # `url` 404s there, but html_url reveals the true repo + path.
                html_url = contributing.get("html_url") or ""
                m = re.match(r"https://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+)", html_url)
                if m:
                    o2, r2, ref2, path2 = m.groups()
                    fr2 = await client.get(
                        f"https://api.github.com/repos/{o2}/{r2}/contents/{path2}",
                        headers=headers, params={"ref": ref2},
                    )
                    if fr2.status_code == 200:
                        return _decode_contents(fr2.json(), html_url)
    except Exception:
        pass

    # 2) Conventional path fallback (works for private repos / non-standard setups).
    for path in CONTRIBUTING_CANDIDATES:
        try:
            params = {"ref": ref} if ref else None
            fr = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/contents/{path}",
                headers=headers, params=params,
            )
            if fr.status_code == 200:
                return _decode_contents(fr.json())
        except Exception:
            continue
    return empty


async def _get_all_files(client, files_url, headers):
    """Paginate the PR files endpoint up to MAX_FILES_FOR_SIGNALS."""
    files = []
    page = 1
    while len(files) < MAX_FILES_FOR_SIGNALS:
        resp = await client.get(files_url, headers=headers, params={"per_page": 100, "page": page})
        if resp.status_code != 200:
            if page == 1:
                raise ValueError(f"Failed to fetch PR files: {resp.text}")
            break
        batch = resp.json()
        if not batch:
            break
        files.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return files


async def fetch_pr_data(pr_url: str, github_token: str = None) -> dict:
    owner, repo, pr_number = parse_pr_url(pr_url)

    headers = {"Accept": "application/vnd.github.v3+json", "User-Agent": "pr-review-agent"}
    token = github_token or os.getenv("GITHUB_TOKEN")
    if token and token != "your_github_token_here_optional":
        headers["Authorization"] = f"Bearer {token}"

    base_url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}"
    files_url = f"{base_url}/files"

    async with httpx.AsyncClient(timeout=30.0) as client:
        pr_response = await client.get(base_url, headers=headers)
        if pr_response.status_code == 401 and not github_token:
            # Stale token from the environment — retry unauthenticated rather than
            # failing public-repo reviews. (An explicit user token still errors.)
            headers.pop("Authorization", None)
            pr_response = await client.get(base_url, headers=headers)
        if pr_response.status_code == 401:
            raise ValueError("GitHub rejected the provided token (401 Bad credentials).")
        if pr_response.status_code == 404:
            raise ValueError("PR not found. For private repos, supply a GitHub token.")
        if pr_response.status_code == 403 and "rate limit" in pr_response.text.lower():
            raise ValueError("GitHub API rate limit hit. Supply a GitHub token to raise the limit.")
        if pr_response.status_code != 200:
            raise ValueError(f"Failed to fetch PR metadata: {pr_response.text}")
        pr_metadata = pr_response.json()

        pr_files = await _get_all_files(client, files_url, headers)

        # Review process: (1) ground in the repo's contribution guidelines,
        # (2) resolve linked issues + assignee status.
        base_ref = pr_metadata.get("base", {}).get("ref", "")
        contributing = await fetch_contributing(client, owner, repo, base_ref, headers)
        issue_link = await fetch_linked_issues(client, owner, repo, pr_metadata, headers)

    truncated = len(pr_files) >= MAX_FILES_FOR_SIGNALS
    signals = compute_signals(pr_files, pr_metadata, truncated)

    # Build the diff sent to the model (bounded), preferring source over generated files.
    files_summary = []
    diff_patches = []
    for f in pr_files:
        filename = f.get("filename", "")
        files_summary.append({
            "filename": filename,
            "status": f.get("status", ""),
            "additions": f.get("additions", 0),
            "deletions": f.get("deletions", 0),
        })

    diff_candidates = [f for f in pr_files if f.get("patch")]
    diff_truncated = False
    for f in diff_candidates[:MAX_DIFF_FILES]:
        diff_patches.append(f"--- a/{f['filename']}\n+++ b/{f['filename']}\n{f['patch']}")
    if len(diff_candidates) > MAX_DIFF_FILES:
        diff_truncated = True

    diff = "\n\n".join(diff_patches)
    if len(diff) > DIFF_CHAR_CAP:
        diff = diff[:DIFF_CHAR_CAP] + "\n... [Diff truncated for length]"
        diff_truncated = True

    return {
        "title": pr_metadata.get("title", ""),
        "description": pr_metadata.get("body", "") or "",
        "author": pr_metadata.get("user", {}).get("login", ""),
        "base_branch": pr_metadata.get("base", {}).get("ref", ""),
        "head_branch": pr_metadata.get("head", {}).get("ref", ""),
        "commits": pr_metadata.get("commits", 0),
        "changed_files": pr_metadata.get("changed_files", 0),
        "additions": pr_metadata.get("additions", 0),
        "deletions": pr_metadata.get("deletions", 0),
        "files_summary": files_summary,
        "diff": diff,
        "diff_truncated": diff_truncated,
        # Per-file patches so issues can be shown with their diff hunk inline.
        "patches": {f["filename"]: f["patch"] for f in diff_candidates[:MAX_DIFF_FILES]},
        "signals": signals,
        "contributing": contributing,
        "issue_link": issue_link,
    }
