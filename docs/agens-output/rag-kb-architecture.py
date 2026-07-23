from __future__ import annotations

from html import escape
from pathlib import Path


OUT = Path(__file__).with_suffix(".svg")


def text(lines: list[str], x: float, y: float, value: str, *, size: int = 14,
         fill: str = "#111827", weight: int = 400, anchor: str = "middle",
         role: str = "decoration") -> None:
    lines.append(
        f'<text x="{x}" y="{y}" text-anchor="{anchor}" font-size="{size}" '
        f'font-weight="{weight}" fill="{fill}" data-graph-role="{role}">{escape(value)}</text>'
    )


def node(lines: list[str], node_id: str, x: float, y: float, width: float,
         height: float, title: str, subtitle: str, accent: str,
         badge: str) -> None:
    cx = x + width / 2
    lines.append(
        f'<g id="node-{node_id}" data-graph-role="node" data-node-id="{node_id}" '
        f'data-graph-bounds="{x} {y} {x + width} {y + height}">'
    )
    lines.append(
        f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="8" '
        f'fill="#ffffff" stroke="#d1d5db" stroke-width="1.5" data-graph-role="decoration"/>'
    )
    lines.append(
        f'<rect x="{x + 12}" y="{y + 12}" width="34" height="34" rx="7" '
        f'fill="{accent}" data-graph-role="decoration"/>'
    )
    text(lines, x + 29, y + 34, badge, size=10, fill="#ffffff", weight=700)
    text(lines, cx + 17, y + 29, title, size=14, weight=600)
    text(lines, cx + 17, y + 50, subtitle, size=11, fill="#6b7280")
    lines.append("</g>")


def container(lines: list[str], container_id: str, x: float, y: float,
              width: float, height: float, title: str, subtitle: str,
              accent: str = "#2563eb") -> None:
    lines.append(
        f'<g id="container-{container_id}" data-graph-role="container" '
        f'data-graph-bounds="{x} {y} {x + width} {y + height}">'
    )
    lines.append(
        f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="10" '
        f'fill="{accent}" fill-opacity="0.025" stroke="#dbe5f1" stroke-width="1.2" '
        f'stroke-dasharray="6 5" data-graph-role="decoration"/>'
    )
    text(lines, x + 16, y + 23, title, size=13, fill=accent, weight=700, anchor="start")
    text(lines, x + 16, y + 42, subtitle, size=11, fill="#94a3b8", anchor="start")
    lines.append("</g>")


def edge(lines: list[str], edge_id: str, source: str, target: str,
         points: list[tuple[float, float]], color: str, marker: str,
         dash: str = "", width: float = 2.0) -> None:
    path = " ".join(
        (f"M {x},{y}" if index == 0 else f"L {x},{y}")
        for index, (x, y) in enumerate(points)
    )
    dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
    lines.append(
        f'<path id="edge-{edge_id}" data-graph-role="edge" data-edge-id="{edge_id}" '
        f'data-source="{source}" data-target="{target}" d="{path}" fill="none" '
        f'stroke="{color}" stroke-width="{width}" stroke-linejoin="round" '
        f'marker-end="url(#{marker})"{dash_attr}/>'
    )


def edge_label(lines: list[str], label_id: str, edge_id: str, x: float, y: float,
               width: float, value: str, color: str = "#6b7280") -> None:
    height = 22
    lines.append(
        f'<g id="label-{label_id}" data-graph-role="label" data-owner="{edge_id}" '
        f'data-graph-bounds="{x - width / 2} {y - height / 2} {x + width / 2} {y + height / 2}">'
    )
    lines.append(
        f'<rect x="{x - width / 2}" y="{y - height / 2}" width="{width}" height="{height}" '
        f'rx="5" fill="#ffffff" fill-opacity="0.96" data-graph-role="decoration"/>'
    )
    text(lines, x, y + 4, value, size=11, fill=color)
    lines.append("</g>")


