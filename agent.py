import os
import json
from typing import Any

from dotenv import load_dotenv
from huggingface_hub import AsyncInferenceClient

# Loads HF_TOKEN from .env using python-dotenv
load_dotenv()


REQUIRED_KEYS = {"summary", "issues", "suggestions", "verdict", "verdict_reason"}
VALID_VERDICTS = {"APPROVE", "REQUEST CHANGES"}
HF_TOKEN_MISSING_ERROR = "HF_TOKEN is not set in .env"
LLM_JSON_ERROR = "Failed to parse a valid JSON review from the LLM."
LLM_PROVIDER_ERROR = "LLM provider request failed."
MAX_TITLE_CHARS = 300
MAX_AUTHOR_CHARS = 120
MAX_DESCRIPTION_CHARS = 2_000
MAX_DIFF_CHARS = 12_000


def bounded_text(value: Any, max_chars: int) -> str:
    return str(value or "")[:max_chars]


def normalize_review_payload(payload: Any) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("LLM response must be a JSON object")
    missing = REQUIRED_KEYS - set(payload)
    if missing:
        raise ValueError(f"LLM response missing required keys: {', '.join(sorted(missing))}")

    verdict = str(payload.get("verdict", "")).upper().strip()
    if verdict not in VALID_VERDICTS:
        verdict = "REQUEST CHANGES"

    issues = payload.get("issues")
    if not isinstance(issues, list):
        issues = []
    normalized_issues = []
    for item in issues[:25]:
        if not isinstance(item, dict):
            continue
        severity = str(item.get("severity", "low")).lower()
        if severity not in {"high", "medium", "low"}:
            severity = "low"
        normalized_issues.append(
            {
                "severity": severity,
                "file": str(item.get("file", ""))[:240],
                "comment": str(item.get("comment", ""))[:2000],
            }
        )

    suggestions = payload.get("suggestions")
    if not isinstance(suggestions, list):
        suggestions = []

    return {
        "summary": str(payload.get("summary", ""))[:4000],
        "issues": normalized_issues,
        "suggestions": [str(item)[:1000] for item in suggestions[:20]],
        "verdict": verdict,
        "verdict_reason": str(payload.get("verdict_reason", ""))[:1000],
    }


async def review_pr(pr_data: dict) -> dict:
    hf_token = os.getenv("HF_TOKEN", "").strip()
    if not hf_token or hf_token == "your_huggingface_token_here":
        return {"error": HF_TOKEN_MISSING_ERROR}
        
    client = AsyncInferenceClient(token=hf_token)
    model = "Qwen/Qwen2.5-72B-Instruct"
    
    system_prompt = "You are a senior software engineer doing a thorough PR review."
    user_prompt = build_review_prompt(pr_data)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt}
    ]
    
    try:
        response = await client.chat_completion(
            model=model,
            messages=messages,
            max_tokens=2048,
        )
        content = response.choices[0].message.content.strip()
        
        # Strip potential markdown formatting
        if content.startswith("```json"):
            content = content[7:]
        elif content.startswith("```"):
            content = content[3:]
            
        if content.endswith("```"):
            content = content[:-3]
            
        content = content.strip()
        
        review_result = json.loads(content)
        return normalize_review_payload(review_result)
    except (json.JSONDecodeError, ValueError):
        return {"error": LLM_JSON_ERROR}
    except Exception:
        return {"error": LLM_PROVIDER_ERROR}


def build_review_prompt(pr_data: dict) -> str:
    return f"""Review the following Pull Request and return ONLY a JSON object.

Title: {bounded_text(pr_data.get('title'), MAX_TITLE_CHARS)}
Author: {bounded_text(pr_data.get('author'), MAX_AUTHOR_CHARS)}
Description: {bounded_text(pr_data.get('description'), MAX_DESCRIPTION_CHARS)}

Stats:
- Commits: {pr_data.get('commits')}
- Changed files: {pr_data.get('changed_files')}
- Additions: {pr_data.get('additions')}
- Deletions: {pr_data.get('deletions')}

Diff:
{bounded_text(pr_data.get('diff'), MAX_DIFF_CHARS)}

You must return ONLY a JSON object with the following keys:
- summary (string): what this PR does
- issues (list of objects with keys: severity (string: "high/medium/low"), file (string), comment (string))
- suggestions (list of strings)
- verdict (string): "APPROVE" or "REQUEST CHANGES"
- verdict_reason (string): one sentence why

Do not include any markdown formatting like ```json or any other text before or after the JSON.
"""
