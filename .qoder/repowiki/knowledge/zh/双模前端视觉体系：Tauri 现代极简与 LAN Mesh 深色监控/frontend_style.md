该项目包含两套独立的前端应用，分别服务于不同的使用场景，采用了截然不同的视觉风格和技术栈。

### 1. QuickLAN 桌面客户端 (Tauri + React)
**技术栈**：React, TypeScript, Vite, Tauri, `lucide-react` (图标库)。
**样式方案**：原生 CSS (Vanilla CSS)，无预处理器或原子化框架。

*   **视觉风格**：
    *   **极简商务风**：采用浅色背景 (`#f6f8fb`) 搭配深蓝/深灰文字 (`#17202a`)，营造出干净、专业的桌面应用质感。
    *   **卡片式布局**：大量使用白色背景 (`#ffffff`) 配合细微的边框 (`#dce4ed`) 和圆角 (`8px`) 来划分功能区域（如设备列表、共享资源）。
    *   **状态可视化**：通过颜色区分状态，如在线为绿色 (`#15935d`)，选中态为浅蓝背景 (`#eaf4ff`)，错误提示为淡红背景 (`#fff1f0`)。
    *   **响应式设计**：通过 `@media (max-width: 920px)` 实现简单的流式布局切换，确保在窄窗口下的可用性。

*   **核心文件**：
    *   `quicklan-main/src/styles.css`：定义了全局变量、布局网格 (`.grid`, `.shell`)、组件样式 (`.tab`, `.device`, `.resource`) 及动画 (`.toast`)。
    *   `quicklan-main/src/App.tsx`：基于 React 函数组件构建，通过 `className` 映射 CSS 类名，利用 `lucide-react` 提供统一的线性图标风格。

### 2. LAN Mesh 分布式监控面板 (Python Web)
**技术栈**：Python (FastAPI/Flask), HTML5, 原生 JavaScript。
**样式方案**：内联 CSS (Single-file Style)，深色主题 (Dark Mode)。

*   **视觉风格**：
    *   **极客监控风**：采用深色背景 (`#0f1117`) 搭配高对比度文字 (`#e4e7ef`)，适合长时间运行的监控场景。
    *   **霓虹点缀**：使用高饱和度的强调色，如蓝色 (`#5b8cff`) 代表 Secretary 节点，绿色 (`#4ade80`) 代表在线/空闲，红色 (`#f87171`) 代表离线/失败。
    *   **数据密度优先**：布局紧凑，使用网格系统 (`.grid`) 展示大量主机、任务和 Agent 信息，强调信息的直观获取而非装饰性。
    *   **动态反馈**：通过 CSS 动画 (`@keyframes pulse`) 模拟 WebSocket 连接状态的呼吸灯效果。

*   **核心文件**：
    *   `lan_mesh/web/templates/dashboard.html`：单文件全栈模板，包含了所有的 HTML 结构、CSS 样式定义和前端交互逻辑。

### 开发规范与建议
1.  **QuickLAN 样式维护**：由于未使用 CSS Modules 或 Scoped CSS，建议在 `styles.css` 中保持类名的语义化（如 `.device-list` 而非 `.list`），避免全局污染。新增组件时应复用现有的 `.panel`, `.primary`, `.secondary` 等基础类。
2.  **LAN Mesh 面板扩展**：目前为单体 HTML 文件，若需增加复杂交互，建议将 JS 逻辑抽离至 `lan_mesh/web/static/` 目录下的独立 `.js` 文件，并保持 CSS 变量 (`:root`) 的一致性以方便主题切换。
3.  **图标一致性**：QuickLAN 统一使用 `lucide-react`，新增功能时应从该库选取风格匹配的图标，保持视觉语言的统一。