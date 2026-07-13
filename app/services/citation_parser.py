from __future__ import annotations

import re


class CitationParser:
    """解析回答中的引用标记，并保持首次出现顺序。"""

    _citation_pattern = re.compile(r"\[\s*参考\s*(\d+)\s*\]")

    def extract_indices(self, answer: str) -> list[int]:
        """提取 1-based 参考编号，按首次出现顺序去重。

        Args:
            answer: 模型生成且保留引用标记的回答。

        Returns:
            回答中出现的参考编号列表。
        """
        indices: list[int] = []
        seen: set[int] = set()
        for match in self._citation_pattern.finditer(answer):
            index = int(match.group(1))
            if index in seen:
                continue
            seen.add(index)
            indices.append(index)
        return indices
