import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  App,
  Button,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Tag,
} from 'antd';
import {
  CheckCircleOutlined,
  DeleteOutlined,
  EditOutlined,
  PlayCircleOutlined,
  PlusOutlined,
} from '@ant-design/icons';
import dayjs from 'dayjs';
import PageHeader from '@/components/PageHeader';
import { evalApi, kbApi } from '@/api';
import type {
  ChunkSummary,
  EvalDataset,
  EvaluationReport,
  KnowledgeBase,
} from '@/types';

interface DatasetFormValues {
  question: string;
  expected_answer?: string;
  expected_chunk_ids?: number[];
  status?: string;
}

const ALL_DATASET_STATUS = '__ACTIVE_DATASET__';
const DATASET_STATUS_LABELS: Record<string, string> = {
  CANDIDATE: '候选',
  ACTIVE: '有效',
  NEEDS_REVIEW: '待审核',
  ARCHIVED: '已归档',
};
const HISTORY_PAGE_SIZE = 8;

function formatRate(value: number | null | undefined): string {
  if (value == null) {
    return '未评估';
  }
  return `${(value * 100).toFixed(1)}%`;
}

function getErrorDetail(error: unknown): string | undefined {
  const data = (error as { response?: { data?: { message?: string; detail?: string } } })?.response
    ?.data;
  return data?.message || data?.detail;
}

function formatChunkLocation(chunk: ChunkSummary): string {
  const location: string[] = [];
  if (chunk.page_number != null) {
    location.push(`第${chunk.page_number}页`);
  }
  if (chunk.section_title) {
    location.push(chunk.section_title);
  }
  return location.join(' · ') || '未标注位置';
}

