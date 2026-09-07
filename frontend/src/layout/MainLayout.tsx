import { Outlet, useLocation, useNavigate } from 'react-router-dom';
import { Button } from 'antd';
import {
  MessageOutlined,
  DatabaseOutlined,
  ExperimentOutlined,
  DashboardOutlined,
  BarChartOutlined,
  LockOutlined,
  LogoutOutlined,
} from '@ant-design/icons';
import { useEffect, useState } from 'react';
import AppLogo from '@/components/AppLogo';
import { kbApi, statsApi } from '@/api';
import { useAuthStore } from '@/store/useAuthStore';

const NAV_ITEMS = [
  { path: '/chat', label: '检索问答', icon: <MessageOutlined /> },
  { path: '/kb', label: '知识库', icon: <DatabaseOutlined /> },
  { path: '/dashboard', label: '成本监控', icon: <DashboardOutlined /> },
  { path: '/eval', label: '效果评估', icon: <ExperimentOutlined /> },
  { path: '/usage', label: '全局用量', icon: <BarChartOutlined /> },
];

export default function MainLayout() {
  const navigate = useNavigate();
  const location = useLocation();
  const { user, logout } = useAuthStore();
  const [loggingOut, setLoggingOut] = useState(false);
  const [evaluationAccess, setEvaluationAccess] = useState<boolean | null>(null);
  const [usageAccess, setUsageAccess] = useState<boolean | null>(null);

  useEffect(() => {
    if (!user) {
      setEvaluationAccess(null);
      return;
    }
    void kbApi
      .list()
      .then((res) => {
        setEvaluationAccess(res.data.data.some((kb) => kb.permission === 'ADMIN'));
      })
      .catch(() => {
        // 权限状态无法确认时按无权处理，避免直接访问评估页绕过入口控制。
        setEvaluationAccess(false);
      });
  }, [user]);

  useEffect(() => {
    if (location.pathname === '/eval' && evaluationAccess === false) {
      navigate('/chat', { replace: true });
    }
  }, [evaluationAccess, location.pathname, navigate]);

  useEffect(() => {
    if (!user) {
      setUsageAccess(null);
      return;
    }
    // 探针只判定管理员身份，不依赖 Prometheus 在线；失败静默处理避免全局弹窗。
    void statsApi
      .getUsageAccess({ skipGlobalErrorMessage: true })
      .then((res) => {
        setUsageAccess(res.data.data === true);
      })
      .catch(() => {
        // 权限状态无法确认时按无权处理，与评估入口保持一致的 fail-closed 语义。
        setUsageAccess(false);
      });
  }, [user]);

  useEffect(() => {
    if (location.pathname === '/usage' && usageAccess === false) {
      navigate('/chat', { replace: true });
    }
  }, [usageAccess, location.pathname, navigate]);

  const blockEvaluationPage = location.pathname === '/eval' && evaluationAccess !== true;
  const blockUsagePage = location.pathname === '/usage' && usageAccess !== true;

  const selectedPath = '/' + (location.pathname.split('/')[1] || 'chat');

  const handleLogout = async () => {
    setLoggingOut(true);
    logout();
    setLoggingOut(false);
    navigate('/login');
  };

  return (
    <div className="h-full flex">
      <aside className="hidden md:flex w-[212px] shrink-0 flex-col bg-pine-deep text-paper/85">
        <div className="flex items-center gap-3 px-5 h-16 border-b border-white/10">
          <AppLogo />
          <div className="min-w-0">
            <div className="text-paper font-semibold text-[15px] leading-tight">知库问答</div>
            <div className="eyebrow !text-paper/50 mt-0.5">RAG · 溯源</div>
          </div>
        </div>

        <nav className="flex-1 py-4 px-2.5 space-y-1">
          {NAV_ITEMS.map((item) => {
            const active = selectedPath === item.path;
            const isEvaluation = item.path === '/eval';
            const isUsage = item.path === '/usage';
            const locked =
              (isEvaluation && evaluationAccess === false) || (isUsage && usageAccess === false);
            const disabled =
              (isEvaluation && evaluationAccess !== true) || (isUsage && usageAccess !== true);
            const lockedTitle = isEvaluation ? '无评估管理权限' : '无系统管理员权限';
            return (
              <button
                key={item.path}
                type="button"
                disabled={disabled}
                aria-label={locked ? `${item.label}（无权限）` : item.label}
                title={locked ? lockedTitle : undefined}
                onClick={() => navigate(item.path)}
                className={`w-full flex items-center gap-3 px-3.5 py-2.5 rounded-[2px] text-[13.5px] transition-colors disabled:cursor-not-allowed disabled:opacity-45 ${
                  active
                    ? 'bg-white/10 text-paper'
                    : 'text-paper/60 hover:bg-white/5 hover:text-paper/90'
                }`}
              >
                <span className={active ? 'text-paper' : 'text-paper/50'}>{item.icon}</span>
                {item.label}
                {locked && <LockOutlined className="ml-auto text-paper/45" />}
                {active && <span className="ml-auto w-1.5 h-1.5 rounded-full bg-paper/70" />}
              </button>
            );
          })}
        </nav>

        <div className="px-5 py-4 border-t border-white/10 flex items-center gap-3">
          <span className="w-8 h-8 rounded-[2px] bg-white/10 text-paper text-sm font-semibold flex items-center justify-center shrink-0">
            {(user?.username || '访').slice(0, 1).toUpperCase()}
          </span>
          <div className="min-w-0 flex-1">
            <div className="text-paper text-[13px] truncate">{user?.username || '已登录'}</div>
            <div className="eyebrow !text-paper/40 mt-0.5">企业成员</div>
          </div>
          <Button
            type="text"
            size="small"
            loading={loggingOut}
            onClick={handleLogout}
            aria-label="退出登录"
            className="!text-paper/60 hover:!text-paper"
            icon={<LogoutOutlined />}
          />
        </div>
      </aside>

      <main className="flex-1 min-w-0 flex flex-col overflow-hidden">
        <div className="md:hidden flex items-center gap-2.5 px-4 h-12 border-b border-line bg-card shrink-0">
          <AppLogo size={26} />
          <span className="font-semibold text-[14px]">知库问答</span>
        </div>
        <div className="flex-1 overflow-auto">
          {blockEvaluationPage || blockUsagePage ? null : <Outlet />}
        </div>
        <nav className="md:hidden grid grid-cols-4 border-t border-line bg-card shrink-0">
          {NAV_ITEMS.map((item) => {
            const active = selectedPath === item.path;
            const isEvaluation = item.path === '/eval';
            const isUsage = item.path === '/usage';
            const locked =
              (isEvaluation && evaluationAccess === false) || (isUsage && usageAccess === false);
            const disabled =
              (isEvaluation && evaluationAccess !== true) || (isUsage && usageAccess !== true);
            const lockedTitle = isEvaluation ? '无评估管理权限' : '无系统管理员权限';
            return (
              <button
                key={item.path}
                type="button"
                disabled={disabled}
                aria-label={locked ? `${item.label}（无权限）` : item.label}
                title={locked ? lockedTitle : undefined}
                onClick={() => navigate(item.path)}
                className={`flex flex-col items-center gap-1 py-2 text-[11px] disabled:cursor-not-allowed disabled:opacity-45 ${
                  active ? 'text-pine' : 'text-faint'
                }`}
              >
                {item.icon}
                {item.label}
                {locked && <LockOutlined className="text-[10px]" />}
              </button>
            );
          })}
        </nav>
      </main>
    </div>
  );
}
