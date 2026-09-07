import { useCallback, useEffect, useMemo, useState } from 'react';
import { App, Button } from 'antd';
import { ReloadOutlined } from '@ant-design/icons';
import PageHeader from '@/components/PageHeader';
import { statsApi } from '@/api';
import type { TokenStats } from '@/types';

const TOKEN_ITEMS = [
  { key: 'input_tokens', costKey: 'input_cost', label: '输入', color: '#2f5d50' },
  { key: 'answer_generation_tokens', costKey: 'answer_generation_cost', label: '回答生成', color: '#4a7c67' },
  { key: 'intent_tokens', costKey: 'intent_cost', label: '意图识别', color: '#3f6f8f' },
  { key: 'embedding_tokens', costKey: 'embedding_cost', label: 'Embedding', color: '#7ba08b' },
  { key: 'hyde_tokens', costKey: 'hyde_cost', label: 'HyDE', color: '#a8752c' },
  { key: 'reranker_tokens', costKey: 'reranker_cost', label: 'Reranker', color: '#c68b47' },
  { key: 'faithfulness_tokens', costKey: 'faithfulness_cost', label: '忠实度校验', color: '#bf3b2b' },
] as const;

function formatNumber(value: number): string {
  return value.toLocaleString('zh-CN');
}

export default function DashboardPage() {
  const { message } = App.useApp();
  const [stats, setStats] = useState<TokenStats | null>(null);
  const [loading, setLoading] = useState(true);

  const fetchStats = useCallback(async () => {
    setLoading(true);
    try {
      const res = await statsApi.getTokenStats();
      setStats(res.data.data);
    } catch (error: unknown) {
      const detail = (error as { response?: { data?: { detail?: string } } })?.response
        ?.data?.detail;
      message.error(detail || '读取 Token 统计失败');
    } finally {
      setLoading(false);
    }
  }, [message]);

  useEffect(() => {
    void fetchStats();
  }, [fetchStats]);

  const totalTokens = useMemo(() => {
    if (!stats) {
      return 0;
    }
    return TOKEN_ITEMS.reduce((sum, item) => sum + stats[item.key], 0);
  }, [stats]);

  const totalCost = useMemo(() => {
    if (!stats) {
      return 0;
    }
    return TOKEN_ITEMS.reduce((sum, item) => sum + Number(stats[item.costKey]), 0);
  }, [stats]);

  return (
    <div className="min-h-full px-4 py-5 md:px-6 md:py-6">
      <PageHeader
        eyebrow="TOKEN & COST"
        title="成本监控"
        description="统计当前用户的七类在线 Token 消耗与近似人民币成本。"
        actions={
          <Button icon={<ReloadOutlined />} loading={loading} onClick={() => fetchStats()}>
            刷新
          </Button>
        }
      />

      {stats ? (
        <div className="grid gap-4 xl:grid-cols-[380px_minmax(0,1fr)]">
          <section className="archive-card p-5">
            <div className="eyebrow">ESTIMATED COST</div>
            <div className="flex items-end gap-2 mt-3">
              <span className="text-[34px] font-semibold leading-none">¥{stats.estimated_cost}</span>
              <span className="text-faint text-[13px] pb-1">{stats.currency}</span>
            </div>
            <div className="font-data text-[13px] text-soft mt-4">
              TOTAL TOKENS {formatNumber(totalTokens)}
            </div>
            <div className="mt-5 pt-4 border-t border-line text-[12.5px] text-soft leading-relaxed">
              Redis 统计为近似累计用量，不是账单级账本。provider 无可靠 usage 时只记录观测，不用本地估算冒充精确 Token。
            </div>
          </section>

          <section className="archive-card p-5">
            <div className="flex flex-wrap items-end justify-between gap-3">
              <div>
                <div className="eyebrow">TOKEN BREAKDOWN</div>
                <h2 className="text-[16px] font-semibold mt-1">七类在线消耗</h2>
              </div>
              <span className="font-data text-[12px] text-faint">
                input 只统计聊天模型输入，其他类型只统计对应输出
              </span>
            </div>

            <div className="flex h-3 mt-5 overflow-hidden rounded-[1px] bg-[#eef0ea]">
              {TOKEN_ITEMS.map((item) => {
                const value = stats[item.key];
                const cost = stats[item.costKey];
                const percent = totalCost > 0 ? (Number(cost) / totalCost) * 100 : 0;
                return (
                  <div
                    key={item.key}
                    title={`${item.label} ¥${cost} · ${formatNumber(value)} tokens`}
                    style={{ width: `${percent}%`, backgroundColor: item.color }}
                  />
                );
              })}
            </div>

            <div className="grid gap-3 mt-5 sm:grid-cols-2 xl:grid-cols-3">
              {TOKEN_ITEMS.map((item) => {
                const value = stats[item.key];
                const cost = stats[item.costKey];
                const percent = totalCost > 0 ? (Number(cost) / totalCost) * 100 : 0;
                return (
                  <div key={item.key} className="border border-line bg-[#f7f8f4] p-3.5">
                    <div className="flex items-center gap-2">
                      <span className="w-2.5 h-2.5 rounded-[1px]" style={{ backgroundColor: item.color }} />
                      <span className="text-[13px]">{item.label}</span>
                    </div>
                    <div className="font-data text-[21px] mt-2">{formatNumber(value)}</div>
                    <div className="font-data text-[11px] text-faint mt-1">
                      {percent.toFixed(1)}% · ¥{cost}
                    </div>
                  </div>
                );
              })}
            </div>
          </section>
        </div>
      ) : (
        <div className="archive-card p-10 text-center text-soft">
          {loading ? '读取统计中...' : '暂无统计数据'}
        </div>
      )}
    </div>
  );
}
