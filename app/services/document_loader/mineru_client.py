import tempfile
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, BinaryIO, Callable

from langchain_core.documents import Document

from app.core.config import Settings, get_settings
from app.services.document_loader.exceptions import EmptyDocumentError, ExternalParserError

LoaderFactory = Callable[..., Any]
logger = logging.getLogger(__name__)


class MinerULoaderClient(ABC):
    """MinerU SDK 集成基类，统一处理外部解析调用与结果转换。

    子类只需要声明是否按页拆分，并实现 `_convert_sdk_documents`，
    将 MinerU 返回结果转换为项目内部使用的 LangChain `Document` 列表。
    """

    split_pages: bool

    def __init__(
        self,
        settings: Settings | None = None,
        loader_factory: LoaderFactory | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.loader_factory = loader_factory or self._default_loader_factory

    def parse(self, file: BinaryIO, file_name: str, file_type: str) -> list[Document]:
        """执行 MinerU 解析并转换为内部文档对象。

        Args:
            file: 上传文件的二进制流。
            file_name: 原始文件名，用于临时文件后缀和 metadata。
            file_type: 当前项目内部识别出的文件类型。

        Returns:
            转换后的 LangChain `Document` 列表。
        """
        # 第一步：调用 MinerU SDK，拿到外部解析结果。
        sdk_docs = self._load_with_mineru(file, file_name)
        # 第二步：交给具体子类补齐结构化 metadata 并转换格式。
        docs = self._convert_sdk_documents(sdk_docs, file_name, file_type)
        if not docs:
            raise EmptyDocumentError("empty MinerU document")
        return docs

    def _load_with_mineru(self, file: BinaryIO, file_name: str) -> list[Document]:
        """按项目配置调用 MinerU SDK，并返回原始解析结果。"""
        # 复杂文档解析能力依赖 MinerU，这里显式失败，避免静默退回低质量本地解析。
        if not self.settings.mineru_enabled:
            raise ExternalParserError("MinerU is disabled")
        if self.settings.mineru_mode != "sdk":
            raise ExternalParserError(f"unsupported MinerU mode: {self.settings.mineru_mode}")

        # 先把项目配置映射为 SDK 识别的模式名，避免调用点分散处理配置差异。
        sdk_mode = self._sdk_mode()
        # precision 模式走远端能力，没有 API key 无法继续，尽早在本地报配置错误。
        if sdk_mode == "precision" and not self.settings.mineru_api_key:
            raise ExternalParserError("MinerU API key is not configured")

        # SDK 只接受文件路径，这里把上传流落到临时文件，再在 finally 中清理。
        temp_path = self._write_temp_file(file, file_name)
        try:
            # 统一组装 SDK loader 参数，保证不同解析器共用同一套调用约束。
            loader = self.loader_factory(
                source=str(temp_path),
                mode=sdk_mode,
                token=self.settings.mineru_api_key or None,
                language="ch",
                timeout=self.settings.mineru_timeout_seconds,
                split_pages=self.split_pages,
            )
            logger.info("MinerU解析开始了...")
            docs = loader.load()
            logger.info("MinerU解析结束了...")
            return docs
        except Exception as exc:
            # 外部 SDK 可能抛出多种实现细节异常，这里收敛为项目统一错误类型。
            raise ExternalParserError(f"MinerU SDK request failed: {exc}") from exc
        finally:
            # 无论解析成功还是失败，都要清理临时文件，避免磁盘残留。
            temp_path.unlink(missing_ok=True)

    def _default_loader_factory(self, **kwargs: Any) -> Any:
        """创建官方 MinerU LangChain loader。"""
        from langchain_mineru import MinerULoader

        return MinerULoader(**kwargs)

    def _sdk_mode(self) -> str:
        """将项目配置中的 API 类型映射为 MinerU SDK 模式。"""
        api_type = self.settings.mineru_api_type.lower()
        # 项目里 accurate / precision 统一视为 SDK 的 precision 模式，避免上层感知供应商细节。
        if api_type in {"accurate", "precision"}:
            return "precision"
        if api_type == "flash":
            return "flash"
        raise ExternalParserError(f"unsupported MinerU API type: {self.settings.mineru_api_type}")

    def _write_temp_file(self, file: BinaryIO, file_name: str) -> Path:
        """把上传流写入临时文件，适配 SDK 仅支持路径输入的接口。"""
        suffix = Path(file_name).suffix
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp_file:
            temp_file.write(file.read())
            return Path(temp_file.name)

    def _first_text(self, value: Any) -> str | None:
        """提取非空字符串，过滤掉空值和非文本字段，保证内容与 metadata 可预测。"""
        if isinstance(value, str):
            text = value.strip()
            return text or None
        return None

    @abstractmethod
    def _convert_sdk_documents(
        self,
        sdk_docs: list[Document],
        file_name: str,
        file_type: str,
    ) -> list[Document]:
        """由子类实现具体转换逻辑，补齐文件类型相关的 metadata。"""
        ...
