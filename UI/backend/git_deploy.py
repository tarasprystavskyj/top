"""
Git Deploy API Endpoint
Виконує git pull на сервері через HTTP запит з секретним ключем
"""
import os
import subprocess
import hashlib
import hmac
from datetime import datetime
from fastapi import APIRouter, HTTPException, Query, Body
from pydantic import BaseModel
from typing import Optional

router = APIRouter()

# Секретний ключ для авторизації (потім винести в .env)
DEPLOY_SECRET = os.getenv("DEPLOY_SECRET", "change_me_in_production_12345")

# Шлях до репозиторію (налаштовується через змінну середовища)
REPO_PATH = os.getenv("REPO_PATH", "/var/www/vps2.happyuser.info/top/top_1/obw_platform")


class DeployResponse(BaseModel):
    success: bool
    message: str
    output: Optional[str] = None
    error: Optional[str] = None
    timestamp: str


def verify_secret(provided_secret: str) -> bool:
    """Перевіряє секретний ключ"""
    return hmac.compare_digest(provided_secret, DEPLOY_SECRET)


@router.get("/deploy/pull")
async def deploy_pull_get(secret: str = Query(..., description="Секретний ключ для авторизації")):
    """
    Виконує git pull через GET запит
    
    Приклад: /api/deploy/pull?secret=your_secret_key
    """
    if not verify_secret(secret):
        raise HTTPException(status_code=403, detail="Invalid secret key")
    
    return await execute_git_pull()


@router.post("/deploy/pull")
async def deploy_pull_post(secret: str = Body(..., embed=True)):
    """
    Виконує git pull через POST запит
    
    Body: {"secret": "your_secret_key"}
    """
    if not verify_secret(secret):
        raise HTTPException(status_code=403, detail="Invalid secret key")
    
    return await execute_git_pull()


async def execute_git_pull() -> DeployResponse:
    """Виконує git pull і повертає результат"""
    timestamp = datetime.now().isoformat()
    
    try:
        # Перевіряємо чи існує репозиторій
        if not os.path.exists(REPO_PATH):
            return DeployResponse(
                success=False,
                message=f"Repository path not found: {REPO_PATH}",
                timestamp=timestamp
            )
        
        if not os.path.exists(os.path.join(REPO_PATH, ".git")):
            return DeployResponse(
                success=False,
                message=f"Not a git repository: {REPO_PATH}",
                timestamp=timestamp
            )
        
        # Виконуємо git pull
        result = subprocess.run(
            ["git", "pull"],
            cwd=REPO_PATH,
            capture_output=True,
            text=True,
            timeout=30
        )
        
        if result.returncode == 0:
            return DeployResponse(
                success=True,
                message="Git pull completed successfully",
                output=result.stdout,
                timestamp=timestamp
            )
        else:
            return DeployResponse(
                success=False,
                message="Git pull failed",
                output=result.stdout,
                error=result.stderr,
                timestamp=timestamp
            )
            
    except subprocess.TimeoutExpired:
        return DeployResponse(
            success=False,
            message="Git pull timeout (>30s)",
            timestamp=timestamp
        )
    except Exception as e:
        return DeployResponse(
            success=False,
            message=f"Error: {str(e)}",
            timestamp=timestamp
        )


@router.get("/deploy/status")
async def deploy_status(secret: str = Query(...)):
    """
    Перевіряє статус git репозиторію
    """
    if not verify_secret(secret):
        raise HTTPException(status_code=403, detail="Invalid secret key")
    
    timestamp = datetime.now().isoformat()
    
    try:
        # Отримуємо поточний коміт
        result = subprocess.run(
            ["git", "log", "-1", "--oneline"],
            cwd=REPO_PATH,
            capture_output=True,
            text=True,
            timeout=10
        )
        
        current_commit = result.stdout.strip() if result.returncode == 0 else "Unknown"
        
        # Перевіряємо чи є зміни
        result = subprocess.run(
            ["git", "status", "--short"],
            cwd=REPO_PATH,
            capture_output=True,
            text=True,
            timeout=10
        )
        
        has_changes = bool(result.stdout.strip())
        
        return {
            "success": True,
            "repo_path": REPO_PATH,
            "current_commit": current_commit,
            "has_local_changes": has_changes,
            "timestamp": timestamp
        }
        
    except Exception as e:
        return {
            "success": False,
            "message": f"Error: {str(e)}",
            "timestamp": timestamp
        }
