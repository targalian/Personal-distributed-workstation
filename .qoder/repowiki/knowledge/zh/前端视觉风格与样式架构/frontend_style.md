该仓库包含两套独立的前端界面，分别服务于不同的子系统，采用了截然不同的技术栈和视觉风格。

### 1. QuickLAN (Tauri + React) - 现代轻量桌面应用
**技术栈**：React 18, TypeScript, Vite, Tauri 2.0。
**样式方案**：**纯 CSS (Vanilla CSS)**。未使用 Tailwind、Sass 或任何 UI 组件库。
- **设计语言**：采用“企业级清爽”风格。主色调为深蓝色 (`#176bc3`)，背景为浅灰白 (`#f6f8fb`)，强调高对比度和清晰的层级。
- **布局策略**：大量使用 `CSS Grid` 和 `Flexbox` 进行响应式布局（如 `.grid.two`, `.shell`）。
- **组件化样式**：通过语义化类名（如 `.tab`, `.pill`, `.dropzone`, `.modal-backdrop`）定义通用 UI 模式。按钮分为 `.primary`（实心蓝）和 `.secondary`（描边白）。
- **图标系统**：集成 `lucide-react` 提供统一的线性图标风格。
- **交互反馈**：定义了简单的 Toast 动画 (`@keyframes toast-life`) 和模态框遮罩层。

### 2. LAN Mesh Station Director (Python Web) - 极客暗黑监控面板
**技术栈**：Python (FastAPI/Flask), 原生 HTML/CSS/JS。
**样式方案**：**内联 CSS (Dark Theme)**。所有样式集中在 `dashboard.html` 的 `<style>` 标签中。
- **设计语言**：采用“赛博朋克/开发者工具”暗黑风格。背景为深灰黑 (`#0f1117`)，表面色为 `#1a1d27`，强调色为亮蓝 (`#5b8cff`) 和状态绿 (`#4ade80`)。
- **CSS 变量**：使用 `:root` 定义了一套完整的设计令牌（Design Tokens），包括 `--bg`, `--surface`, `--border`, `--accent` 等，便于统一维护主题。
- **可视化元素**：
  - **进度条**：根据负载动态变色（低负载绿色，中负载黄色，高负载红色）。
  - **状态徽章**：使用不同颜色的 `.badge` 区分主机角色（Secretary/Worker）和状态（Online/Offline）。
  - **卡片布局**：`.card` 带有顶部彩色边框指示状态，悬停时有轻微上浮效果。
- **响应式策略**：针对移动端（`max-width: 640px`）进行了深度优化，将顶部 Tab 栏隐藏并替换为底部固定导航栏（`.mobile-nav`），表格支持横向滚动，模态框在移动端全屏显示。

### 开发约定
- **QuickLAN**：遵循 BEM 类似的命名规范，样式与 React 组件紧密耦合在 `styles.css` 中。禁止随意修改全局重置样式。
- **LAN Mesh**：由于是单文件应用，样式修改需直接在 HTML 模板中进行。移动端适配优先保证核心数据（如主机状态、任务进度）的可读性。