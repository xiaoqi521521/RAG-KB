"""测试评估数据集状态更新功能"""
import asyncio
from app.core.database import engine
from sqlalchemy import text


async def test_status_field():
    """验证 status 字段可以被正确设置和更新"""
    async with engine.begin() as conn:
        # 查询现有的评估数据集
        result = await conn.execute(text("""
            SELECT id, kb_id, question, status, created_at
            FROM kb_eval_dataset
            WHERE kb_id = 1
            ORDER BY id DESC
            LIMIT 5
        """))
        datasets = result.fetchall()

        print("\n=== 当前评估数据集（前5条）===")
        for row in datasets:
            print(f"ID={row.id}, KB_ID={row.kb_id}, Status={row.status}")
            print(f"  问题: {row.question[:50]}...")

        # 验证可用的状态值
        print("\n=== 验证状态约束 ===")
        result = await conn.execute(text("""
            SELECT conname, consrc
            FROM pg_constraint
            WHERE conrelid = 'kb_eval_dataset'::regclass
            AND conname LIKE '%status%'
        """))
        constraints = result.fetchall()
        for row in constraints:
            print(f"约束: {row.conname}")
            print(f"  条件: {row.consrc}")

        print("\n✅ 状态字段验证完成")


if __name__ == "__main__":
    asyncio.run(test_status_field())
