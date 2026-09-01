from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass

from minio.commonconfig import CopySource
from sqlalchemy import select
from sqlalchemy.sql import and_

from app.core.clients import close_clients, get_minio, init_clients
from app.core.config import get_settings
from app.core.database import AsyncSessionLocal
from app.integrations.minio import build_kb_folder_name
from app.models import KbDocument, KnowledgeBase


@dataclass(frozen=True)
class MigrationItem:
    """单个文档对象的旧路径和目标路径。"""

    document: KbDocument
    old_path: str
    new_path: str


def build_target_path(old_path: str, kb_id: int, kb_name: str) -> str | None:
    """将旧版 `kb/{id}/...` 路径转换为带知识库名称的路径。"""
    prefix = f"kb/{kb_id}/"
    if not old_path.startswith(prefix):
        return None
    return f"kb/{build_kb_folder_name(kb_id, kb_name)}/{old_path[len(prefix):]}"


async def migrate(*, apply: bool) -> None:
    """迁移历史对象；默认只输出计划，不修改 MinIO 或数据库。"""
    settings = get_settings()
    await init_clients(settings)
    copied_paths: list[str] = []
    try:
        minio = get_minio()
        existing_paths = {
            obj.object_name
            for obj in minio.list_objects(
                settings.minio_bucket,
                prefix="kb/",
                recursive=True,
            )
        }
        skipped_missing = 0
        async with AsyncSessionLocal() as session:
            rows = (
                await session.execute(
                    select(KbDocument, KnowledgeBase)
                    .join(KnowledgeBase, KnowledgeBase.id == KbDocument.kb_id)
                    .where(
                        and_(
                            KbDocument.is_deleted.is_(False),
                            KnowledgeBase.is_deleted.is_(False),
                        )
                    )
                )
            ).all()
            items: list[MigrationItem] = []
            for document, knowledge_base in rows:
                if document.minio_path not in existing_paths:
                    skipped_missing += 1
                    continue
                new_path = build_target_path(
                    document.minio_path,
                    document.kb_id,
                    knowledge_base.name,
                )
                if new_path is not None and new_path != document.minio_path:
                    items.append(
                        MigrationItem(
                            document=document,
                            old_path=document.minio_path,
                            new_path=new_path,
                        )
                    )

            if not items:
                print("没有需要迁移的历史对象。")
                return

            print(f"发现 {len(items)} 个对象需要迁移：")
            for item in items:
                print(f"  {item.old_path} -> {item.new_path}")
            if skipped_missing:
                print(f"跳过 {skipped_missing} 条缺失对象引用。")
            if not apply:
                print("当前为预览模式，未修改 MinIO 或数据库。执行时请添加 --apply。")
                return
            try:
                for item in items:
                    minio.copy_object(
                        bucket_name=settings.minio_bucket,
                        object_name=item.new_path,
                        source=CopySource(settings.minio_bucket, item.old_path),
                    )
                    copied_paths.append(item.new_path)
                    item.document.minio_path = item.new_path
                await session.commit()
            except Exception:
                await session.rollback()
                for object_path in copied_paths:
                    minio.remove_object(
                        bucket_name=settings.minio_bucket,
                        object_name=object_path,
                    )
                raise

            for item in items:
                minio.remove_object(
                    bucket_name=settings.minio_bucket,
                    object_name=item.old_path,
                )
            print(f"已完成 {len(items)} 个对象迁移。")
    finally:
        await close_clients()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="迁移 MinIO 知识库对象目录")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="实际复制对象、更新数据库路径并删除旧对象；默认只预览",
    )
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(migrate(apply=parse_args().apply))
