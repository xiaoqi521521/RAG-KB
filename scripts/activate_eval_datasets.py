#!/usr/bin/env python3
"""批量激活评估数据集

将指定知识库的所有非 ARCHIVED 评估数据集设置为 ACTIVE 状态。
"""
import asyncio
import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession

from app.core.config import get_settings
from app.models import EvalDataset, EvalDatasetStatus


async def activate_datasets(kb_id: int, dry_run: bool = True) -> None:
    """激活指定知识库的评估数据集"""
    settings = get_settings()
    engine = create_async_engine(settings.database_url, echo=False)

    async with AsyncSession(engine) as session:
        # 查询需要更新的数据集
        result = await session.execute(
            select(EvalDataset).where(
                EvalDataset.kb_id == kb_id,
                EvalDataset.status != EvalDatasetStatus.ACTIVE.value,
                EvalDataset.status != EvalDatasetStatus.ARCHIVED.value,
            )
        )
        datasets = list(result.scalars().all())

        if not datasets:
            print("✅ 没有需要激活的数据集（所有非归档问题已经是 ACTIVE 状态）")
            return

        print(f"找到 {len(datasets)} 个需要激活的数据集:")
        for i, dataset in enumerate(datasets, 1):
            print(f"  {i}. [{dataset.status}] {dataset.question[:60]}")

        if dry_run:
            print()
            print("⚠️  这是试运行模式，不会实际修改数据")
            print("   要实际执行，请添加 --execute 参数")
            return

        # 执行更新
        await session.execute(
            update(EvalDataset)
            .where(
                EvalDataset.kb_id == kb_id,
                EvalDataset.status != EvalDatasetStatus.ACTIVE.value,
                EvalDataset.status != EvalDatasetStatus.ARCHIVED.value,
            )
            .values(status=EvalDatasetStatus.ACTIVE.value)
        )
        await session.commit()

        print()
        print(f"✅ 已成功激活 {len(datasets)} 个评估数据集")

    await engine.dispose()


async def main() -> None:
    """主函数"""
    if len(sys.argv) < 2:
        print("用法: python scripts/activate_eval_datasets.py <kb_id> [--execute]")
        print()
        print("示例:")
        print("  python scripts/activate_eval_datasets.py 1          # 试运行")
        print("  python scripts/activate_eval_datasets.py 1 --execute  # 实际执行")
        sys.exit(1)

    try:
        kb_id = int(sys.argv[1])
    except ValueError:
        print(f"错误: kb_id 必须是整数，得到: {sys.argv[1]}")
        sys.exit(1)

    dry_run = "--execute" not in sys.argv

    print(f"=== 批量激活评估数据集 ===")
    print(f"知识库 ID: {kb_id}")
    print(f"模式: {'试运行' if dry_run else '执行'}")
    print()

    await activate_datasets(kb_id, dry_run=dry_run)


if __name__ == "__main__":
    asyncio.run(main())
