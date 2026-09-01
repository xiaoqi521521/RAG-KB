from __future__ import annotations

import re


class CitationParser:
    """解析回答中的引用标记，并保持首次出现顺序。"""

    _citation_pattern = re.compile(r"\[\s*参考\s*(\d+)\s*\]")

    def extract_reference_numbers(self, answer: str) -> list[str]:
        """提取规范化后的数字文本，避免超长编号触发整数转换异常。"""
        numbers: list[str] = []
        seen: set[str] = set()
        for match in self._citation_pattern.finditer(answer):
            number = match.group(1).lstrip("0") or "0"
            if number in seen:
                continue
            seen.add(number)
            numbers.append(number)
        return numbers

    def extract_indices(self, answer: str) -> list[int]:
        """提取 1-based 参考编号，按首次出现顺序去重。

        Args:
            answer: 模型生成且保留引用标记的回答。

        Returns:
            回答中出现的参考编号列表。
        """
        indices: list[int] = []
        for number in self.extract_reference_numbers(answer):
            try:
                indices.append(int(number))
            except ValueError:
                # 超长数字仍是引用标记，但无法安全转换为 Python int。
                continue
        return indices

    def rewrite_reference_numbers(
        self,
        answer: str,
        reference_mapping: dict[str, int],
    ) -> str:
        """按给定映射重写回答中的有效引用编号。"""

        def replace(match: re.Match[str]) -> str:
            number = match.group(1).lstrip("0") or "0"
            normalized = reference_mapping.get(number)
            if normalized is None:
                return match.group(0)
            return f"[参考{normalized}]"

        return self._citation_pattern.sub(replace, answer)
