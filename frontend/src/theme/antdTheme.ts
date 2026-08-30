import type { ThemeConfig } from 'antd';

export const antdTheme: ThemeConfig = {
  token: {
    colorPrimary: '#2f5d50',
    colorInfo: '#2f5d50',
    colorLink: '#2f5d50',
    colorSuccess: '#3f7d5c',
    colorWarning: '#a8752c',
    colorError: '#bf3b2b',
    colorTextBase: '#1f2628',
    colorTextSecondary: '#5f6b6a',
    colorBgLayout: '#f3f4f0',
    colorBgContainer: '#fbfbf9',
    colorBorder: '#d3d7cc',
    colorBorderSecondary: '#e7e9e2',
    colorSplit: '#dfe2d9',
    borderRadius: 2,
    fontSize: 14,
    fontFamily:
      "-apple-system, 'PingFang SC', 'Microsoft YaHei', 'Segoe UI', 'Noto Sans SC', sans-serif",
  },
  components: {
    Button: { fontWeight: 500 },
    Table: { headerBg: '#eef0ea', headerColor: '#5f6b6a', rowHoverBg: '#f4f6f1' },
    Modal: { titleFontSize: 16 },
    Tag: { borderRadiusSM: 2 },
  },
};
