/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        paper: '#f3f4f0',
        card: '#fbfbf9',
        ink: '#1f2628',
        soft: '#5f6b6a',
        faint: '#8b9694',
        line: '#dfe2d9',
        pine: {
          DEFAULT: '#2f5d50',
          deep: '#24493f',
          wash: '#e6ede9',
        },
        seal: {
          DEFAULT: '#bf3b2b',
          wash: '#f7e9e6',
        },
        amber: '#a8752c',
      },
      fontFamily: {
        data: ['"IBM Plex Mono"', 'ui-monospace', 'SFMono-Regular', 'monospace'],
      },
    },
  },
  plugins: [],
};
