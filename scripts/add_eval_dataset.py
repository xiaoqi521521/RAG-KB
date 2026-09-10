"""
添加和修改评估测试集数据
"""
import asyncio
import sys
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text
from app.core.database import engine


async def view_chunks():
    """查看 kb_id=1 的文档分块"""
    async with engine.begin() as conn:
        result = await conn.execute(text("""
            SELECT id, doc_id, kb_id, chunk_index,
                   LEFT(content, 150) as content_preview,
                   section_title
            FROM kb_doc_chunk
            WHERE kb_id = 1
            ORDER BY id
        """))
        rows = result.fetchall()
        print(f"\n=== 找到 {len(rows)} 个分块 ===")
        for row in rows:
            print(f"ID: {row.id}, doc_id: {row.doc_id}, chunk_index: {row.chunk_index}")
            print(f"  章节: {row.section_title}")
            print(f"  内容预览: {row.content_preview}...")
            print()


async def view_existing_eval_data():
    """查看现有的评估数据集 id=1,2"""
    async with engine.begin() as conn:
        result = await conn.execute(text("""
            SELECT id, kb_id, question, expected_answer,
                   expected_chunk_ids, status, created_by
            FROM kb_eval_dataset
            WHERE id IN (1, 2)
        """))
        rows = result.fetchall()
        print("\n=== 现有评估数据集 (id=1,2) ===")
        for row in rows:
            print(f"ID: {row.id}, kb_id: {row.kb_id}, status: {row.status}")
            print(f"  问题: {row.question}")
            print(f"  期望答案: {row.expected_answer}")
            print(f"  期望chunk_ids: {row.expected_chunk_ids}")
            print()


async def update_existing_datasets(chunk_mapping):
    """更新 id=1,2 的评估数据集"""
    # 根据实际 chunk 内容更新
    updates = [
        {
            "id": 1,
            "question": "员工年假有多少天？",
            "expected_chunk_ids": chunk_mapping.get("年假", []),
            "status": "ACTIVE"
        },
        {
            "id": 2,
            "question": "试用期多长时间？",
            "expected_chunk_ids": chunk_mapping.get("试用期", []),
            "status": "ACTIVE"
        }
    ]

    async with engine.begin() as conn:
        for update in updates:
            if not update["expected_chunk_ids"]:
                print(f"⚠ 警告：ID={update['id']} 的 expected_chunk_ids 为空，跳过更新")
                continue
            await conn.execute(text("""
                UPDATE kb_eval_dataset
                SET question = :question,
                    expected_chunk_ids = :expected_chunk_ids,
                    status = :status,
                    kb_id = 1
                WHERE id = :id
            """), {
                "id": update["id"],
                "question": update["question"],
                "expected_chunk_ids": update["expected_chunk_ids"],
                "status": update["status"]
            })
            print(f"✓ 已更新评估数据集 ID={update['id']}")