export default function EvalPage() {
  const { message } = App.useApp();
  const [form] = Form.useForm<DatasetFormValues>();

  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBase[]>([]);
  const [selectedKbId, setSelectedKbId] = useState<number | null>(null);
  const [datasetStatus, setDatasetStatus] = useState<string>(ALL_DATASET_STATUS);
  const [historyVersion, setHistoryVersion] = useState<number | undefined>(undefined);
  const [historyVersions, setHistoryVersions] = useState<number[]>([]);
  const [running, setRunning] = useState(false);
  const [history, setHistory] = useState<EvaluationReport[]>([]);
  const [historyTotal, setHistoryTotal] = useState(0);
  const [historyPage, setHistoryPage] = useState(1);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [dataset, setDataset] = useState<EvalDataset[]>([]);
  const [datasetLoading, setDatasetLoading] = useState(false);
  const [chunks, setChunks] = useState<ChunkSummary[]>([]);
  const [chunksLoading, setChunksLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [editingItem, setEditingItem] = useState<EvalDataset | null>(null);
  const [permissionDenied, setPermissionDenied] = useState(false);
  const requestSequence = useRef(0);
  const latestRequestSequence = useRef(0);

  const [latestReport, setLatestReport] = useState<EvaluationReport | null>(null);
  const selectedKb = useMemo(
    () => knowledgeBases.find((kb) => kb.id === selectedKbId) || null,
    [knowledgeBases, selectedKbId],
  );
  const canManageEvaluation = selectedKb?.permission === 'ADMIN';
  const nextVersion = useMemo(
    () => (historyVersions.length ? Math.max(...historyVersions) + 1 : 1),
    [historyVersions],
  );

  const fetchEvaluationData = useCallback(async (
    kbId: number,
    statusFilter?: string,
    page = 1,
  ) => {
    const requestId = ++requestSequence.current;
    const datasetStatusFilter =
      statusFilter === ALL_DATASET_STATUS ? undefined : statusFilter;
    setHistoryLoading(true);
    setDatasetLoading(true);
    setChunksLoading(true);
    try {
      const [historyResponse, datasetResponse, chunksResponse] = await Promise.all([
        evalApi.getHistory(kbId, { page, pageSize: HISTORY_PAGE_SIZE }),
        evalApi.listDataset(kbId, datasetStatusFilter),
        evalApi.listChunks(kbId),
      ]);
      // 用户快速切换版本或知识库时，旧请求不能覆盖最新筛选结果。
      if (requestId !== requestSequence.current) {
        return;
      }
      const historyData = historyResponse.data.data;
      const historyPageData = Array.isArray(historyData)
        ? {
            items: historyData,
            total: historyData.length,
            page,
            page_size: HISTORY_PAGE_SIZE,
            versions: historyData.map((report) => report.eval_version),
          }
        : historyData;
      setHistory(historyPageData.items);
      setHistoryTotal(historyPageData.total);
      setHistoryVersions(historyPageData.versions);
      setDataset(datasetResponse.data.data);
      setChunks(chunksResponse.data.data);
      setPermissionDenied(false);
    } catch (error: unknown) {
      if (requestId !== requestSequence.current) {
        return;
      }
      if ((error as { response?: { status?: number } })?.response?.status === 403) {
        setPermissionDenied(true);
      } else {
        message.error({ content: getErrorDetail(error) || '读取评估数据失败', key: 'evaluation-load' });
      }
    } finally {
      if (requestId === requestSequence.current) {
        setHistoryLoading(false);
        setDatasetLoading(false);
        setChunksLoading(false);
      }
    }
  }, [message]);

  const fetchLatestReport = useCallback(async (kbId: number, versionFilter?: number) => {
    const requestId = ++latestRequestSequence.current;
    try {
      const response = await evalApi.getHistory(kbId, {
        version: versionFilter,
        page: 1,
        pageSize: 1,
      });
      if (requestId !== latestRequestSequence.current) {
        return;
      }
      const data = response.data.data;
      setLatestReport(Array.isArray(data) ? data[0] || null : data.items[0] || null);
    } catch (error: unknown) {
      if (requestId === latestRequestSequence.current) {
        message.error({ content: getErrorDetail(error) || '读取最新评估报告失败', key: 'evaluation-latest-load' });
        setLatestReport(null);
      }
    }
  }, [message]);

  useEffect(() => {
    kbApi
      .list()
      .then((res) => {
        // 评估接口要求 ADMIN 权限，下拉框只展示具备该权限的知识库。
        const evaluationKnowledgeBases = res.data.data.filter(
          (knowledgeBase) => knowledgeBase.permission === 'ADMIN',
        );
        setKnowledgeBases(evaluationKnowledgeBases);
        const first = evaluationKnowledgeBases[0];
        if (first) {
          setSelectedKbId(first.id);
          setHistoryPage(1);
        } else {
          setSelectedKbId(null);
        }
      })
      .catch((error: unknown) => {
        message.error({
          content: getErrorDetail(error) || '读取知识库失败',
          key: 'evaluation-kb-load',
        });
      });
  }, [message]);

  useEffect(() => {
    if (selectedKbId == null) {
      return;
    }
    const selectedKnowledgeBase = knowledgeBases.find((kb) => kb.id === selectedKbId);
    if (!selectedKnowledgeBase) {
      return;
    }
    if (selectedKnowledgeBase.permission !== 'ADMIN') {
      setPermissionDenied(true);
      setHistory([]);
      setDataset([]);
      setChunks([]);
      return;
    }
    setPermissionDenied(false);
    void fetchEvaluationData(selectedKbId, datasetStatus, historyPage);
  }, [datasetStatus, fetchEvaluationData, historyPage, knowledgeBases, selectedKbId]);

  useEffect(() => {
    if (selectedKbId == null || !knowledgeBases.some((kb) => kb.id === selectedKbId)) {
      setLatestReport(null);
      return;
    }
    void fetchLatestReport(selectedKbId, historyVersion);
  }, [fetchLatestReport, historyVersion, knowledgeBases, selectedKbId]);

  const runEvaluation = async () => {
    if (selectedKbId == null) {
      message.warning('请选择知识库');
      return;
    }

    // 前置检查：确认有 ACTIVE 状态的评估数据集
    const activeDatasets = dataset.filter((item) => item.status === 'ACTIVE');
    if (activeDatasets.length === 0) {
      message.warning({
        content: '没有可运行的 ACTIVE 标准问题，请先添加标准问题并设置为"有效"状态',
        duration: 6,
      });
      return;
    }

    setRunning(true);
    // 显示持续的加载提示
    const loadingMessage = message.loading({
      content: `正在评估 ${activeDatasets.length} 个标准问题，请耐心等待...`,
      duration: 0, // 不自动关闭
      key: 'evaluation-running',
    });

    try {
      const res = await evalApi.runEvaluation(selectedKbId);
      loadingMessage(); // 关闭加载提示
      message.success(`评估完成：版本 ${res.data.data.eval_version}`);
      await fetchEvaluationData(selectedKbId, datasetStatus, historyPage);
      if (!historyVersion) {
        setLatestReport(res.data.data);
      } else {
        await fetchLatestReport(selectedKbId, historyVersion);
      }
    } catch (error: unknown) {
      loadingMessage(); // 关闭加载提示
      const errorDetail = getErrorDetail(error);
      console.error('Evaluation run failed:', error);

      // 特殊处理超时错误
      if ((error as any)?.code === 'ECONNABORTED' || (error as any)?.message?.includes('timeout')) {
        message.error({
          content: `评估运行超时。这可能是因为：1) 问题数量较多（当前 ${activeDatasets.length} 个）2) LLM API 响应慢。建议减少问题数量或稍后重试。`,
          key: 'evaluation-action',
          duration: 10,
        });
      } else {
        message.error({
          content: errorDetail || '评估执行失败，请检查浏览器控制台获取详细信息',
          key: 'evaluation-action',
          duration: 8,
        });
      }
    } finally {
      setRunning(false);
    }
  };

  const openCreateModal = () => {
    setEditingItem(null);
    form.resetFields();
    setModalOpen(true);
  };

  const openEditModal = (item: EvalDataset) => {
    setEditingItem(item);
    form.setFieldsValue({
      question: item.question,
      expected_answer: item.expected_answer || '',
      expected_chunk_ids: item.expected_chunk_ids || [],
      status: item.status,
    });
    setModalOpen(true);
  };

  const saveDataset = async (values: DatasetFormValues) => {
    if (selectedKbId == null) {
      return;
    }
    setSaving(true);
    try {
      const payload = {
        question: values.question,
        expected_answer: values.expected_answer?.trim() || null,
        expected_chunk_ids: values.expected_chunk_ids?.length ? values.expected_chunk_ids : null,
        status: values.status || null,
      };
      if (editingItem) {
        await evalApi.updateQuestion(selectedKbId, editingItem.id, payload);
        message.success('标准问题已更新');
      } else {
        await evalApi.addQuestion(selectedKbId, payload);
        message.success('标准问题已添加');
      }
      setModalOpen(false);
      await fetchEvaluationData(selectedKbId, datasetStatus, historyPage);
    } catch (error: unknown) {
      message.error({
        content: getErrorDetail(error) || '保存标准问题失败',
        key: 'evaluation-action',
      });
    } finally {
      setSaving(false);
    }
  };

  const deleteDataset = async (item: EvalDataset) => {
    if (selectedKbId == null) {
      return;
    }
    try {
      await evalApi.deleteQuestion(selectedKbId, item.id);
      message.success('标准问题已删除');
      await fetchEvaluationData(selectedKbId, datasetStatus, historyPage);
    } catch (error: unknown) {
      message.error({
        content: getErrorDetail(error) || '删除标准问题失败',
        key: 'evaluation-action',
      });
    }
  };

  const reportCards = latestReport
    ? [
        { label: '命中率@5', value: formatRate(latestReport.hit_rate_at_5) },
        { label: 'MRR@5', value: formatRate(latestReport.mrr_at_5) },
        { label: '忠实度', value: formatRate(latestReport.avg_faithfulness) },
        { label: '答案相关性', value: formatRate(latestReport.avg_answer_relevancy) },
        { label: '上下文召回', value: formatRate(latestReport.avg_context_recall) },
        { label: '上下文精度', value: formatRate(latestReport.avg_context_precision) },
        { label: '拒答率', value: formatRate(latestReport.refusal_rate) },
      ]
    : [];

  const chunkOptions = chunks.map((chunk) => ({
    label: `${chunk.document_name} · #${chunk.chunk_index} · ${formatChunkLocation(chunk)}`,
    value: chunk.chunk_id,
  }));

  const historyColumns = [
    { title: '版本', dataIndex: 'eval_version', key: 'eval_version', width: 150 },
    {
      title: '时间',
      dataIndex: 'eval_at',
      key: 'eval_at',
      width: 150,
      render: (value: string) => (
        <span className="font-data text-[12px]">{dayjs(value).format('MM-DD HH:mm')}</span>
      ),
    },
    {
      title: '题量',
      dataIndex: 'total_questions',
      key: 'total_questions',
      width: 80,
    },
    {
      title: '成功',
      dataIndex: 'success_count',
      key: 'success_count',
      width: 72,
    },
    {
      title: '部分成功',
      dataIndex: 'partial_count',
      key: 'partial_count',
      width: 88,
    },
    {
      title: '失败',
      dataIndex: 'failed_count',
      key: 'failed_count',
      width: 72,
    },
    {
      title: '检索样本',
      dataIndex: 'retrieval_sample_count',
      key: 'retrieval_sample_count',
      width: 92,
    },
    {
      title: '命中数',
      dataIndex: 'hit_count',
      key: 'hit_count',
      width: 72,
    },
    {
      title: '命中率@5',
      key: 'hit_rate_at_5',
      width: 90,
      render: (_: unknown, record: EvaluationReport) => formatRate(record.hit_rate_at_5),
    },
    {
      title: 'MRR@5',
      key: 'mrr_at_5',
      width: 90,
      render: (_: unknown, record: EvaluationReport) => formatRate(record.mrr_at_5),
    },
    {
      title: '忠实度样本',
      dataIndex: 'faithfulness_sample_count',
      key: 'faithfulness_sample_count',
      width: 100,
    },
    {
      title: '忠实度',
      key: 'avg_faithfulness',
      width: 90,
      render: (_: unknown, record: EvaluationReport) => formatRate(record.avg_faithfulness),
    },
    {
      title: '答案相关性样本',
      dataIndex: 'answer_relevancy_sample_count',
      key: 'answer_relevancy_sample_count',
      width: 120,
    },
    {
      title: '答案相关性',
      key: 'avg_answer_relevancy',
      width: 105,
      render: (_: unknown, record: EvaluationReport) => formatRate(record.avg_answer_relevancy),
    },
    {
      title: '上下文召回样本',
      dataIndex: 'context_recall_sample_count',
      key: 'context_recall_sample_count',
      width: 120,
    },
    {
      title: '上下文召回',
      key: 'avg_context_recall',
      width: 105,
      render: (_: unknown, record: EvaluationReport) => formatRate(record.avg_context_recall),
    },
    {
      title: '上下文精度样本',
      dataIndex: 'context_precision_sample_count',
      key: 'context_precision_sample_count',
      width: 120,
    },
    {
      title: '上下文精度',
      key: 'avg_context_precision',
      width: 105,
      render: (_: unknown, record: EvaluationReport) => formatRate(record.avg_context_precision),
    },
    {
      title: '拒答数',
      dataIndex: 'refusal_count',
      key: 'refusal_count',
      width: 72,
    },
    {
      title: '拒答率',
      key: 'refusal_rate',
      width: 90,
      render: (_: unknown, record: EvaluationReport) => formatRate(record.refusal_rate),
    },
  ];

  const datasetColumns = [
    {
      title: '标准问题',
      dataIndex: 'question',
      key: 'question',
      render: (question: string, record: EvalDataset) => (
        <div className="max-w-[420px]">
          <div className="line-clamp-2">{question}</div>
          <div className="mt-1">
            <Tag>{DATASET_STATUS_LABELS[record.status] || record.status}</Tag>
            {record.expected_chunk_ids?.length ? (
              <span className="font-data text-[11px] text-faint">
                {record.expected_chunk_ids.length} 个目标 chunk
              </span>
            ) : null}
          </div>
        </div>
      ),
    },
    {
      title: '期望答案',
      dataIndex: 'expected_answer',
      key: 'expected_answer',
      render: (value: string | null) => (
        <span className="text-soft text-[13px] line-clamp-2">{value || '未标注'}</span>
      ),
    },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 140,
      render: (value: string) => (
        <span className="font-data text-[12px]">{dayjs(value).format('MM-DD HH:mm')}</span>
      ),
    },
    {
      title: '更新时间',
      dataIndex: 'updated_at',
      key: 'updated_at',
      width: 140,
      render: (value: string) => (
        <span className="font-data text-[12px]">{dayjs(value).format('MM-DD HH:mm')}</span>
      ),
    },
    {
      title: '操作',
      key: 'actions',
      width: 110,
      render: (_: unknown, record: EvalDataset) => (
        <Space>
          <Button
            size="small"
            disabled={!canManageEvaluation}
            icon={<EditOutlined />}
            onClick={() => openEditModal(record)}
          />
          <Popconfirm
            title="删除标准问题"
            okText="删除"
            cancelText="取消"
            okButtonProps={{ danger: true }}
            onConfirm={() => deleteDataset(record)}
          >
            <Button
              size="small"
              danger
              disabled={!canManageEvaluation}
              icon={<DeleteOutlined />}
            />
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <div className="min-h-full px-4 py-5 md:px-6 md:py-6">
      <PageHeader
        eyebrow="RAG EVALUATION"
        title="效果评估"
        description="用固定问题集衡量检索命中率、排序质量、忠实度和拒答稳定性。"
      />

      <div className="archive-card p-4 mb-4">
        <div className="flex flex-wrap items-end gap-3">
          <div className="min-w-[240px] flex-1">
            <div className="eyebrow mb-1.5">知识库</div>
            <Select
              value={selectedKbId}
              options={knowledgeBases.map((kb) => ({ label: kb.name, value: kb.id }))}
              onChange={(value) => {
                setSelectedKbId(value);
                setDatasetStatus(ALL_DATASET_STATUS);
                setHistoryVersion(undefined);
                setHistoryPage(1);
              }}
              placeholder={knowledgeBases.length ? '选择知识库' : '暂无评估权限的知识库'}
              disabled={!knowledgeBases.length}
              className="w-full"
            />
          </div>
          <div className="w-[220px]">
            <div className="eyebrow mb-1.5">评估版本</div>
            <InputNumber
              value={nextVersion}
              min={1}
              precision={0}
              disabled
              className="w-full font-data"
            />
          </div>
          <Button
            type="primary"
            icon={<PlayCircleOutlined />}
            loading={running}
            disabled={!canManageEvaluation}
            onClick={runEvaluation}
          >
            运行评估
          </Button>
        </div>
      </div>

      {permissionDenied && (
        <div className="mb-4 border border-[#e6c7c1] bg-[#fff8f6] px-4 py-3 text-[13px] text-seal">
          当前账号没有该知识库的评估管理权限。
        </div>
      )}

      <div className="archive-card p-4 mb-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <div className="eyebrow">LATEST REPORT</div>
            <h2 className="text-[16px] font-semibold mt-1">
              {selectedKb?.name || '未选择知识库'}
            </h2>
          </div>
          {latestReport && (
            <div className="text-right">
              <div className="eyebrow">版本</div>
              <Select
                value={historyVersion}
                allowClear
                showSearch
                options={historyVersions.map((item) => ({ label: String(item), value: item }))}
                onChange={(value) => {
                  setHistoryVersion(value || undefined);
                }}
                placeholder={latestReport.eval_version}
                className="mt-1 min-w-[180px] text-left font-data"
                size="small"
              />
            </div>
          )}
        </div>

        {latestReport ? (
          <>
            <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-7 gap-3 mt-4">
              {reportCards.map((card) => (
                <div key={card.label} className="border border-line bg-[#f7f8f4] p-3">
                  <div className="eyebrow">{card.label}</div>
                  <div className="font-data text-[19px] mt-1.5">{card.value}</div>
                </div>
              ))}
            </div>
            <div className="grid grid-cols-3 gap-3 mt-3 text-[12.5px]">
              <div>成功 {latestReport.success_count}</div>
              <div>部分成功 {latestReport.partial_count}</div>
              <div>失败 {latestReport.failed_count}</div>
            </div>
          </>
        ) : (
          <Empty className="mt-4" description="尚未运行评估" />
        )}
      </div>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_420px]">
        <div className="archive-card p-0 overflow-hidden">
          <div className="px-4 py-3 border-b border-line flex items-center justify-between">
            <div>
              <div className="eyebrow">DATASET</div>
              <div className="text-[15px] font-semibold mt-1">标准问题集</div>
            </div>
            <Space>
              <Select
                value={datasetStatus}
                allowClear
                options={[
                  { label: '全部（不含已归档）', value: ALL_DATASET_STATUS },
                  { label: '候选', value: 'CANDIDATE' },
                  { label: '有效', value: 'ACTIVE' },
                  { label: '待审核', value: 'NEEDS_REVIEW' },
                  { label: '已归档', value: 'ARCHIVED' },
                ]}
                onChange={(value) => {
                  setDatasetStatus(value || ALL_DATASET_STATUS);
                }}
                placeholder="筛选状态"
                className="min-w-[170px]"
                size="small"
              />
              <Button
                icon={<PlusOutlined />}
                disabled={!canManageEvaluation}
                onClick={openCreateModal}
              >
                添加问题
              </Button>
            </Space>
          </div>
          <Table
            rowKey="id"
            loading={datasetLoading}
            columns={datasetColumns}
            dataSource={dataset}
            pagination={{ pageSize: 8, showSizeChanger: false }}
            scroll={{ x: 760 }}
            locale={{ emptyText: '暂无标准问题' }}
          />
        </div>

        <div className="archive-card p-0 overflow-hidden">
          <div className="px-4 py-3 border-b border-line">
            <div className="eyebrow">HISTORY</div>
            <div className="text-[15px] font-semibold mt-1">评估历史</div>
          </div>
          <Table
            rowKey="eval_version"
            loading={historyLoading}
            columns={historyColumns}
            dataSource={history}
            pagination={{
              current: historyPage,
              pageSize: HISTORY_PAGE_SIZE,
              total: historyTotal,
              showSizeChanger: false,
              onChange: (page) => setHistoryPage(page),
              showTotal: (total) => `共 ${total} 条`,
            }}
            size="small"
            scroll={{ x: 1800 }}
            locale={{ emptyText: '暂无评估记录' }}
          />
        </div>
      </div>

      <Modal
        title={editingItem ? '编辑标准问题' : '添加标准问题'}
        open={modalOpen}
        confirmLoading={saving}
        onCancel={() => setModalOpen(false)}
        onOk={() => form.submit()}
        okText="保存"
        cancelText="取消"
        width={720}
      >
        <Form form={form} layout="vertical" onFinish={saveDataset} requiredMark={false}>
          <Form.Item
            name="question"
            label="问题"
            rules={[{ required: true, message: '请输入问题' }]}
          >
            <Input.TextArea rows={2} placeholder="用户会怎么问？" />
          </Form.Item>
          <Form.Item name="expected_answer" label="期望答案">
            <Input.TextArea rows={3} placeholder="可留空，若已标注目标 chunk" />
          </Form.Item>
          <Form.Item
            name="expected_chunk_ids"
            label="目标 chunk"
            extra={
              chunksLoading ? (
                <span>
                  <CheckCircleOutlined className="mr-1" /> 读取 chunk 中...
                </span>
              ) : (
                '选择应被检索命中的 chunk，可多选。'
              )
            }
          >
            <Select
              mode="multiple"
              options={chunkOptions}
              loading={chunksLoading}
              placeholder="选择目标 chunk"
              showSearch
              optionFilterProp="label"
            />
          </Form.Item>
          <Form.Item
            name="status"
            label="状态"
            initialValue="ACTIVE"
            extra="设置评估数据集的状态：有效、候选、待审核或已归档"
          >
            <Select
              options={[
                { label: '有效', value: 'ACTIVE' },
                { label: '候选', value: 'CANDIDATE' },
                { label: '待审核', value: 'NEEDS_REVIEW' },
                { label: '已归档', value: 'ARCHIVED' },
              ]}
              placeholder="选择状态"
            />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
