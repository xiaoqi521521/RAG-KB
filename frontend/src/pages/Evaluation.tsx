import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  App,
  Button,
  Empty,
  Form,
  Input,
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
}

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

export default function EvalPage() {
  const { message } = App.useApp();
  const [form] = Form.useForm<DatasetFormValues>();

  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBase[]>([]);
  const [selectedKbId, setSelectedKbId] = useState<number | null>(null);
  const [version, setVersion] = useState(dayjs().format('YYYYMMDD-HHmm'));
  const [running, setRunning] = useState(false);
  const [history, setHistory] = useState<EvaluationReport[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [dataset, setDataset] = useState<EvalDataset[]>([]);
  const [datasetLoading, setDatasetLoading] = useState(false);
  const [chunks, setChunks] = useState<ChunkSummary[]>([]);
  const [chunksLoading, setChunksLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [editingItem, setEditingItem] = useState<EvalDataset | null>(null);
  const [permissionDenied, setPermissionDenied] = useState(false);

  const latestReport = history[0] || null;
  const selectedKb = useMemo(
    () => knowledgeBases.find((kb) => kb.id === selectedKbId) || null,
    [knowledgeBases, selectedKbId],
  );
  const canManageEvaluation = selectedKb?.permission === 'ADMIN';

  const fetchEvaluationData = useCallback(async (kbId: number) => {
    setHistoryLoading(true);
    setDatasetLoading(true);
    setChunksLoading(true);
    try {
      const [historyResponse, datasetResponse, chunksResponse] = await Promise.all([
        evalApi.getHistory(kbId),
        evalApi.listDataset(kbId),
        evalApi.listChunks(kbId),
      ]);
      setHistory(historyResponse.data.data);
      setDataset(datasetResponse.data.data);
      setChunks(chunksResponse.data.data);
      setPermissionDenied(false);
    } catch (error: unknown) {
      if ((error as { response?: { status?: number } })?.response?.status === 403) {
        setPermissionDenied(true);
      } else {
        message.error({ content: getErrorDetail(error) || '读取评估数据失败', key: 'evaluation-load' });
      }
    } finally {
      setHistoryLoading(false);
      setDatasetLoading(false);
      setChunksLoading(false);
    }
  }, [message]);

  useEffect(() => {
    kbApi
      .list()
      .then((res) => {
        setKnowledgeBases(res.data.data);
        const first = res.data.data[0];
        if (first) {
          setSelectedKbId(first.id);
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
    void fetchEvaluationData(selectedKbId);
  }, [fetchEvaluationData, knowledgeBases, selectedKbId]);

  const runEvaluation = async () => {
    if (selectedKbId == null) {
      message.warning('请选择知识库');
      return;
    }
    if (!version.trim()) {
      message.warning('请输入评估版本');
      return;
    }
    setRunning(true);
    try {
      const res = await evalApi.runEvaluation(selectedKbId, version.trim());
      message.success(`评估完成：${res.data.data.eval_version}`);
      await fetchEvaluationData(selectedKbId);
    } catch (error: unknown) {
      message.error({
        content: getErrorDetail(error) || '评估执行失败',
        key: 'evaluation-action',
      });
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
      };
      if (editingItem) {
        await evalApi.updateQuestion(selectedKbId, editingItem.id, payload);
        message.success('标准问题已更新');
      } else {
        await evalApi.addQuestion(selectedKbId, payload);
        message.success('标准问题已添加');
      }
      setModalOpen(false);
      await fetchEvaluationData(selectedKbId);
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
      await fetchEvaluationData(selectedKbId);
    } catch (error: unknown) {
      message.error({
        content: getErrorDetail(error) || '删除标准问题失败',
        key: 'evaluation-action',
      });
    }
  };

  const reportCards = latestReport
    ? [
        { label: 'Hit Rate@5', value: formatRate(latestReport.hit_rate_at_5) },
        { label: 'MRR@5', value: formatRate(latestReport.mrr_at_5) },
        { label: 'Faithfulness', value: formatRate(latestReport.avg_faithfulness) },
        { label: '答案相关性', value: formatRate(latestReport.avg_answer_relevancy) },
        { label: '上下文召回', value: formatRate(latestReport.avg_context_recall) },
        { label: '拒答率', value: formatRate(latestReport.refusal_rate) },
      ]
    : [];

  const chunkOptions = chunks.map((chunk) => ({
    label: `${chunk.document_name} · #${chunk.chunk_index} · ${
      chunk.section_title || (chunk.page_number ? `第${chunk.page_number}页` : '未标注位置')
    }`,
    value: chunk.chunk_id,
  }));

  const historyColumns = [
    { title: '版本', dataIndex: 'eval_version', key: 'eval_version' },
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
      title: 'Hit@5',
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
      title: '忠实度',
      key: 'avg_faithfulness',
      width: 90,
      render: (_: unknown, record: EvaluationReport) => formatRate(record.avg_faithfulness),
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
            <Tag>{record.status}</Tag>
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
              onChange={setSelectedKbId}
              placeholder="选择知识库"
              className="w-full"
            />
          </div>
          <div className="w-[220px]">
            <div className="eyebrow mb-1.5">评估版本</div>
            <Input
              value={version}
              onChange={(event) => setVersion(event.target.value)}
              placeholder="如 20260829-1000"
              className="font-data"
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
              <div className="font-data text-[13px] mt-1">{latestReport.eval_version}</div>
            </div>
          )}
        </div>

        {latestReport ? (
          <>
            <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-3 mt-4">
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
            <Button
              icon={<PlusOutlined />}
              disabled={!canManageEvaluation}
              onClick={openCreateModal}
            >
              添加问题
            </Button>
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
            pagination={false}
            size="small"
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
        </Form>
      </Modal>
    </div>
  );
}
