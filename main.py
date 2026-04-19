# Test PR URL for demo: https://github.com/fastapi/fastapi/pull/1

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional

from github import fetch_pr_data
from agent import review_pr

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="/workspace/pr-review-agent/static"), name="static")

class PRRequest(BaseModel):
    pr_url: str
    github_token: Optional[str] = None

@app.get("/")
async def read_root():
    return FileResponse("/workspace/pr-review-agent/static/index.html")

@app.post("/review")
async def create_review(request: PRRequest):
    try:
        pr_data = await fetch_pr_data(request.pr_url, request.github_token)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"GitHub API Error: {str(e)}")
        
    review_result = await review_pr(pr_data)
    
    if "error" in review_result:
        raise HTTPException(status_code=500, detail=review_result["error"])
        
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
