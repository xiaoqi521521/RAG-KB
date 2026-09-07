"""
更新评估测试集数据 - 仅针对 policy.pdf
清理旧数据，添加新的评估问题（每个问题只包含一个疑问）
"""
import asyncio
import sys
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text
from app.core.database import engine


async def view_current_chunks():
    """查看当前 kb_id=1 的分块"""
    async with engine.begin() as conn:
        result = await conn.execute(text("""
            SELECT id, doc_id, chunk_index,
                   LEFT(content, 200) as content_preview
            FROM kb_doc_chunk
            WHERE kb_id = 1
            ORDER BY id
        """))
        rows = result.fetchall()
        print(f"\n=== kb_id=1 的分块 (共 {len(rows)} 个) ===")
        for row in rows:
            print(f"Chunk ID: {row.id}, doc_id: {row.doc_id}, chunk_index: {row.chunk_index}")
            print(f"  内容: {row.content_preview}...")
            print()
        return rows


async def archive_old_eval_data():
    """归档 kb_id=1 的所有旧评估数据（将 status 设置为 ARCHIVED）"""
    async with engine.begin() as conn:
        result = await conn.execute(text("""
            UPDATE kb_eval_dataset
            SET status = 'ARCHIVED'
            WHERE kb_id = 1 AND status != 'ARCHIVED'
            RETURNING id, question
        """))
        archived = result.fetchall()
        print(f"\n=== 归档了 {len(archived)} 条旧评估数据 ===")
        for row in archived:
            print(f"  已归档 ID={row.id}: {row.question}")


async def build_chunk_mapping(chunks):
    """根据 chunk 内容建立关键词到 chunk_id 的映射"""
    async with engine.begin() as conn:
        result = await conn.execute(text("""
            SELECT id, content
            FROM kb_doc_chunk
            WHERE kb_id = 1
            ORDER BY id
        """))
        rows = result.fetchall()

        # 建立关键词映射
        mapping = {}
        for row in rows:
            content = row.content.lower()

            # 根据内容关键词建立映射
            if "入职手续" in content or ("新员工" in content and "材料" in content):
                mapping.setdefault("入职手续", []).append(row.id)
            if "试用期" in content:
                mapping.setdefault("试用期", []).append(row.id)
            if "薪酬结构" in content or ("固定薪资" in content and "绩效奖金" in content):
                mapping.setdefault("薪酬结构", []).append(row.id)
            if "调薪" in content:
                mapping.setdefault("调薪", []).append(row.id)
            if "福利" in content and ("体检" in content or "餐补" in content or "交通补贴" in content):
                mapping.setdefault("员工福利", []).append(row.id)
            if "工作时间" in content or ("09:00" in content and "18:00" in content):
                mapping.setdefault("工作时间", []).append(row.id)
            if "弹性工作制" in content:
                mapping.setdefault("弹性工作", []).append(row.id)
            if "加班" in content and "补偿" in content:
                mapping.setdefault("加班政策", []).append(row.id)
            if "年假" in content or ("假期类型" in content and "5-20天" in content):
                mapping.setdefault("年假", []).append(row.id)
            if "病假" in content:
                mapping.setdefault("病假", []).append(row.id)
            if "婚假" in content:
                mapping.setdefault("婚假", []).append(row.id)
            if "绩效评级" in content or ("s(卓越)" in content or "a(优秀)" in content):
                mapping.setdefault("绩效评级", []).append(row.id)
            if "利益冲突" in content or ("兼职" in content and "竞争对手" in content):
                mapping.setdefault("利益冲突", []).append(row.id)
            if "信息安全" in content or ("屏幕锁定" in content and "保密" in content):
                mapping.setdefault("信息安全", []).append(row.id)
            if "培训" in content and ("技能提升" in content or "管理力" in content):
                mapping.setdefault("培训", []).append(row.id)
            if "晋升" in content and ("p序列" in content or "m序列" in content):
                mapping.setdefault("晋升体系", []).append(row.id)
            if "离职流程" in content or ("主动离职" in content and "提前" in content):
                mapping.setdefault("离职流程", []).append(row.id)
            if "申诉" in content and ("多层级" in content or "员工关系委员会" in content):
                mapping.setdefault("申诉机制", []).append(row.id)

        return mapping


