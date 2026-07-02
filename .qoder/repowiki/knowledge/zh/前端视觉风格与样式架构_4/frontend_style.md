该项目包含两套独立的前端界面，分别服务于不同的子系统，采用了截然不同的技术栈和视觉风格。

### 1. QuickLAN (Tauri + React) - 现代化桌面应用风格
**技术栈**：React 18, TypeScript, Vite, Tauri 2.0。
**样式方案**：原生 CSS (Vanilla CSS)，无预处理器或 CSS-in-JS。
- **设计语言**：采用“清爽商务”风格，以白色背景 (`#ffffff`) 和浅灰蓝边框 (`#dce4ed`) 为主，强调高对比度和清晰的层级。
- **色彩体系**：主色调为深蓝色 (`#176bc3`)，辅助色包括成功绿 (`#15935d`)、警告红 (`#a8241a`) 和中性灰 (`#4d667e`)。
- **布局策略**：大量使用 CSS Grid 和 Flexbox。定义了 `.shell`, `.grid.two`, `.panel` 等语义化容器类，实现了响应式的双栏/单栏切换（断点 `920px`）。
- **组件规范**：
  - **按钮**：分为 `.primary` (实心蓝底白字) 和 `.secondary` (白底灰边)。
  - **卡片**：`.device`, `.resource` 等卡片具有统一的圆角 (`8px`) 和微弱的阴影/边框效果。
  - **图标**：集成 `lucide-react` 图标库，保持视觉一致性。
- **文件组织**：所有样式集中在 `quicklan-main/src/styles.css`，通过类名在 `App.tsx` 中引用。

### 2. LAN Mesh Station Director - 极客暗黑监控风格
**技术栈**：原生 HTML5, Vanilla JavaScript。
**样式方案**：内联 `<style>` 标签，CSS 变量驱动。
- **设计语言**：典型的“开发者仪表盘”暗黑模式。背景为深灰黑 (`#0f1117`)，表面层为稍亮的灰色 (`#1a1d27`)。
- **色彩体系**：
  - **Accent**：亮蓝色 (`#5b8cff`) 用于高亮和交互。
  - **状态色**：绿色 (`#4ade80`) 表示在线/成功，黄色 (`#fbbf24`) 表示忙碌/警告，红色 (`#f87171`) 表示离线/失败。
- **视觉元素**：
  - **进度条**：`.pbar` 配合 `.pfill` 实现资源占用可视化，根据数值动态变色（低/中/高）。
  - **徽章**：`.badge` 用于显示主机评级（S/A/B/C/D）和任务状态。
  - **动画**：包含 WebSocket 连接状态的脉冲动画 (`@keyframes pulse`) 和 Toast 提示的淡入淡出。
- **布局策略**：基于 Grid 的自适应卡片布局 (`grid-template-columns: repeat(auto-fill, minmax(360px, 1fr))`)，确保在不同屏幕宽度下的可读性。

### 开发约定
1. **QuickLAN**：新增 UI 时应优先复用 `styles.css` 中定义的 `.panel`, `.stack`, `.toolbar` 等原子布局类，避免硬编码 margin/padding。
2. **LAN Mesh**：修改样式需直接编辑 `dashboard.html` 头部的 `<style>` 块，注意保持 CSS 变量 (`:root`) 的统一性以支持主题微调。
3. **响应式**：QuickLAN 已内置移动端适配逻辑；LAN Mesh 通过媒体查询 (`max-width: 640px`) 处理小屏堆叠。