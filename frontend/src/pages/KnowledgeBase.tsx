import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { App, Button, Form, Input, Modal, Switch, Tag } from 'antd';
import { ArrowRightOutlined, FileDoneOutlined, PlusOutlined } from '@ant-design/icons';
import dayjs from 'dayjs';
import PageHeader from '@/components/PageHeader';
import { kbApi } from '@/api';
import type { KnowledgeBase, KnowledgeBaseCreateRequest } from '@/types';

const PERMISSION_COLORS: Record<string, string> = {
  READ: 'default',
  WRITE: 'green',
  ADMIN: 'gold',
};

export default function KnowledgeBasePage() {
  const navigate = useNavigate();
  const { message } = App.useApp();
  const [items, setItems] = useState<KnowledgeBase[]>([]);
  const [loading, setLoading] = useState(true);
  const [modalOpen, setModalOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [form] = Form.useForm<KnowledgeBaseCreateRequest>();

  const fetchList = useCallback(async () => {
    setLoading(true);
    try {
      const res = await kbApi.list();
      setItems(res.data.data);
    } catch {
      message.error({ content: '读取知识库失败', key: 'kb-list-load-error' });
    } finally {
      setLoading(false);
    }
  }, [message]);

  useEffect(() => {
    void fetchList();
  }, [fetchList]);

  const handleCreate = async (values: KnowledgeBaseCreateRequest) => {
    setCreating(true);
    try {
      await kbApi.create({
        ...values,
        description: values.description || null,
      });
      message.success('知识库已创建');
      setModalOpen(false);
      form.resetFields();
      await fetchList();
    } catch (error: unknown) {
      const detail = (error as { response?: { data?: { detail?: string } } })?.response
        ?.data?.detail;
      message.error(detail || '创建失败');
    } finally {
      setCreating(false);
    }
  };

  return (
    <div className="min-h-full px-4 py-5 md:px-6 md:py-6">
      <PageHeader
        eyebrow="KNOWLEDGE BASES"
        title="知识库"
        description="按知识库划定数据边界；权限在检索层硬过滤，不依赖提示词约束。"
        actions={
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setModalOpen(true)}>
            新建知识库
          </Button>
        }
      />

      {loading ? (
        <div className="archive-card p-10 text-center text-soft">读取知识库中...</div>
      ) : items.length === 0 ? (
        <div className="archive-card p-10 text-center">
          <FileDoneOutlined className="text-3xl text-faint mb-3" />
          <div className="text-[15px] font-medium">还没有知识库</div>
          <p className="text-soft text-[13px] mt-1">创建第一个知识库后，即可上传企业文档。</p>
          <Button
            type="primary"
            className="mt-4"
            icon={<PlusOutlined />}
            onClick={() => setModalOpen(true)}
          >
            新建知识库
          </Button>
        </div>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 2xl:grid-cols-3">
          {items.map((kb) => (
            <article key={kb.id} className="archive-card hoverable flex flex-col p-4">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="font-data text-[11px] text-faint">KB-{String(kb.id).padStart(4, '0')}</div>
                  <h2 className="text-[16px] font-semibold mt-1 truncate" title={kb.name}>
                    {kb.name}
                  </h2>
                </div>
                <Tag color={PERMISSION_COLORS[kb.permission] || 'default'} className="font-data">
                  {kb.permission}
                </Tag>
              </div>

              <p className="text-soft text-[13px] leading-relaxed mt-3 line-clamp-3 min-h-[60px]">
                {kb.description || '暂无描述'}
              </p>

              <div className="grid grid-cols-2 gap-2 mt-3 text-[12px]">
                <div>
                  <div className="eyebrow">授权主体</div>
                  <div className="mt-1">{kb.department_id}</div>
                </div>
                <div>
                  <div className="eyebrow">访问</div>
                  <div className="mt-1">{kb.is_public ? '公开读取' : '授权读取'}</div>
                </div>
              </div>

              <div className="flex items-center justify-between border-t border-line mt-4 pt-3">
                <span className="font-data text-[11px] text-faint">
                  {kb.created_at ? dayjs(kb.created_at).format('YYYY-MM-DD') : '未记录时间'}
                </span>
                <Button
                  size="small"
                  iconPosition="end"
                  icon={<ArrowRightOutlined />}
                  onClick={() => navigate(`/kb/${kb.id}/documents`)}
                >
                  文档
                </Button>
              </div>
            </article>
          ))}
        </div>
      )}

      <Modal
        title="新建知识库"
        open={modalOpen}
        confirmLoading={creating}
        onCancel={() => setModalOpen(false)}
        onOk={() => form.submit()}
        okText="创建"
        cancelText="取消"
        destroyOnClose
      >
        <Form form={form} layout="vertical" onFinish={handleCreate} requiredMark={false}>
          <Form.Item
            name="name"
            label="名称"
            rules={[{ required: true, message: '请输入知识库名称' }]}
          >
            <Input maxLength={100} placeholder="如：员工手册" />
          </Form.Item>
          <Form.Item name="description" label="描述">
            <Input.TextArea rows={3} maxLength={2000} placeholder="说明这个知识库收录哪些内容" />
          </Form.Item>
          <Form.Item
            name="department_id"
            label="部门标识"
            rules={[{ required: true, message: '请输入部门标识' }]}
          >
            <Input maxLength={50} placeholder="如 HR / TECH / ALL" />
          </Form.Item>
          <Form.Item name="is_public" label="公开读取" valuePropName="checked" initialValue={false}>
            <Switch checkedChildren="公开" unCheckedChildren="授权" />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
