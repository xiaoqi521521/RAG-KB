from __future__ import annotations

import asyncio
import logging
from io import BytesIO
from uuid import uuid4

from fastapi import UploadFile
from minio import Minio

logger = logging.getLogger(__name__)


def build_kb_folder_name(kb_id: int, kb_name: str) -> str:
    """构造 MinIO 知识库目录名，避免名称破坏对象路径层级。"""
    safe_name = "".join(
        character if character not in "/\\" and ord(character) >= 32 else "_"
        for character in kb_name.strip()
    ).strip(" .")
    return f"{kb_id}-{safe_name or '未命名知识库'}"


class MinioStorageService:
    """MinIO 原始文件存储服务，负责上传、下载和删除对象。"""

    def __init__(self, *, client: Minio, bucket: str) -> None:
        self.client = client
        self.bucket = bucket

    async def upload(self, kb_id: int, file: UploadFile, *, kb_name: str) -> str:
        """按知识库标识上传文件，返回对象路径。"""
        file_name = file.filename or "uploaded-file"
        content = await file.read()
        folder_name = build_kb_folder_name(kb_id, kb_name)
        object_key = f"kb/{folder_name}/{uuid4().hex[:8]}-{file_name}"
        content_type = file.content_type or "application/octet-stream"

        def _upload() -> None:
            self._ensure_bucket_exists()
            self.client.put_object(
                bucket_name=self.bucket,
                object_name=object_key,
                data=BytesIO(content),
                length=len(content),
                content_type=content_type,
            )

        await asyncio.to_thread(_upload)
        logger.info("[MinIO] 上传成功")
        return object_key

    async def download(self, object_key: str) -> bytes:
        """从 MinIO 下载文件内容。"""

        def _download() -> bytes:
            response = self.client.get_object(
                bucket_name=self.bucket,
                object_name=object_key,
            )
            try:
                return response.read()
            finally:
                response.close()
                response.release_conn()

        content = await asyncio.to_thread(_download)
        logger.info("[MinIO] 下载成功")
        return content

    async def delete(self, object_key: str) -> None:
        """删除 MinIO 对象；删除失败只记录告警，不阻断主流程。"""

        def _delete() -> None:
            self.client.remove_object(
                bucket_name=self.bucket,
                object_name=object_key,
            )

        try:
            await asyncio.to_thread(_delete)
            logger.info("[MinIO] 删除成功")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[MinIO] 删除失败：error_type=%s", type(exc).__name__)

    def _ensure_bucket_exists(self) -> None:
        exists = self.client.bucket_exists(self.bucket)
        if not exists:
            self.client.make_bucket(self.bucket)
            logger.info("[MinIO] Bucket 已创建：%s", self.bucket)
