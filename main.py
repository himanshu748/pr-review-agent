from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agent import HF_TOKEN_MISSING_ERROR, review_pr
from github import fetch_pr_data

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(
    title="AI PR Review Agent",
    description="Review GitHub pull requests with bounded diff fetching and Hugging Face-powered analysis.",
    version="0.2.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:8000", "http://localhost:8000"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class PRRequest(BaseModel):
    pr_url: str = Field(..., min_length=1, max_length=300)
    github_token: Optional[str] = Field(default=None, max_length=200)


@app.get("/")
async def read_root():
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/review")
async def create_review(request: PRRequest):
    try:
        pr_data = await fetch_pr_data(request.pr_url, request.github_token)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        raise HTTPException(status_code=502, detail="GitHub API request failed")
        
    review_result = await review_pr(pr_data)
    
    if "error" in review_result:
        status_code = 503 if review_result["error"] == HF_TOKEN_MISSING_ERROR else 502
        raise HTTPException(status_code=status_code, detail=review_result["error"])
        
    return {
        "metadata": {
            "title": pr_data.get("title"),
            "author": pr_data.get("author"),
            "commits": pr_data.get("commits"),
            "changed_files": pr_data.get("changed_files"),
            "additions": pr_data.get("additions"),
            "deletions": pr_data.get("deletions")
        },
        "review": review_result
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
