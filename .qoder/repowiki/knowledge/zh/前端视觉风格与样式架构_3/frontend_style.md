该仓库包含两套独立的前端界面，分别服务于不同的子系统，采用了截然不同的技术栈和视觉风格。

### 1. QuickLAN (Tauri + React) - 现代桌面应用风格
**技术栈**：React 18, Vite, Tauri 2, `lucide-react` 图标库。
**样式方案**：纯 CSS (Vanilla CSS)，无预处理器或原子化框架。
- **设计语言**：采用“清爽商务”风格，以白色背景 (`#ffffff`) 和浅灰底色 (`#f6f8fb`) 为主，搭配深青色文字 (`#17202a`) 和品牌蓝 (`#176bc3`)。
- **布局策略**：大量使用 CSS Grid 和 Flexbox 实现响应式面板。通过 `.grid.two` 等类名实现双栏布局，并在小屏幕下自动切换为单栏。
- **组件规范**：
  - **按钮**：定义了 `.primary` (实心蓝) 和 `.secondary` (描边白) 两种主要状态，以及 `.icon-button` 用于工具栏操作。
  - **卡片与面板**：使用 `.panel` 和 `.device` 类提供统一的圆角 (`8px`)、边框 (`#dce4ed`) 和内边距。
  - **反馈**：内置了 `.toast` 动画和 `.modal-backdrop` 遮罩层，提供原生的桌面应用交互体验。
- **文件入口**：`quicklan-main/src/styles.css` 是核心样式文件，`quicklan-main/src/App.tsx` 负责逻辑与视图绑定。

### 2. LAN Mesh Station Director (Python Web) - 极客暗黑风格
**技术栈**：Python FastAPI/Flask (后端), 原生 HTML/CSS/JS (前端)。
**样式方案**：内联 CSS (Single File Component 模式)。
- **设计语言**：采用“深色极客”主题，背景为深黑蓝 (`#0f1117`)，表面色为深灰 (`#1a1d27`)，强调色为亮蓝 (`#5b8cff`) 和荧光绿 (`#4ade80`)。
- **视觉特征**：
  - **状态指示**：广泛使用彩色圆点 (`.dot`, `.status-dot`) 和徽章 (`.badge`) 来展示节点在线状态、评级 (S/A/B/C) 和任务进度。
  - **数据可视化**：内置了资源监控条 (`.pbar`, `.pfill`)，根据负载百分比动态变色（绿/黄/红）。
  - **移动端适配**：在 `dashboard.html` 中通过 `@media` 查询实现了完整的移动端底部导航栏 (`.mobile-nav`) 和全屏模态框，确保在手机端也能管理分布式集群。
- **PWA 支持**：通过 `lan_mesh/web/static/manifest.json` 配置了 PWA 属性，支持离线访问和桌面安装。

### 开发约定
- **QuickLAN**：样式修改应直接在 `styles.css` 中进行，遵循 BEM 命名变体（如 `.tab.active`）。禁止引入 Tailwind 或 Bootstrap 以保持 Tauri 包体积最小化。
- **LAN Mesh**：所有样式集中在 `dashboard.html` 的 `<style>` 标签内。修改时需同时考虑桌面端和移动端的显示效果，特别是表格和网格的横向滚动处理。