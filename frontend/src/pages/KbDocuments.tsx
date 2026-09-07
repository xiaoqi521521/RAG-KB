import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { App, Button, Popconfirm, Space, Table, Tag, Upload } from 'antd';
import type { UploadFile } from 'antd';
import {
  ArrowLeftOutlined,
  CloudUploadOutlined,
  DeleteOutlined,
  DownloadOutlined,
  ReloadOutlined,
  SyncOutlined,
} from '@ant-design/icons';
import dayjs from 'dayjs';
import PageHeader from '@/components/PageHeader';
import { kbApi } from '@/api';
import type { KbDocument } from '@/types';

const STATUS_META: Record<string, { color: string; text: string }> = {
  PENDING: { color: 'default', text: '待处理' },
  PROCESSING: { color: 'processing', text: '索引中' },
  DONE: { color: 'success', text: '已发布' },
  FAILED: { color: 'error', text: '失败' },
};

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function formatIndexError(error: string): string {
  if (/MinerU SDK request failed|SSL|UNEXPECTED_EOF|解析服务/i.test(error)) {
    return '文档解析服务暂时不可用，请稍后点击“重建索引”重试。若多次失败，请联系管理员检查解析服务或网络连接。';
  }
  if (/empty (MinerU )?document|no valid chunks|有效内容/i.test(error)) {
    return '未能从文档中提取有效内容，请确认文件不为空且内容可以正常读取。';
  }
  if (/embedding|向量化/i.test(error)) {
    return '文档向量化暂时失败，请稍后点击“重建索引”重试。';
  }
  if (/索引任务无法完成|索引处理失败|索引服务暂时不可用/i.test(error)) {
    return error;
  }
  return '索引处理失败，请稍后点击“重建索引”重试。若仍然失败，请联系管理员。';
}

