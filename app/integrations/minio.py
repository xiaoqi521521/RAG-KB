from typing import Protocol


class MinioStorageService(Protocol):
    """当前阶段的最小存储依赖，只暴露索引管道需要的下载能力。"""

    async def download(self, object_key: str) -> bytes:
        ...