async def add_new_eval_datasets(chunk_mapping):
    """添加新的评估测试集 - 每个问题只包含一个疑问"""

    # 根据 policy.pdf (从心工作室员工手册) 的实际内容创建问题
    # 每个 question 只包含一个问题，不要有两个问题
    new_datasets = [
        {
            "question": "新员工入职需要携带哪些材料？",
            "expected_answer": "需携带：身份证原件及复印件（2份）、最高学历证书及学位证书原件及复印件、近6个月内1寸彩色免冠照片2张、上一家公司离职证明原件、银行卡（工资发放专用）、体检报告（入职前30天内）。",
            "expected_chunk_ids": chunk_mapping.get("入职手续", []),
        },
        {
            "question": "试用期时长是如何规定的？",
            "expected_answer": "试用期时长按岗位级别确定，一般为1-6个月。",
            "expected_chunk_ids": chunk_mapping.get("试用期", []),
        },
        {
            "question": "公司的薪酬结构包括哪些部分？",
            "expected_answer": "公司实行\"固定薪资 + 绩效奖金 + 股权激励\"的薪酬体系。月固定工资占70%-80%，每月15日发放；季度绩效奖金占15%-25%，依据当季KPI考核结果发放；年终奖视公司业绩，通常为1-6个月固定工资；股权激励视级别，P6及以上岗位可参与期权计划。",
            "expected_chunk_ids": chunk_mapping.get("薪酬结构", []),
        },
        {
            "question": "员工福利包括哪些内容？",
            "expected_answer": "包括：每年一次全面健康体检、每工作日50元餐补、交通补贴（市区内600元/月，郊区1000元/月）、生日礼金500元购物卡、节日福利礼包、员工学习基金每年3000元、健身房报销上限2400元/年等。",
            "expected_chunk_ids": chunk_mapping.get("员工福利", []),
        },
        {
            "question": "公司的标准工作时间是怎样的？",
            "expected_answer": "周一至周五 09:00-18:00，午休时间12:00-13:00（1小时）。法定节假日休息，特殊情况按加班政策执行。",
            "expected_chunk_ids": chunk_mapping.get("工作时间", []),
        },
        {
            "question": "什么是弹性工作制？",
            "expected_answer": "研发、产品等岗位实行弹性工作制，核心工作时间为10:00-16:00，员工可在此基础上自由调整上下班时间，但每日工作时长不得低于8小时。",
            "expected_chunk_ids": chunk_mapping.get("弹性工作", []),
        },
        {
            "question": "加班如何补偿？",
            "expected_answer": "加班须事先获得主管审批。工作日加班补偿为调休（1:1），节假日加班按法律规定支付三倍工资或调休。超时加班（每月累计超过36小时）须经VP级以上管理者批准。",
            "expected_chunk_ids": chunk_mapping.get("加班政策", []),
        },
        {
            "question": "员工年假有多少天？",
            "expected_answer": "年假为5-20天，全薪，需主管审批。具体天数根据工龄确定。",
            "expected_chunk_ids": chunk_mapping.get("年假", []),
        },
        {
            "question": "病假需要提供什么证明？",
            "expected_answer": "病假有证明的按实际天数，薪资按比例发放，需主管+HR审批。",
            "expected_chunk_ids": chunk_mapping.get("病假", []),
        },
        {
            "question": "婚假有多少天？",
            "expected_answer": "婚假3天，全薪，需主管+HR审批。",
            "expected_chunk_ids": chunk_mapping.get("婚假", []),
        },
        {
            "question": "绩效评级标准是怎样的？",
            "expected_answer": "绩效分为5个等级：S(卓越)10%、A(优秀)25%、B(达标)50%、C(待改进)10%、D(不合格)5%。",
            "expected_chunk_ids": chunk_mapping.get("绩效评级", []),
        },
        {
            "question": "员工可以从事兼职工作吗？",
            "expected_answer": "员工不得在任职期间于竞争对手或业务相关公司从事兼职工作，不得以个人名义接受客户或供应商的馈赠（价值超过500元须上报）。副业经营须事先书面告知HR并获得批准。",
            "expected_chunk_ids": chunk_mapping.get("利益冲突", []),
        },
        {
            "question": "信息安全方面有哪些要求？",
            "expected_answer": "工作电脑须设置屏幕锁定（离座超过3分钟自动锁屏）；禁止通过个人邮件、微信等渠道传输公司敏感数据；客户数据属于高度机密，仅限有权限的人员访问；离职时须归还全部公司设备及资料，删除个人设备上的公司数据；发现信息安全事件须在1小时内上报信息安全团队。",
            "expected_chunk_ids": chunk_mapping.get("信息安全", []),
        },
        {
            "question": "公司提供哪些培训机会？",
            "expected_answer": "公司提供技能提升课程（每季度至少1次）、管理力发展项目（每年度）、安全合规培训（每年度必修）、行业峰会/外训（视情况，员工申请HR审批）等培训机会。",
            "expected_chunk_ids": chunk_mapping.get("培训", []),
        },
        {
            "question": "公司的晋升体系是怎样的？",
            "expected_answer": "公司设立专业技术（P序列）与管理（M序列）双通道晋升体系。员工可根据自身发展方向选择晋升路径，不强制由技术转管理。晋升评审每年10月启动，晋升须满足在当前级别至少满12个月，且最近两次绩效不低于B。",
            "expected_chunk_ids": chunk_mapping.get("晋升体系", []),
        },
        {
            "question": "员工主动离职需要提前多久通知？",
            "expected_answer": "员工主动离职须提前书面通知，试用期员工提前3天，正式员工提前30天。",
            "expected_chunk_ids": chunk_mapping.get("离职流程", []),
        },
        {
            "question": "公司有哪些申诉渠道？",
            "expected_answer": "公司设立多层级申诉机制：第一层为直属主管（适用于日常工作纠纷）、第二层为人力资源部（适用于薪酬、绩效等制度性纠纷）、第三层为员工关系委员会（适用于重大申诉事项）、匿名举报邮箱ethics@techstars.cn（适用于违规、违纪举报）。",
            "expected_chunk_ids": chunk_mapping.get("申诉机制", []),
        },
    ]

    async with engine.begin() as conn:
        added_count = 0
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
            added_count += 1
            print(f"✓ 已添加评估数据集 ID={new_id}: {dataset['question']}")

        print(f"\n共成功添加 {added_count} 条评估数据集")


