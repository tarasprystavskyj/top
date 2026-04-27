"""
Git Deploy API endpoints for the admin UI.

Routes mounted by api_main.py under prefix /api:
  GET  /api/deploy/status?secret=...
  POST /api/deploy/pull        JSON: {"secret":"..."}
  GET  /api/deploy/pull?secret=...  optional manual URL trigger
"""
import hmac
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel

router = APIRouter()

BACKEND_DIR = Path(__file__).resolve().parent
DEFAULT_REPO_PATH = str(BACKEND_DIR.parent.parent)  # /var/www/.../top_1

DEPLOY_SECRET = os.getenv("DEPLOY_SECRET", "").strip()
REPO_PATH = os.getenv("REPO_PATH", DEFAULT_REPO_PATH).strip()
GIT_TIMEOUT_SEC = int(os.getenv("GIT_TIMEOUT_SEC", "60"))
GIT_REMOTE = os.getenv("GIT_REMOTE", "origin").strip()
GIT_BRANCH = os.getenv("GIT_BRANCH", "main").strip()


class DeployResponse(BaseModel):
    success: bool
    message: str
    output: Optional[str] = None
    error: Optional[str] = None
    timestamp: str
    repo_path: Optional[str] = None
    current_commit: Optional[str] = None


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _verify_secret(provided_secret: str) -> None:
    if not DEPLOY_SECRET:
        raise HTTPException(status_code=500, detail="DEPLOY_SECRET is not configured on backend")
    if not hmac.compare_digest(str(provided_secret), DEPLOY_SECRET):
        raise HTTPException(status_code=403, detail="Invalid secret key")


def _repo_dir() -> Path:
    repo = Path(REPO_PATH).expanduser().resolve()
    if not repo.exists():
        raise HTTPException(status_code=500, detail=f"Repository path not found: {repo}")
    if not (repo / ".git").exists():
        raise HTTPException(status_code=500, detail=f"Not a git repository: {repo}")
    return repo


def _run_git(args: List[str], timeout: int = GIT_TIMEOUT_SEC) -> subprocess.CompletedProcess:
    repo = _repo_dir()
    env = os.environ.copy()
    # Never hang waiting for SSH passphrase/password in an HTTP request.
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def _run_git_text(args: List[str], timeout: int = 15) -> str:
    result = _run_git(args, timeout=timeout)
    if result.returncode != 0:
        return (result.stderr or result.stdout or "").strip()
    return result.stdout.strip()


def _read_secret_from_body(payload: Any) -> str:
    if isinstance(payload, dict):
        return str(payload.get("secret", ""))
    return str(payload or "")


@router.get("/deploy/status")
def deploy_status(secret: str = Query(...)) -> Dict[str, Any]:
    """Return repository status for the deploy UI."""
    _verify_secret(secret)
    repo = _repo_dir()

    current_commit = _run_git_text(["log", "-1", "--oneline"], timeout=15)
    branch = _run_git_text(["rev-parse", "--abbrev-ref", "HEAD"], timeout=15)
    status_short = _run_git_text(["status", "--short"], timeout=15)

    # Best-effort remote check. If SSH agent is not ready, show the error instead of hiding it.
    fetch = _run_git(["fetch", GIT_REMOTE, GIT_BRANCH], timeout=GIT_TIMEOUT_SEC)
    upstream = _run_git_text(["rev-parse", f"{GIT_REMOTE}/{GIT_BRANCH}"], timeout=15)
    local = _run_git_text(["rev-parse", "HEAD"], timeout=15)

    return {
        "success": True,
        "repo_path": str(repo),
        "branch": branch,
        "target": f"{GIT_REMOTE}/{GIT_BRANCH}",
        "current_commit": current_commit,
        "local_head": local,
        "remote_head": upstream,
        "up_to_date": bool(local and upstream and local == upstream),
        "has_local_changes": bool(status_short),
        "local_changes": status_short,
        "fetch_returncode": fetch.returncode,
        "fetch_stdout": fetch.stdout.strip(),
        "fetch_stderr": fetch.stderr.strip(),
        "timestamp": _now(),
    }


@router.get("/deploy/pull")
def deploy_pull_get(secret: str = Query(...)) -> DeployResponse:
    """Manual URL trigger: /api/deploy/pull?secret=..."""
    _verify_secret(secret)
    return _execute_git_pull()


@router.post("/deploy/pull")
def deploy_pull_post(payload: Any = Body(...)) -> DeployResponse:
    """Button trigger from frontend. Expected JSON: {"secret":"..."}."""
    _verify_secret(_read_secret_from_body(payload))
    return _execute_git_pull()


def _execute_git_pull() -> DeployResponse:
    timestamp = _now()
    repo = _repo_dir()

    try:
        before = _run_git_text(["log", "-1", "--oneline"], timeout=15)
        # --autostash prevents local manual UI edits from immediately breaking pull.
        # BatchMode=yes prevents the HTTP request from hanging on SSH passphrase prompt.
        result = _run_git(["pull", "--rebase", "--autostash", GIT_REMOTE, GIT_BRANCH], timeout=GIT_TIMEOUT_SEC)
        after = _run_git_text(["log", "-1", "--oneline"], timeout=15)

        output = "\n".join(part for part in [f"before: {before}", result.stdout.strip(), f"after: {after}"] if part)

        if result.returncode == 0:
            return DeployResponse(
                success=True,
                message="Git pull completed successfully",
                output=output,
                error=result.stderr.strip() or None,
                timestamp=timestamp,
                repo_path=str(repo),
                current_commit=after,
            )

        return DeployResponse(
            success=False,
            message="Git pull failed",
            output=output,
            error=result.stderr.strip() or result.stdout.strip(),
            timestamp=timestamp,
            repo_path=str(repo),
            current_commit=after,
        )

    except subprocess.TimeoutExpired:
        return DeployResponse(success=False, message=f"Git pull timeout (>{GIT_TIMEOUT_SEC}s)", timestamp=timestamp, repo_path=str(repo))
    except Exception as exc:
        return DeployResponse(success=False, message=f"Error: {exc}", timestamp=timestamp, repo_path=str(repo))
