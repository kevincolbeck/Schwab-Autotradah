/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        surface: '#0f1117',
        panel: '#1a1d27',
        border: '#2a2d3a',
        green: { DEFAULT: '#00d084', dim: '#00875a' },
        red: { DEFAULT: '#ff4d4f', dim: '#a61d24' },
        yellow: { DEFAULT: '#faad14', dim: '#876800' },
        blue: { DEFAULT: '#40a9ff' },
        gray: { muted: '#6b7280' },
      },
      fontFamily: { mono: ['JetBrains Mono', 'Consolas', 'monospace'] },
    },
  },
  plugins: [],
}
