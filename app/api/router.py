from fastapi import APIRouter

from app.api.routes import (
    chat,
    document_updates,
    evaluation,
    feedback,
    health,
    knowledge_bases,
    rag,
    stats,
)

api_router = APIRouter()
api_router.include_router(health.router, prefix="/health", tags=["health"])
api_router.include_router(knowledge_bases.router, prefix="/kb", tags=["knowledge-bases"])
api_router.include_router(document_updates.router, prefix="/kb", tags=["document-updates"])
api_router.include_router(evaluation.router, prefix="/eval", tags=["evaluation"])
api_router.include_router(feedback.router, prefix="/feedback", tags=["feedback"])
api_router.include_router(rag.router, prefix="/rag", tags=["rag"])
api_router.include_router(chat.router, prefix="/chat", tags=["chat"])
api_router.include_router(stats.router, prefix="/stats", tags=["stats"])