async def add_new_datasets(chunk_mapping):
    """添加 8 个新的评估测试集"""
    # 根据实际 HR 手册内容创建问题
    new_datasets = [
        {
            "question": "新员工入职第一天需要完成哪些步骤？",
            "expected_answer": "新员工入职第一天需要：1. 8:30前到前台凭录用通知书领取工牌；2. 到IT部领取笔记本电脑并配置开发环境；3. 与直属Leader进行30分钟入职对齐会；4. 阅读代码规范文档并签字确认；5. 完成OA系统账号激活和基础信息填写。",
            "expected_chunk_ids": chunk_mapping.get("入职", []),
        },
        {
            "question": "病假如何申请？需要注意什么？",
            "expected_answer": "员工因病无法正常工作，需当天上午9点前通知直属Leader和HR。病假超过3天需提供医院证明。当年累计病假超过30天，按事假处理。",
            "expected_chunk_ids": chunk_mapping.get("病假", []),
        },
        {
            "question": "婚假有多少天？如何申请？",
            "expected_answer": "员工结婚享有3天婚假。需提前1个月告知HR，并在婚后1个月内提交结婚证复印件。",
            "expected_chunk_ids": chunk_mapping.get("婚假", []),
        },
        {
            "question": "绩效考核包含哪些维度？满分多少？",
            "expected_answer": "绩效考核包含以下维度，满分100分：工作目标完成情况60分、工作质量与效率20分、团队协作与沟通10分、学习成长与创新10分。",
            "expected_chunk_ids": chunk_mapping.get("考核维度", []),
        },
        {
            "question": "绩效考核结果分为哪几个等级？年终奖金系数分别是多少？",
            "expected_answer": "考核结果分为4个等级：优秀（90-100分，年终奖金系数1.5）、良好（75-89分，年终奖金系数1.2）、合格（60-74分，年终奖金系数1.0）、待提升（60分以下，需制定改进计划）。",
            "expected_chunk_ids": chunk_mapping.get("考核结果", []),
        },
        {
            "question": "员工提出离职需要提前多久申请？",
            "expected_answer": "员工需提前30天在OA系统提交离职申请，经直属Leader和HR审批。",
            "expected_chunk_ids": chunk_mapping.get("离职申请", []),
        },
        {
            "question": "离职员工需要完成哪些工作交接？",
            "expected_answer": "离职员工需在最后工作日前完成：1. 文档整理和知识沉淀（代码、设计文档、操作手册等）；2. 与接任同事完成面对面工作交接；3. 归还公司设备（电脑、门禁卡等）；4. 财务结清（报销款、借款等）。",
            "expected_chunk_ids": chunk_mapping.get("工作交接", []),
        },
        {
            "question": "公司的核心理念是什么？主要业务是什么？",
            "expected_answer": "公司秉持\"技术驱动、用户优先\"的理念，专注于企业智能化解决方案，为各行业客户提供AI赋能服务。公司成立于2018年。",
            "expected_chunk_ids": chunk_mapping.get("公司简介", []),
        }
    ]

    async with engine.begin() as conn:
        for dataset in new_datasets:
            if not dataset["expected_chunk_ids"]:
                print(f"⚠ 警告：问题 '{dataset['question']}' 的 expected_chunk_ids 为空，跳过")
                continue
            result = await conn.execute(text("""
                INSERT INTO kb_eval_dataset
                (kb_id, question, expected_answer, expected_chunk_ids, created_by, status)
                VALUES (1, :question, :expected_answer, :expected_chunk_ids, 1, 'ACTIVE')
                RETURNING id
            """), {
                "question": dataset["question"],
                "expected_answer": dataset["expected_answer"],
                "expected_chunk_ids": dataset["expected_chunk_ids"]
            })
            new_id = result.scalar()
            print(f"✓ 已添加评估数据集 ID={new_id}: {dataset['question']}")


async def build_chunk_mapping():
    """根据 chunk 内容建立关键词到 chunk_id 的映射"""
    async with engine.begin() as conn:
        result = await conn.execute(text("""
            SELECT id, content, section_title
            FROM kb_doc_chunk
            WHERE kb_id = 1
            ORDER BY id
        """))
        rows = result.fetchall()

        # 建立关键词映射
        mapping = {}
        for row in rows:
            content = row.content.lower()
            section = (row.section_title or "").lower()

            # 根据内容关键词建立映射
            if "年假" in content or "年假" in section:
                mapping.setdefault("年假", []).append(row.id)
            if "试用期" in content or "试用期" in section:
                mapping.setdefault("试用期", []).append(row.id)
            if "入职" in content and ("第一天" in content or "步骤" in content):
                mapping.setdefault("入职", []).append(row.id)
            if "病假" in content:
                mapping.setdefault("病假", []).append(row.id)
            if "婚假" in content:
                mapping.setdefault("婚假", []).append(row.id)
            if "考核" in content and ("维度" in content or "满分" in content):
                mapping.setdefault("考核维度", []).append(row.id)
            if "考核结果" in content or ("等级" in content and "年终奖金" in content):
                mapping.setdefault("考核结果", []).append(row.id)
            if "离职申请" in content or ("提前" in content and "30" in content and "离职" in content):
                mapping.setdefault("离职申请", []).append(row.id)
            if "工作交接" in content or ("离职" in content and "交接" in content):
                mapping.setdefault("工作交接", []).append(row.id)
            if "公司简介" in section or ("成立" in content and "2018" in content):
                mapping.setdefault("公司简介", []).append(row.id)

        return mapping


async def main():
    print("=" * 60)
    print("步骤 1：查看现有数据")
    print("=" * 60)

    # 1. 查看分块数据
    await view_chunks()

    # 2. 查看现有评估数据
    await view_existing_eval_data()

    print("\n" + "=" * 60)
    print("步骤 2：建立 chunk 映射")
    print("=" * 60)

    chunk_mapping = await build_chunk_mapping()
    print("\n关键词到 chunk_id 的映射：")
    for key, chunk_ids in chunk_mapping.items():
        print(f"  {key}: {chunk_ids}")

    print("\n" + "=" * 60)
    print("步骤 3：执行更新和添加操作")
    print("=" * 60)

    # 3. 更新现有数据集
    print("\n更新现有数据集...")
    await update_existing_datasets(chunk_mapping)

    # 4. 添加新数据集
    print("\n添加新数据集...")
    await add_new_datasets(chunk_mapping)

    print("\n✓ 所有操作完成！")

    # 5. 验证结果
    print("\n" + "=" * 60)
    print("步骤 4：验证结果")
    print("=" * 60)
    await view_existing_eval_data()


if __name__ == "__main__":
    asyncio.run(main())
