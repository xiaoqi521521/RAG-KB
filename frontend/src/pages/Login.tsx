import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { App, Button, Form, Input } from 'antd';
import { ArrowRightOutlined } from '@ant-design/icons';
import { authApi } from '@/api';
import { useAuthStore } from '@/store/useAuthStore';

const PIPELINE_STEPS = [
  { index: '01', title: '混合检索', detail: '向量与全文双路召回，RRF 融合排序' },
  { index: '02', title: '精排', detail: 'Reranker 重排序，超时自动降级' },
  { index: '03', title: '溯源', detail: '引用编号对应文档、页码与章节' },
];

const DEMO_ACCOUNTS = [
  { username: 'admin', role: '系统管理员' },
  { username: 'hr001', role: 'HR 部门' },
  { username: 'tech001', role: '技术部门' },
];

export default function LoginPage() {
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();
  const { message } = App.useApp();
  const setAuth = useAuthStore((s) => s.setAuth);
  const [form] = Form.useForm<{ username: string; password: string }>();

  const onFinish = async (values: { username: string; password: string }) => {
    setLoading(true);
    try {
      const res = await authApi.login(values);
      setAuth({ username: values.username }, res.data.data);
      message.success('已登录');
      navigate('/chat');
    } catch (error: unknown) {
      const body = (error as {
        response?: { data?: { message?: string; detail?: string } };
      })?.response?.data;
      message.error(body?.message || body?.detail || '登录失败，请检查用户名或密码');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="h-full flex bg-paper">
      <div className="hidden md:flex w-[46%] max-w-[620px] flex-col justify-between bg-pine-deep text-paper px-12 py-10">
        <div className="flex items-center gap-3">
          <span
            className="w-10 h-10 rounded-[3px] bg-paper text-pine-deep font-bold text-xl flex items-center justify-center"
            style={{ boxShadow: 'inset 0 0 0 1.5px rgba(47, 93, 80, 0.35)' }}
          >
            知
          </span>
          <div>
            <div className="font-semibold text-[16px]">知库问答</div>
            <div className="eyebrow !text-paper/50 mt-0.5">企业 RAG 知识库</div>
          </div>
        </div>

        <div className="max-w-[400px]">
          <h1 className="text-[34px] leading-snug font-semibold tracking-tight">
            答案，都有出处。
          </h1>
          <p className="mt-4 text-paper/70 text-[14px] leading-relaxed">
            面向企业内部的知识库问答系统。每个回答都附编号引用印章，可回溯到具体文档与页码。
          </p>
          <div className="mt-10 space-y-5">
            {PIPELINE_STEPS.map((step) => (
              <div key={step.index} className="flex gap-4">
                <span className="font-data text-paper/45 text-[12px] pt-0.5 shrink-0">
                  {step.index}
                </span>
                <div>
                  <div className="text-[13.5px] font-medium">{step.title}</div>
                  <div className="text-paper/55 text-[12.5px] mt-0.5">{step.detail}</div>
                </div>
              </div>
            ))}
          </div>
        </div>

        <p className="text-paper/45 text-[12.5px] leading-relaxed max-w-[400px]">
          未检出相关内容时，系统明确拒答，不会用通用知识补全企业政策。
        </p>
      </div>

      <div className="flex-1 flex items-center justify-center px-6">
        <div className="w-full max-w-[400px]">
          <div className="md:hidden flex items-center gap-2.5 mb-8">
            <span className="w-9 h-9 rounded-[3px] bg-pine text-paper font-bold flex items-center justify-center">
              知
            </span>
            <span className="font-semibold text-[16px]">知库问答</span>
          </div>

          <div className="archive-card p-8">
            <div className="eyebrow">登录</div>
            <h2 className="text-xl font-semibold mt-1 mb-6 tracking-tight">使用企业账号继续</h2>
            <Form
              form={form}
              layout="vertical"
              onFinish={onFinish}
              autoComplete="off"
              requiredMark={false}
            >
              <Form.Item
                name="username"
                label="用户名"
                rules={[{ required: true, message: '请输入用户名' }]}
              >
                <Input placeholder="如 admin" size="large" />
              </Form.Item>
              <Form.Item
                name="password"
                label="密码"
                rules={[{ required: true, message: '请输入密码' }]}
              >
                <Input.Password placeholder="请输入密码" size="large" />
              </Form.Item>
              <Button
                type="primary"
                htmlType="submit"
                loading={loading}
                block
                size="large"
                iconPosition="end"
                icon={<ArrowRightOutlined />}
              >
                进入知库
              </Button>
            </Form>
          </div>

          <div className="archive-card mt-4 px-4 py-3">
            <div className="font-data text-[11px] tracking-[0.14em] text-faint mb-2">
              演示账号 · 统一口令 demo123
            </div>
            <div className="space-y-1.5">
              {DEMO_ACCOUNTS.map((account) => (
                <button
                  key={account.username}
                  type="button"
                  className="w-full flex items-center justify-between text-[12.5px] group"
                  onClick={() =>
                    form.setFieldsValue({ username: account.username, password: 'demo123' })
                  }
                >
                  <span className="font-data text-pine group-hover:underline">
                    {account.username}
                  </span>
                  <span className="text-faint">{account.role}</span>
                </button>
              ))}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
