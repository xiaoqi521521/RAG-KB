from fastapi import APIRouter

from app.api.routes import document_updates, health, knowledge_bases

api_router = APIRouter()
api_router.include_router(health.router, prefix="/health", tags=["health"])
api_router.include_router(knowledge_bases.router, prefix="/kb", tags=["knowledge-bases"])
api_router.include_router(document_updates.router, prefix="/kb", tags=["document-updates"])
