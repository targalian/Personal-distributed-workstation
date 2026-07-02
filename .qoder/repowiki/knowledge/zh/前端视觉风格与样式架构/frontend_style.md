该仓库包含两套独立的前端界面，分别服务于不同的子系统，采用了截然不同的视觉风格和技术栈。

### 1. QuickLAN (Tauri + React)
**技术栈**: React + TypeScript + Vite + Tauri。
**样式方案**: 纯 CSS (Vanilla CSS)。
- **设计语言**: 采用“现代商务/工具类”风格。主色调为深蓝 (`#176bc3`) 搭配浅灰背景 (`#f6f8fb`)。
- **布局策略**: 大量使用 `display: grid` 和 `flexbox` 进行响应式布局。定义了 `.shell`, `.grid.two`, `.panel` 等语义化容器类。
- **组件风格**: 
  - **卡片与面板**: 白色背景 (`#ffffff`)，细边框 (`#dce4ed`)，圆角 `8px`。
  - **按钮**: 区分 `.primary` (实心蓝) 和 `.secondary` (描边白)。
  - **图标**: 使用 `lucide-react` 库提供统一的线性图标。
- **响应式**: 通过 `@media (max-width: 920px)` 实现双栏变单栏的自适应。

### 2. LAN Mesh Station Director (Python Backend + HTML Template)
**技术栈**: Python (FastAPI/Flask) + 原生 HTML/CSS/JS。
**样式方案**: 内联 CSS (Single-file styling)。
- **设计语言**: 采用“深色极客/监控大屏”风格。背景为深黑蓝 (`#0f1117`)，表面色为 `#1a1d27`。
- **色彩体系**: 
  - **状态色**: 绿色 (`#4ade80`) 表示在线/成功，红色 (`#f87171`) 表示离线/失败，黄色 (`#fbbf24`) 表示忙碌/警告。
  - **强调色**: 亮蓝 (`#5b8cff`) 用于高亮和链接。
- **视觉元素**:
  - **进度条**: 带有颜色渐变 (`.pfill.low/mid/high`) 的动态进度指示器。
  - **徽章 (Badges)**: 半透明背景的标签，用于显示角色 (Secretary/Station) 和状态。
  - **动画**: 包含 WebSocket 连接状态的脉冲动画 (`@keyframes pulse`) 和 Toast 通知的滑入效果。
- **移动端适配**: 针对小屏幕提供了底部导航栏 (`.mobile-nav`) 和全屏模态框，优化了触摸体验。

### 开发约定
- **QuickLAN**: 样式集中在 `src/styles.css`，通过类名组合控制外观，避免使用 CSS-in-JS。
- **LAN Mesh**: 样式直接嵌入在 `dashboard.html` 的 `<style>` 标签中，便于作为单文件分发，但维护复杂度较高。
- **一致性**: 两个系统都使用了 `Inter` 或系统默认字体栈以确保跨平台清晰度。