async def verify_results():
    """验证结果"""
    async with engine.begin() as conn:
        # 查看 ACTIVE 状态的数据
        result = await conn.execute(text("""
            SELECT id, question, expected_chunk_ids, status
            FROM kb_eval_dataset
            WHERE kb_id = 1 AND status = 'ACTIVE'
            ORDER BY id
        """))
        active_rows = result.fetchall()
        print(f"\n=== ACTIVE 状态的评估数据（共 {len(active_rows)} 条）===")
        for row in active_rows:
            print(f"ID: {row.id}, Status: {row.status}")
            print(f"  问题: {row.question}")
            print(f"  期望chunk_ids: {row.expected_chunk_ids}")
            print()

        # 查看 ARCHIVED 状态的数据
        result = await conn.execute(text("""
            SELECT id, question, status
            FROM kb_eval_dataset
            WHERE kb_id = 1 AND status = 'ARCHIVED'
            ORDER BY id
        """))
        archived_rows = result.fetchall()
        print(f"=== ARCHIVED 状态的评估数据（共 {len(archived_rows)} 条）===")
        for row in archived_rows:
            print(f"ID: {row.id}, Status: {row.status}, 问题: {row.question}")


async def main():
    print("=" * 70)
    print("更新评估数据集 - 仅针对 policy.pdf (从心工作室员工手册)")
    print("=" * 70)

    # 1. 查看当前分块
    print("\n步骤 1：查看当前分块数据")
    chunks = await view_current_chunks()

    # 2. 归档旧的评估数据
    print("\n步骤 2：归档旧的评估数据")
    await archive_old_eval_data()

    # 3. 建立 chunk 映射
    print("\n步骤 3：建立 chunk 映射")
    chunk_mapping = await build_chunk_mapping(chunks)
    print("\n关键词到 chunk_id 的映射：")
    for key, chunk_ids in chunk_mapping.items():
        print(f"  {key}: {chunk_ids}")

    # 4. 添加新的评估数据集
    print("\n" + "=" * 70)
    print("步骤 4：添加新的评估数据集")
    print("=" * 70)
    await add_new_eval_datasets(chunk_mapping)

    # 5. 验证结果
    print("\n" + "=" * 70)
    print("步骤 5：验证结果")
    print("=" * 70)
    await verify_results()

    print("\n✅ 所有操作完成！")


if __name__ == "__main__":
    asyncio.run(main())
