该项目包含两套独立的前端界面，分别服务于不同的业务场景，采用了截然不同的视觉风格和技术栈。

### 1. QuickLAN 桌面应用 (Tauri + React)
**技术栈**: React, TypeScript, Vite, Tauri 2。
**样式方法**: **纯 CSS (Vanilla CSS)**。未使用 Tailwind CSS、Sass 或任何 UI 组件库（如 Ant Design, MUI）。
**视觉风格**:
- **明亮简洁 (Light & Clean)**: 采用浅色背景 (`#f6f8fb`) 和白色卡片 (`#ffffff`)，搭配深蓝/灰色文本 (`#17202a`, `#31465a`)。
- **圆角与柔和边界**: 广泛使用 `border-radius: 8px` 和淡色边框 (`#dce4ed`)，营造现代、柔和的桌面应用质感。
- **布局策略**: 基于 CSS Grid 和 Flexbox 的手写布局系统。定义了 `.shell`, `.grid.two`, `.panel`, `.stack` 等语义化类名来组织页面结构。
- **交互反馈**: 包含自定义的 Toast 动画 (`@keyframes toast-life`) 和模态框 (`modal-backdrop`)。
- **图标**: 使用 `lucide-react` 作为图标库，保持视觉一致性。

### 2. LAN Mesh 监控仪表盘 (Python Web)
**技术栈**: Python (FastAPI/Flask 推测), HTML5, 原生 JavaScript。
**样式方法**: **单文件内嵌 CSS**。所有样式直接写在 `dashboard.html` 的 `<style>` 标签中。
**视觉风格**:
- **深色科技风 (Dark Tech)**: 采用深灰/黑色背景 (`#0f1117`, `#1a1d27`)，配合高亮强调色 (`#5b8cff` 蓝色, `#4ade80` 绿色)。
- **数据可视化导向**: 设计了专门的资源进度条 (`.progress-bar`)，根据负载高低显示不同颜色（绿/黄/红）。
- **玻璃拟态元素**: 顶栏使用了 `backdrop-filter: blur(8px)` 增加层次感。
- **响应式网格**: 使用 `grid-template-columns: repeat(auto-fill, minmax(380px, 1fr))` 实现主机卡片的自适应排列。

### 开发规范与建议
1. **无 CSS 框架依赖**: 两个前端模块均未引入重型 CSS 框架，维护时需直接修改 CSS 文件或 `<style>` 块。
2. **样式隔离**: QuickLAN 的样式通过 `styles.css` 全局生效；LAN Mesh 的样式仅限于其 HTML 模板。
3. **设计令牌 (Design Tokens)**: 
   - QuickLAN: 硬编码在 `styles.css` 的 `:root` 和各类选择器中。
   - LAN Mesh: 使用 CSS 变量 (`--bg`, `--surface`, `--accent`) 管理主题色，便于后续扩展深色模式或主题切换。
4. **响应式设计**: 两者均通过 `@media` 查询处理移动端或小屏幕适配，但主要以桌面端体验为优先。