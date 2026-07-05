from fastapi import APIRouter

from app.api.routes import health, knowledge_bases

api_router = APIRouter()
api_router.include_router(health.router, prefix="/health", tags=["health"])
api_router.include_router(knowledge_bases.router, prefix="/kb", tags=["knowledge-bases"])
