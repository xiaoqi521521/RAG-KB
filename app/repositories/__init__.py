"""Repository package."""
from app.repositories.chat import ChatRepository
from app.repositories.chunks import ChunkRepository
from app.repositories.documents import DocumentRepository
from app.repositories.index_tasks import IndexTaskRepository
from app.repositories.knowledge_bases import KnowledgeBaseRepository
from app.repositories.permissions import KbPermissionRepository

__all__ = [
    "ChatRepository",
    "ChunkRepository",
    "DocumentRepository",
    "IndexTaskRepository",
    "KnowledgeBaseRepository",
    "KbPermissionRepository",
]