def legend_item(lines: list[str], x: float, y: float, color: str, marker: str,
                value: str, dash: str = "") -> None:
    dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
    lines.append(
        f'<line x1="{x}" y1="{y}" x2="{x + 30}" y2="{y}" stroke="{color}" '
        f'stroke-width="2"{dash_attr} marker-end="url(#{marker})" data-graph-role="decoration"/>'
    )
    text(lines, x + 40, y + 4, value, size=11, fill="#6b7280", anchor="start")


def build_svg() -> str:
    lines: list[str] = []
    lines.append(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1500 1120" width="1500" height="1120" '
        'data-generator="fireworks-tech-graph" data-quality-profile="standard" '
        'data-max-bends-per-edge="6" data-min-node-gap="36" data-min-container-gutter="20" '
        'data-min-label-clearance="4" data-min-segment-length="16">'
    )
    lines.append("  <style>")
    lines.append(
        "    text { font-family: 'Helvetica Neue', Helvetica, Arial, 'PingFang SC', "
        "'Microsoft YaHei', 'Microsoft JhengHei', 'SimHei', sans-serif; }"
    )
    lines.append("  </style>")
    lines.append("  <defs>")
    for marker_id, color in (
        ("arrow-blue", "#2563eb"),
        ("arrow-green", "#16a34a"),
        ("arrow-purple", "#9333ea"),
        ("arrow-gray", "#6b7280"),
        ("arrow-red", "#dc2626"),
        ("arrow-orange", "#ea580c"),
    ):
        lines.append(
            f'<marker id="{marker_id}" markerWidth="10" markerHeight="7" refX="9" refY="3.5" orient="auto">'
            f'<polygon points="0 0, 10 3.5, 0 7" fill="{color}"/></marker>'
        )
    lines.append("  </defs>")
    lines.append('<rect width="1500" height="1120" fill="#ffffff" data-graph-role="background"/>')
    text(lines, 750, 42, "RAG-KB 企业级知识库架构", size=28, weight=600)
    text(lines, 750, 68, "FastAPI + LangChain · 双管道索引与查询 · 知识库级权限隔离", size=13, fill="#6b7280")

    container(lines, "clients", 60, 94, 1380, 128, "CLIENTS", "React 文档管理、对话界面与运维/评估调用")
    node(lines, "react", 112, 144, 290, 56, "React 前端", "上传 · 检索 · 引用 · SSE", "#2563eb", "UI")
    node(lines, "ops-client", 522, 144, 248, 56, "运维 / 评估调用", "管理 API · 评估 API", "#ea580c", "OPS")
    node(lines, "api-client", 890, 144, 330, 56, "HTTP / SSE 客户端", "Bearer JWT · /api/v1", "#64748b", "API")

    container(lines, "api", 60, 222, 1380, 148, "API & SECURITY", "FastAPI 应用入口：认证、实时身份加载、路由与统一观测")
    node(lines, "fastapi", 112, 280, 280, 58, "FastAPI 应用", "Uvicorn · lifespan · exception handlers", "#009688", "API")
    node(lines, "identity", 476, 280, 280, 58, "JWT + Identity", "实时用户、部门、角色、启用状态", "#dc2626", "AUTH")
    node(lines, "routes", 840, 280, 520, 58, "API Routes", "/auth  /kb  /chat  /rag  /eval  /stats", "#2563eb", "ROUTE")

    container(lines, "index", 60, 390, 680, 344, "OFFLINE INDEX PIPELINE", "上传文档 → 解析分块 → 向量化 → 发布当前版本")
    node(lines, "index-task", 88, 458, 184, 72, "索引任务", "PENDING / PROCESSING / DONE", "#ea580c", "TASK")
    node(lines, "loader", 316, 458, 184, 72, "Loader + Chunking", "PDF · Word · MD · TXT", "#f97316", "DOC")
    node(lines, "embed", 544, 458, 166, 72, "Embedding", "text-embedding-v3 · 1024D", "#9333ea", "EMB")
    node(lines, "publish", 316, 594, 184, 72, "索引发布", "chunk + version + metadata", "#16a34a", "WRITE")

    container(lines, "query", 770, 390, 670, 344, "ONLINE QUERY PIPELINE", "认证授权 → 改写检索 → 精排裁剪 → 引用回答 → 同步 / SSE")
    node(lines, "rewrite", 798, 458, 170, 72, "Query Rewrite", "HyDE · 多路查询", "#9333ea", "QRY")
    node(lines, "retrieve", 1006, 458, 184, 72, "Hybrid Retriever", "向量 + 全文 · kb_id 硬过滤", "#2563eb", "RAG")
    node(lines, "rrf", 1226, 458, 174, 72, "RRF Fusion", "去重 · 融合排序", "#2563eb", "RRF")
    node(lines, "rerank", 798, 594, 170, 72, "Reranker", "gte-rerank · 800ms 超时", "#dc2626", "RANK")
    node(lines, "context", 1006, 594, 184, 72, "Context + Citation", "Token 裁剪 · 来源溯源", "#16a34a", "CTX")
    node(lines, "answer", 1226, 594, 174, 72, "Chat Model", "基于上下文回答 / 拒答", "#009688", "LLM")

    container(lines, "data", 60, 766, 1380, 146, "DATA & PROVIDERS", "业务数据、缓存、对象存储与百炼 OpenAI-compatible 外部服务")
    node(lines, "repos", 92, 830, 250, 62, "Repository 层", "KB · Document · Chunk · Chat · Eval", "#475569", "DAO")
    node(lines, "postgres", 402, 830, 258, 62, "PostgreSQL + PGVector", "业务表 · 向量 · 全文索引", "#336791", "PG")
    node(lines, "redis", 720, 830, 190, 62, "Redis 7", "缓存 · 任务进度 · 预算", "#dc382d", "R7")
    node(lines, "minio", 970, 830, 190, 62, "MinIO", "原始上传文档", "#c2410c", "OBJ")
    node(lines, "bailian", 1220, 830, 190, 62, "阿里云百炼", "Chat · Embedding · Rerank", "#7c3aed", "AI")

    container(lines, "quality", 60, 944, 1380, 132, "QUALITY & OPERATIONS", "指标、成本、预算、评估与可追踪性")
    node(lines, "metrics", 102, 998, 250, 58, "TokenMetrics + Budget", "六类 Token · Redis 原子闸门", "#ea580c", "COST")
    node(lines, "evaluation", 424, 998, 258, 58, "RAGAS Evaluation", "Hit Rate · MRR · Faithfulness", "#7c3aed", "EVAL")
    node(lines, "observability", 754, 998, 260, 58, "Prometheus + Grafana", "耗时 · 质量 · 成本告警", "#f46800", "MON")
    node(lines, "trace", 1084, 998, 300, 58, "Trace / Logs / Feedback", "OpenTelemetry · 回答反馈 · 审计", "#64748b", "OBS")

    edge(lines, "react-to-api", "react", "fastapi", [(257, 200), (257, 280)], "#2563eb", "arrow-blue")
    edge(lines, "ops-to-fastapi", "ops-client", "fastapi", [(646, 200), (646, 220), (440, 220), (440, 250), (370, 250), (370, 280)], "#ea580c", "arrow-orange")
    edge(lines, "api-client-to-routes", "api-client", "routes", [(1055, 200), (1055, 280)], "#2563eb", "arrow-blue")
    edge(lines, "api-to-identity", "fastapi", "identity", [(392, 309), (476, 309)], "#dc2626", "arrow-red")
    edge(lines, "identity-to-routes", "identity", "routes", [(756, 309), (840, 309)], "#dc2626", "arrow-red")
    edge(lines, "routes-to-index", "routes", "index-task", [(920, 338), (920, 354), (64, 354), (64, 438), (180, 438), (180, 458)], "#ea580c", "arrow-orange", dash="5 3", width=1.8)
    edge(lines, "routes-to-query", "routes", "rewrite", [(1260, 338), (1432, 338), (1432, 438), (887, 438), (887, 458)], "#2563eb", "arrow-blue", width=2.2)

    edge(lines, "task-to-loader", "index-task", "loader", [(272, 494), (316, 494)], "#2563eb", "arrow-blue")
    edge(lines, "loader-to-embed", "loader", "embed", [(500, 494), (544, 494)], "#9333ea", "arrow-purple")
    edge(lines, "embed-to-publish", "embed", "publish", [(560, 530), (560, 560), (520, 560), (500, 560), (500, 630)], "#9333ea", "arrow-purple")
    edge(lines, "rewrite-to-retrieve", "rewrite", "retrieve", [(968, 494), (1006, 494)], "#9333ea", "arrow-purple")
    edge(lines, "retrieve-to-rrf", "retrieve", "rrf", [(1190, 494), (1226, 494)], "#2563eb", "arrow-blue")
    edge(lines, "rrf-to-rerank", "rrf", "rerank", [(1313, 530), (1313, 560), (760, 560), (760, 630), (798, 630)], "#2563eb", "arrow-blue")
    edge(lines, "rerank-to-context", "rerank", "context", [(968, 630), (1006, 630)], "#16a34a", "arrow-green")
    edge(lines, "context-to-answer", "context", "answer", [(1190, 630), (1226, 630)], "#2563eb", "arrow-blue")
    edge(lines, "rerank-fallback", "rerank", "rrf", [(883, 666), (883, 700), (1450, 700), (1450, 494), (1400, 494)], "#dc2626", "arrow-red", dash="6 4", width=1.8)

    edge(lines, "publish-to-repos", "publish", "repos", [(316, 630), (217, 630), (217, 830)], "#16a34a", "arrow-green", width=2.0)
    edge(lines, "repos-to-postgres", "repos", "postgres", [(342, 861), (402, 861)], "#16a34a", "arrow-green", width=2.0)
    edge(lines, "embed-to-bailian", "embed", "bailian", [(627, 530), (627, 720), (1200, 720), (1200, 796), (1220, 796), (1220, 830)], "#9333ea", "arrow-purple", width=1.8)
    edge(lines, "routes-to-trace", "routes", "trace", [(1360, 309), (1480, 309), (1480, 978), (1234, 978), (1234, 998)], "#6b7280", "arrow-gray", dash="4 3", width=1.4)

    edge_label(lines, "label-index", "routes-to-index", 560, 374, 92, "上传 / 重建")
    edge_label(lines, "label-query", "routes-to-query", 1080, 408, 74, "问题")
    edge_label(lines, "label-fallback", "rerank-fallback", 1050, 688, 96, "超时回退")
    edge_label(lines, "label-permission", "identity-to-routes", 798, 240, 78, "授权范围")

    lines.append('<g id="legend" data-graph-role="legend">')
    text(lines, 92, 1090, "图例", size=11, fill="#111827", weight=700, anchor="start")
    legend_item(lines, 140, 1086, "#2563eb", "arrow-blue", "主请求 / 查询")
    legend_item(lines, 310, 1086, "#16a34a", "arrow-green", "读写 / 存储", dash="5 3")
    legend_item(lines, 480, 1086, "#9333ea", "arrow-purple", "模型转换 / 扩展")
    legend_item(lines, 680, 1086, "#dc2626", "arrow-red", "认证 / 降级", dash="6 4")
    legend_item(lines, 850, 1086, "#6b7280", "arrow-gray", "异步观测", dash="4 3")
    text(lines, 1400, 1090, "边界：kb_id + 当前版本 + DONE + 未删除", size=11, fill="#6b7280", anchor="end")
    lines.append("</g>")

    lines.append("</svg>")
    return "\n".join(lines)


if __name__ == "__main__":
    OUT.write_text(build_svg(), encoding="utf-8")
    print(f"generated: {OUT}")
