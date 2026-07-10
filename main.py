# Test PR URL for demo: https://github.com/fastapi/fastapi/pull/1

import os
import time

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional

from github import fetch_pr_data
from agent import review_pr

# Resolve paths relative to this file so it runs locally, in Docker, and on Spaces.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

app = FastAPI(title="PR Review Agent", version="2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class PRRequest(BaseModel):
    pr_url: str
    github_token: Optional[str] = None
    model: Optional[str] = None  # must be in agent.ALLOWED_MODELS, else default


@app.get("/")
async def read_root():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/healthz")
async def healthz():
    token = os.getenv("OPENAI_API_KEY")
    configured = bool(token) and token != "your_openai_api_key_here"
    return {"status": "ok", "openai_api_key_configured": configured}


@app.get("/models")
async def models():
    from agent import ALLOWED_MODELS, DEFAULT_MODEL
    configured = os.getenv("OPENAI_MODEL", DEFAULT_MODEL)
    default = configured if configured in ALLOWED_MODELS else DEFAULT_MODEL
    return {"models": ALLOWED_MODELS, "default": default}


@app.post("/review")
async def create_review(request: PRRequest):
    started = time.monotonic()
    try:
        pr_data = await fetch_pr_data(request.pr_url, request.github_token)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"GitHub API Error: {str(e)}")

    review_result = await review_pr(pr_data, model=request.model)

    if "error" in review_result:
        raise HTTPException(status_code=500, detail=review_result["error"])

    review_result["duration_ms"] = int((time.monotonic() - started) * 1000)
    return {
        "metadata": {
            "title": pr_data.get("title"),
            "author": pr_data.get("author"),
            "base_branch": pr_data.get("base_branch"),
            "head_branch": pr_data.get("head_branch"),
            "commits": pr_data.get("commits"),
            "changed_files": pr_data.get("changed_files"),
            "additions": pr_data.get("additions"),
            "deletions": pr_data.get("deletions"),
        },
        "signals": pr_data.get("signals"),
        "review": review_result,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
