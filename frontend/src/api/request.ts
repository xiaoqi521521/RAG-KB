import axios, { AxiosError, InternalAxiosRequestConfig } from 'axios';
import { message } from 'antd';
import type { ApiResponse } from '@/types';
import { TOKEN_STORAGE_KEY, USER_STORAGE_KEY } from '@/store/useAuthStore';

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
    } else if (detail) {
      message.error(detail);
    }

    return Promise.reject(error);
  },
);

export default instance;
