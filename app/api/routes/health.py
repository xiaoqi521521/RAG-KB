import asyncio
import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.core.clients import get_minio, get_redis
from app.core.database import engine

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("")
async def health_check() -> dict[str, str]:
    return {"status": "UP"}


@router.get("/ready")
async def readiness_check() -> JSONResponse:
    """检查应用依赖是否可用，供反向代理和容器编排探活。"""
    checks: dict[str, bool] = {
        "database": await _check_database(),
        "redis": await _check_redis(),
        "minio": await _check_minio(),
    }
    ready = all(checks.values())
    return JSONResponse(
        status_code=200 if ready else 503,
        content={"status": "READY" if ready else "NOT_READY", "checks": checks},
    )


async def _check_database() -> bool:
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("健康检查失败：dependency=database error_type=%s", type(exc).__name__)
        return False


async def _check_redis() -> bool:
    try:
        return bool(await get_redis().ping())
    except Exception as exc:  # noqa: BLE001
        logger.warning("健康检查失败：dependency=redis error_type=%s", type(exc).__name__)
        return False


async def _check_minio() -> bool:
    def _probe() -> bool:
        # Bucket 由首次上传惰性创建；这里只确认 MinIO 服务可访问。
        get_minio().list_buckets()
        return True

    try:
        return await asyncio.to_thread(_probe)
    except Exception as exc:  # noqa: BLE001
        logger.warning("健康检查失败：dependency=minio error_type=%s", type(exc).__name__)
        return False
