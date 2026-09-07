"""恢复被删除的评估数据集记录并设置为 ARCHIVED 状态"""
import asyncio
from app.core.database import engine
from sqlalchemy import text


async def restore_archived_data():
    """恢复 kb_id=1 的旧评估数据集并设置为 ARCHIVED"""
    async with engine.begin() as conn:
        # 检查是否已存在这些 ID 的记录
        result = await conn.execute(text("""
            SELECT id FROM kb_eval_dataset WHERE id IN (1, 2)
        """))
        existing_ids = [row.id for row in result.fetchall()]

        if existing_ids:
            print(f"警告: ID {existing_ids} 的记录已存在，跳过恢复")
            return

        # 恢复 dataset_id=1 的记录
        await conn.execute(text("""
            INSERT INTO kb_eval_dataset
            (id, kb_id, question, expected_answer, expected_chunk_ids, created_by, status, review_reason, source_feedback_id, created_at, updated_at)
            VALUES
            (1, 1, '新员工入职第一天需要做什么？', '领取工牌和电脑，配置 VPN 和开发环境，与直属 Leader 完成对齐会，阅读代码规范。', '{193}', 1, 'ARCHIVED', NULL, NULL, '2026-06-21 07:34:25.257068', '2026-06-21 07:34:25.257068')
        """))
        print("✓ 已恢复 dataset_id=1: 新员工入职第一天需要做什么？ (状态: ARCHIVED)")

        # 恢复 dataset_id=2 的记录
        await conn.execute(text("""
            INSERT INTO kb_eval_dataset
            (id, kb_id, question, expected_answer, expected_chunk_ids, created_by, status, review_reason, source_feedback_id, created_at, updated_at)
            VALUES
            (2, 1, '年假是怎么规定的？', '工作满 1 年未满 10 年享有 5 天年假，工作满 10 年以上享有 10 天年假。', '{193}', 1, 'ARCHIVED', NULL, NULL, '2026-06-21 07:34:25.257068', '2026-06-21 07:34:25.257068')
        """))
        print("✓ 已恢复 dataset_id=2: 年假是怎么规定的？ (状态: ARCHIVED)")

        # 重置序列，确保后续插入不会冲突
        result = await conn.execute(text("""
            SELECT MAX(id) FROM kb_eval_dataset
        """))
        max_id = result.scalar()
        await conn.execute(text(f"""
            SELECT setval('kb_eval_dataset_id_seq', {max_id}, true)
        """))
        print(f"✓ 已重置序列到 {max_id}")

        # 验证恢复结果
        result = await conn.execute(text("""
            SELECT id, question, status
            FROM kb_eval_dataset
            WHERE kb_id = 1
            ORDER BY id
        """))
        rows = result.fetchall()
        print(f"\n=== KB_ID=1 的所有评估数据集（共 {len(rows)} 条）===")
        for row in rows:
            status_emoji = "📦" if row.status == "ARCHIVED" else "✅"
            print(f"{status_emoji} ID={row.id}, Status={row.status}: {row.question[:50]}...")


if __name__ == "__main__":
    asyncio.run(restore_archived_data())