export default function KbDocumentsPage() {
  const { kbId } = useParams<{ kbId: string }>();
  const navigate = useNavigate();
  const { message } = App.useApp();
  const numericKbId = Number(kbId);

  const [documents, setDocuments] = useState<KbDocument[]>([]);
  const [loading, setLoading] = useState(true);
  const [uploading, setUploading] = useState(false);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [fileList, setFileList] = useState<UploadFile[]>([]);
  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const hasActiveTask = useMemo(
    () => documents.some((doc) => doc.status === 'PENDING' || doc.status === 'PROCESSING'),
    [documents],
  );

  const fetchDocuments = useCallback(async () => {
    if (!Number.isFinite(numericKbId)) {
      return;
    }
    setLoading(true);
    try {
      const res = await kbApi.getDocuments(numericKbId);
      setDocuments(res.data.data);
    } catch (error: unknown) {
      const status = (error as { response?: { status?: number } })?.response?.status;
      if (status === 403 || status === 404) {
        navigate('/kb');
        return;
      }
      message.error({ content: '读取文档失败', key: 'kb-documents-load-error' });
    } finally {
      setLoading(false);
    }
  }, [message, navigate, numericKbId]);

  useEffect(() => {
    void fetchDocuments();
  }, [fetchDocuments]);

  useEffect(() => {
    if (!hasActiveTask) {
      if (pollingRef.current) {
        clearInterval(pollingRef.current);
        pollingRef.current = null;
      }
      return;
    }
    pollingRef.current = setInterval(() => {
      kbApi
        .getDocuments(numericKbId)
        .then((res) => setDocuments(res.data.data))
        .catch(() => undefined);
    }, 5000);
    return () => {
      if (pollingRef.current) {
        clearInterval(pollingRef.current);
        pollingRef.current = null;
      }
    };
  }, [hasActiveTask, numericKbId]);

  const handleUpload = async () => {
    if (!selectedFile) {
      message.warning('请选择要上传的文档');
      return;
    }
    setUploading(true);
    try {
      const res = await kbApi.uploadDocument(numericKbId, selectedFile);
      message.success(res.data.data.message || '文档已提交索引');
      setSelectedFile(null);
      setFileList([]);
      await fetchDocuments();
    } catch (error: unknown) {
      const detail = (error as { response?: { data?: { detail?: string } } })?.response
        ?.data?.detail;
      message.error(detail || '文档上传失败，请检查文件格式和网络连接后重试。');
    } finally {
      setUploading(false);
    }
  };

  const handleDownload = async (doc: KbDocument) => {
    try {
      const res = await kbApi.downloadDocument(numericKbId, doc.id);
      const url = URL.createObjectURL(res.data);
      const link = document.createElement('a');
      link.href = url;
      link.download = doc.file_name;
      link.click();
      URL.revokeObjectURL(url);
    } catch {
      message.error('下载失败');
    }
  };

  const handleReindex = async (doc: KbDocument) => {
    try {
      const res = await kbApi.reindexDocument(numericKbId, doc.id);
      message.success(res.data.data.message || '已提交重建');
      await fetchDocuments();
    } catch (error: unknown) {
      const detail = (error as { response?: { data?: { detail?: string } } })?.response
        ?.data?.detail;
      message.error(detail || '提交重建失败');
    }
  };

  const handleDelete = async (doc: KbDocument) => {
    try {
      await kbApi.deleteDocument(numericKbId, doc.id);
      message.success('文档已删除');
      await fetchDocuments();
    } catch {
      message.error('删除失败');
    }
  };

  const publishedCount = documents.filter((doc) => doc.status === 'DONE').length;
  const failedCount = documents.filter((doc) => doc.status === 'FAILED').length;

  const columns = [
    {
      title: '文档',
      dataIndex: 'file_name',
      key: 'file_name',
      render: (_: unknown, record: KbDocument) => (
        <div className="min-w-[220px]">
          <div className="font-medium">{record.file_name}</div>
          <div className="font-data text-[11px] text-faint mt-1">
            {record.file_type.toUpperCase()} · {formatSize(record.file_size)} · V{record.version}
          </div>
          {record.error_msg && (
            <div className="text-[12px] text-seal mt-1">{formatIndexError(record.error_msg)}</div>
          )}
        </div>
      ),
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 110,
      render: (status: string) => (
        <Tag color={STATUS_META[status]?.color || 'default'}>
          {STATUS_META[status]?.text || status}
        </Tag>
      ),
    },
    {
      title: 'Chunk',
      dataIndex: 'chunk_count',
      key: 'chunk_count',
      width: 90,
      render: (value: number | null) => <span className="font-data">{value ?? '-'}</span>,
    },
    {
      title: 'Token',
      dataIndex: 'token_count',
      key: 'token_count',
      width: 100,
      render: (value: number | null) => <span className="font-data">{value ?? '-'}</span>,
    },
    {
      title: '上传时间',
      dataIndex: 'uploaded_at',
      key: 'uploaded_at',
      width: 140,
      render: (value: string | null) => (
        <span className="font-data text-[12px]">
          {value ? dayjs(value).format('YYYY-MM-DD HH:mm') : '-'}
        </span>
      ),
    },
    {
      title: '操作',
      key: 'actions',
      width: 180,
      render: (_: unknown, record: KbDocument) => (
        <Space>
          <Button
            size="small"
            icon={<DownloadOutlined />}
            onClick={() => handleDownload(record)}
          />
          <Popconfirm
            title="重建索引"
            description="将创建新版本并在完成后替换当前索引。"
            okText="重建"
            cancelText="取消"
            onConfirm={() => handleReindex(record)}
          >
            <Button size="small" icon={<SyncOutlined />} />
          </Popconfirm>
          <Popconfirm
            title="删除文档"
            description="删除后不可恢复。"
            okText="删除"
            cancelText="取消"
            okButtonProps={{ danger: true }}
            onConfirm={() => handleDelete(record)}
          >
            <Button size="small" danger icon={<DeleteOutlined />} />
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <div className="min-h-full px-4 py-5 md:px-6 md:py-6">
      <PageHeader
        eyebrow="DOCUMENT INDEX"
        title="文档管理"
        description="上传后进入离线索引管道：解析、分块、向量化、写入 PGVector。"
        actions={
          <Button icon={<ArrowLeftOutlined />} onClick={() => navigate('/kb')}>
            返回
          </Button>
        }
      />

      <div className="archive-card p-4 mb-4">
        <div className="grid gap-4 lg:grid-cols-[minmax(320px,420px)_minmax(0,1fr)]">
          <Upload.Dragger
            multiple={false}
            maxCount={1}
            fileList={fileList}
            accept=".pdf,.docx,.md,.txt"
            beforeUpload={(file) => {
              setSelectedFile(file);
              return false;
            }}
            onRemove={() => setSelectedFile(null)}
            onChange={({ fileList: nextFileList }) => setFileList(nextFileList)}
          >
            <p className="text-pine text-2xl mt-1">
              <CloudUploadOutlined />
            </p>
            <p className="text-[14px] font-medium mt-2">拖拽文档到这里</p>
            <p className="text-faint text-[12px] mt-1">支持 PDF、DOCX、Markdown、TXT</p>
          </Upload.Dragger>

          <div className="flex flex-col justify-between">
            <div className="grid grid-cols-3 gap-3">
              <div>
                <div className="eyebrow">总数</div>
                <div className="font-data text-xl mt-1">{documents.length}</div>
              </div>
              <div>
                <div className="eyebrow">已发布</div>
                <div className="font-data text-xl mt-1 text-pine">{publishedCount}</div>
              </div>
              <div>
                <div className="eyebrow">失败</div>
                <div className="font-data text-xl mt-1 text-seal">{failedCount}</div>
              </div>
            </div>
            <div className="flex flex-wrap items-center gap-2 mt-5">
              <Button
                type="primary"
                loading={uploading}
                disabled={!selectedFile}
                onClick={handleUpload}
              >
                上传并索引
              </Button>
              <Button icon={<ReloadOutlined />} onClick={() => fetchDocuments()}>
                刷新
              </Button>
              {hasActiveTask && (
                <span className="retrieval-line">
                  <span className="retrieval-dot" /> 每 5 秒自动更新状态
                </span>
              )}
            </div>
          </div>
        </div>
      </div>

      <div className="archive-card p-0 overflow-hidden">
        <Table
          rowKey="id"
          loading={loading}
          columns={columns}
          dataSource={documents}
          pagination={{ pageSize: 10, showSizeChanger: false }}
          scroll={{ x: 900 }}
          locale={{ emptyText: '暂无文档，请先上传' }}
        />
      </div>
    </div>
  );
}
