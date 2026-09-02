import axios, { AxiosError, InternalAxiosRequestConfig } from 'axios';
import { message } from 'antd';
import type { ApiResponse } from '@/types';
import { TOKEN_STORAGE_KEY, USER_STORAGE_KEY } from '@/store/useAuthStore';

declare module 'axios' {
  interface AxiosRequestConfig {
    skipGlobalErrorMessage?: boolean;
  }
}

const instance = axios.create({
  baseURL: '/api/v1',
  timeout: 60000,
  headers: { 'Content-Type': 'application/json' },
});

instance.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  const token = localStorage.getItem(TOKEN_STORAGE_KEY);
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  // FormData 需要由浏览器自动补充 multipart boundary，不能沿用全局 JSON 请求头。
  if (config.data instanceof FormData) {
    config.headers.delete('Content-Type');
  }
  return config;
});

instance.interceptors.response.use(
  (response) => response,
  (error: AxiosError<ApiResponse>) => {
    const status = error.response?.status;
    const detail = error.response?.data?.message;

    if (status === 401) {
      localStorage.removeItem(TOKEN_STORAGE_KEY);
      localStorage.removeItem(USER_STORAGE_KEY);
      // 登录页自身的 catch 负责展示 401 错误文案；已在登录页时整页跳转会刷新页面吞掉提示。
      if (window.location.pathname !== '/login') {
        window.location.href = '/login';
      }
    } else if (detail && !error.config?.skipGlobalErrorMessage) {
      message.error({
        content: detail,
        key: `api-error:${status ?? 'unknown'}:${detail}`,
      });
    }

    return Promise.reject(error);
  },
);

export default instance;
