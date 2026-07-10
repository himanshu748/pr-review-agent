# PR Review Agent Notes

## Project Shape
- Small FastAPI app with backend modules in the repo root and a static browser UI in `static/`.
- `github.py` fetches PR metadata and diffs from GitHub.
- `agent.py` calls the OpenAI Responses API and normalizes the review JSON.
- `main.py` wires HTTP routes and static assets.

## Common Commands
- Tests: `python -m pytest`
- Syntax check: `python -m compileall agent.py github.py main.py tests`
- Dev server: `uvicorn main:app --reload --port 8000`

## Conventions
- Keep static paths relative to the repository root; do not hardcode container/workspace paths.
- Do not commit `__pycache__/`, `.env`, or generated local artifacts.
- Never echo GitHub or OpenAI token values in API errors, logs, or UI output.
- Keep upstream LLM/provider failures generic at the API boundary; tests should prove raw exception text is not returned.
- Keep PR review inputs bounded because full diffs and LLM prompts can grow quickly.
