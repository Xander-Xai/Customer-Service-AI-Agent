import { defineConfig } from 'vite';

// 自定义插件：移除 Vite 8 自动注入的 `<link crossorigin>` 属性
// 因为 FastAPI StaticFiles 挂载不返回 Access-Control-Allow-Origin 头
// 导致浏览器以 CORS 模式加载 CSS 失败
function removeLinkCrossorigin() {
  return {
    name: 'remove-link-crossorigin',
    transformIndexHtml(html) {
      return html.replace(
        /<link\s([^>]*?)crossorigin(?:="[^"]*")?\s*([^>]*?)>/gi,
        (_match, before, after) => {
          const attrs = (before + ' ' + after).replace(/\s+/g, ' ').trim();
          return `<link ${attrs}>`;
        }
      );
    },
  };
}

export default defineConfig({
  root: 'web',
  base: '/',
  plugins: [removeLinkCrossorigin()],
  build: {
    outDir: 'static/dist',
    emptyOutDir: true,
    rollupOptions: {
      input: {
        main: 'index.html',
        login: 'login.html',
        admin: 'admin.html',
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      '/ws': {
        target: 'ws://localhost:8000',
        ws: true,
      },
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    exclude: [
      '**/node_modules/**',
      '**/dist/**',
      '**/.{idea,git,cache,output,temp}/**',
      '**/__e2e__/**',
    ],
  },
});
