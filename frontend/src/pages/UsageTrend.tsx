import { useCallback, useEffect, useState } from 'react';
import { Button, Result, Segmented, Table } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import PageHeader from '@/components/PageHeader';
import { statsApi } from '@/api';
import type { DailyUsagePoint } from '@/types';

const RANGES = [
  { label: '近 7 天', value: 7 },
  { label: '近 14 天', value: 14 },
  { label: '近 30 天', value: 30 },
];

function formatNumber(value: number): string {
  return value.toLocaleString('zh-CN');
}

export default function UsageTrendPage() {
  const [days, setDays] = useState(14);
  const [points, setPoints] = useState<DailyUsagePoint[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [forbidden, setForbidden] = useState(false);

  const fetchUsage = useCallback(async (rangeDays: number) => {
    setLoading(true);
    try {
      const res = await statsApi.getDailyUsage(rangeDays);
      setPoints(res.data.data);
      setForbidden(false);
    } catch (error: unknown) {
      const status = (error as { response?: { status?: number } })?.response?.status;
      setForbidden(status === 403);
      setPoints(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchUsage(days);
  }, [days, fetchUsage]);

  const ordered = points ? [...points].reverse() : [];
  const totalTokens = ordered.reduce((sum, item) => sum + item.tokens, 0);
  const totalCost = ordered.reduce((sum, item) => sum + Number(item.cost), 0);
  const totalRetrievalTokens = ordered.reduce(
    (sum, item) => sum + item.retrieval_tokens,
    0,
  );
  const totalRetrievalCost = ordered.reduce(
    (sum, item) => sum + Number(item.retrieval_cost),
    0,
  );
  const totalEvaluationTokens = ordered.reduce(
    (sum, item) => sum + item.evaluation_tokens,
    0,
  );
  const totalEvaluationCost = ordered.reduce(
    (sum, item) => sum + Number(item.evaluation_cost),
    0,
  );

  return (
    <div className="min-h-full px-4 py-5 md:px-6 md:py-6">
      <PageHeader
        eyebrow="DAILY USAGE"
        title="全局用量"
        description="全系统每日 Token 消耗与人民币成本估算，仅系统管理员可查看。"
        actions={
          <div className="flex items-center gap-3">
            <Segmented options={RANGES} value={days} onChange={(value) => setDays(value as number)} />
            <Button icon={<ReloadOutlined />} loading={loading} onClick={() => fetchUsage(days)}>
              刷新
            </Button>
          </div>
        }
      />

      {forbidden ? (
        <Result
          status="403"
          title="无权限访问"
          subTitle="此面板仅对系统管理员开放。"
        />
      ) : (
        <div className="grid gap-4 xl:grid-cols-[320px_minmax(0,1fr)]">
          <section className="archive-card p-5">
            <div className="eyebrow">RANGE TOTAL</div>
            <div className="flex items-end gap-2 mt-3">
              <span className="text-[34px] font-semibold leading-none">
                ¥{totalCost.toFixed(4)}
              </span>
            </div>
            <div className="font-data text-[13px] text-soft mt-4">
              TOTAL TOKENS {formatNumber(totalTokens)}
            </div>
            <div className="grid grid-cols-2 gap-3 mt-4">
              <div>
                <div className="eyebrow">RETRIEVAL TOKENS</div>
                <div className="font-data text-[13.5px] mt-1">
                  {formatNumber(totalRetrievalTokens)}
                </div>
              </div>
              <div>
                <div className="eyebrow">RETRIEVAL COST</div>
                <div className="font-data text-[13.5px] mt-1">
                  ¥{totalRetrievalCost.toFixed(4)}
                </div>
              </div>
              <div>
                <div className="eyebrow">EVALUATION TOKENS</div>
                <div className="font-data text-[13.5px] mt-1">
                  {formatNumber(totalEvaluationTokens)}
                </div>
              </div>
              <div>
                <div className="eyebrow">EVALUATION COST</div>
                <div className="font-data text-[13.5px] mt-1">
                  ¥{totalEvaluationCost.toFixed(4)}
                </div>
              </div>
            </div>
            <div className="mt-5 pt-4 border-t border-line text-[12.5px] text-soft leading-relaxed">
              Token 总量与预估成本为 Redis 非评估消耗加数据库评估 run 消耗；检索问答字段只包含 Redis 非评估消耗，评估字段包含评估 run 的 RAG 执行和 RAGAS 判定。按部署时区自然日统计，成本为配置单价下的估算值，不是账单。
            </div>
          </section>

          <section className="archive-card p-5">
            <Table
              rowKey="date"
              size="small"
              loading={loading}
              pagination={false}
              dataSource={ordered}
            >
              <Table.Column
                title="日期"
                dataIndex="date"
                width={140}
                render={(value: string) => <span className="font-data">{value}</span>}
              />
              <Table.Column
                title="Token 总量"
                dataIndex="tokens"
                align="right"
                width={130}
                render={(value: number) => <span className="font-data">{formatNumber(value)}</span>}
              />
              <Table.Column
                title="检索问答 Token"
                dataIndex="retrieval_tokens"
                align="right"
                width={150}
                render={(value: number) => <span className="font-data">{formatNumber(value)}</span>}
              />
              <Table.Column
                title="评估 Token"
                dataIndex="evaluation_tokens"
                align="right"
                width={130}
                render={(value: number) => <span className="font-data">{formatNumber(value)}</span>}
              />
              <Table.Column
                title="预估成本（CNY）"
                dataIndex="cost"
                align="right"
                width={155}
                render={(value: string) => <span className="font-data">{value}</span>}
              />
              <Table.Column
                title="检索问答成本（CNY）"
                dataIndex="retrieval_cost"
                align="right"
                width={185}
                render={(value: string) => <span className="font-data">{value}</span>}
              />
              <Table.Column
                title="评估成本（CNY）"
                dataIndex="evaluation_cost"
                align="right"
                width={155}
                render={(value: string) => <span className="font-data">{value}</span>}
              />
            </Table>
          </section>
        </div>
      )}
    </div>
  );
}
