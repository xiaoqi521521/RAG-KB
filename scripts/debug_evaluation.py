#!/usr/bin/env python3
"""评估功能调试脚本

用于排查运行评估时的问题。
"""
import asyncio
import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession

from app.core.config import get_settings
from app.models import EvalDataset, EvalResult, KnowledgeBase


async def check_evaluation_status(kb_id: int) -> None:
    """检查评估相关数据的状态"""
    settings = get_settings()
    engine = create_async_engine(settings.database_url, echo=False)

    async with AsyncSession(engine) as session:
        # 检查知识库是否存在
        kb_result = await session.execute(
            select(KnowledgeBase).where(KnowledgeBase.id == kb_id)
        )
        kb = kb_result.scalar_one_or_none()

        if kb is None:
            print(f"❌ 知识库 ID {kb_id} 不存在")
            return

        print(f"✅ 知识库: {kb.name} (ID: {kb.id})")
        print(f"   创建者: {kb.created_by}")
        print(f"   是否删除: {kb.is_deleted}")
        print()

        # 检查评估数据集
        datasets_result = await session.execute(
            select(EvalDataset).where(EvalDataset.kb_id == kb_id)
        )
        datasets = list(datasets_result.scalars().all())

        if not datasets:
            print("❌ 没有评估数据集")
            print("   解决方案: 在前端添加至少一个标准问题")
            return

        print(f"📊 评估数据集总数: {len(datasets)}")
        status_counts = {}
        for dataset in datasets:
            status_counts[dataset.status] = status_counts.get(dataset.status, 0) + 1

        for status, count in sorted(status_counts.items()):
            emoji = "✅" if status == "ACTIVE" else "⚠️"
            print(f"   {emoji} {status}: {count} 个")

        active_count = status_counts.get("ACTIVE", 0)
        if active_count == 0:
            print()
            print("❌ 没有 ACTIVE 状态的评估数据集")
            print("   解决方案: 编辑至少一个问题，将状态设置为'有效'(ACTIVE)")
            print()
            print("   现有问题列表:")
            for i, dataset in enumerate(datasets[:5], 1):
                print(f"      {i}. [{dataset.status}] {dataset.question[:50]}")
            if len(datasets) > 5:
                print(f"      ... 还有 {len(datasets) - 5} 个问题")
        else:
            print(f"   ✅ 可以运行评估 ({active_count} 个 ACTIVE 问题)")

        print()

        # 检查历史评估结果
        results_result = await session.execute(
            select(EvalResult)
            .join(EvalDataset, EvalResult.dataset_id == EvalDataset.id)
            .where(EvalDataset.kb_id == kb_id)
            .order_by(EvalResult.eval_version.desc())
            .limit(1)
        )
        latest_result = results_result.scalar_one_or_none()

        if latest_result:
            print(f"📈 最新评估版本: {latest_result.eval_version}")
            print(f"   下一个版本将是: {latest_result.eval_version + 1}")
        else:
            print("📈 尚未运行过评估，下一个版本将是: 1")

    await engine.dispose()


async def main() -> None:
    """主函数"""
    if len(sys.argv) < 2:
        print("用法: python scripts/debug_evaluation.py <kb_id>")
        print()
        print("示例: python scripts/debug_evaluation.py 1")
        sys.exit(1)

    try:
        kb_id = int(sys.argv[1])
    except ValueError:
        print(f"错误: kb_id 必须是整数，得到: {sys.argv[1]}")
        sys.exit(1)

    print("=== 评估功能调试 ===")
    print(f"检查知识库 ID: {kb_id}")
    print()

    await check_evaluation_status(kb_id)


if __name__ == "__main__":
    asyncio.run(main())
