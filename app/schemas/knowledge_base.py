from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class KnowledgeBaseCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)
    department_id: str = Field(min_length=1, max_length=50)
    is_public: bool = False


class KnowledgeBaseItem(BaseModel):
    id: int
    name: str
    description: str | None = None
    department_id: str
    is_public: bool
    created_by: int
    created_at: datetime | None = None
    permission: str


class DocumentUploadResponse(BaseModel):
    doc_id: int
    file_name: str
    status: str
    message: str

    @classmethod
    def submitted(cls, doc_id: int, file_name: str) -> "DocumentUploadResponse":
        return cls(
            doc_id=doc_id,
            file_name=file_name,
            status="PENDING",
            message="文档已上传，正在后台索引，请通过 status 接口查询进度",
        )


class DocumentReindexSubmitResponse(BaseModel):
    """文档替换或强制重建提交后的响应模型。

    Args:
        doc_id: 已提交重建任务的文档 ID。
        file_name: 当前已发布文档文件名。替换任务完成前仍显示旧文件名。
        status: 当前已发布文档状态。已完成文档重建期间通常保持 DONE。
        task_id: 新创建的 REINDEX 任务 ID。
        message: 面向前端展示的提交结果说明。
    """

    doc_id: int
    file_name: str
    status: str
    task_id: int
    message: str


class DocumentItem(BaseModel):
    """文档列表项响应模型。

    Args:
        id: 文档 ID。
        kb_id: 文档所属知识库 ID。
        file_name: 原始文件名。
        file_type: 文档类型。
        file_size: 文件大小，单位为字节。
        status: 当前索引状态。
        error_msg: 索引失败时的错误信息。
        chunk_count: 当前文档已生成的 chunk 数量。
        token_count: 当前文档已统计的 token 数量。
        version: 文档索引版本号。
        uploaded_by: 上传用户 ID。
        uploaded_at: 上传时间。
        indexed_at: 最近一次索引完成时间。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    kb_id: int
    file_name: str
    file_type: str
    file_size: int
    status: str
    error_msg: str | None = None
    chunk_count: int | None = None
    token_count: int | None = None
    version: int
    uploaded_by: int
    uploaded_at: datetime | None = None
    indexed_at: datetime | None = None


class IndexStatusResponse(BaseModel):
    doc_id: int
    file_name: str
    status: str
    error_msg: str | None = None
    chunk_count: int | None = None
    token_count: int | None = None
    indexed_at: datetime | None = None
    retry_count: int = 0
