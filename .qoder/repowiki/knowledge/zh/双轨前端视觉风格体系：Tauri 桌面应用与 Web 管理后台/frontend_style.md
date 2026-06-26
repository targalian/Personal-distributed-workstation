该仓库包含两套独立的前端界面，分别服务于不同的用户场景，采用了截然不同的视觉风格和技术栈。

### 1. QuickLAN (Tauri 桌面应用)
**技术栈**: React + TypeScript + Vite + Tauri
**样式方案**: 纯 CSS (Vanilla CSS) + BEM 命名变体

*   **视觉风格**: 
    *   **极简商务风**: 采用浅色背景 (`#f6f8fb`) 搭配深蓝/灰文字 (`#17202a`, `#31465a`)，营造干净、专业的工具感。
    *   **卡片化布局**: 大量使用白色背景卡片 (`#ffffff`) 配合细微的边框 (`#dce4ed`) 和圆角 (`8px`)，强调内容区块的独立性。
    *   **状态可视化**: 通过颜色区分状态，如在线为绿色 (`#15935d`)，选中态为蓝色高亮 (`#eaf4ff` / `#2b72b8`)。
    *   **图标系统**: 集成 `lucide-react` 图标库，保持图标风格的一致性（线性、简洁）。

*   **核心文件**: 
    *   `quicklan-main/src/styles.css`: 全局样式定义，包含 CSS 变量、布局类（`.shell`, `.grid`, `.panel`）、组件类（`.tab`, `.device`, `.resource`）及响应式媒体查询。
    *   `quicklan-main/src/App.tsx`: 组件结构，通过 className 绑定样式。

*   **开发约定**:
    *   **类名规范**: 采用语义化的短类名（如 `.shell`, `.pill`, `.dot`），类似 Utility-first 但更偏向于项目特定的组件类。
    *   **布局策略**: 广泛使用 CSS Grid (`display: grid`) 进行复杂布局（如 `.grid.two`, `.form-grid`），Flexbox 用于对齐（如 `.topbar`, `.tabs`）。
    *   **响应式**: 在 `styles.css` 底部定义了 `@media (max-width: 920px)`，将多列网格退化为单列，适配小屏窗口。

### 2. LAN Mesh (Web 管理后台)
**技术栈**: 原生 HTML/CSS/JS (Single File Component 模式)
**样式方案**: 内联 CSS (Dark Mode)

*   **视觉风格**: 
    *   **深色科技风**: 采用深灰/蓝黑背景 (`#0f1117`, `#1a1d27`) 搭配高亮强调色（蓝色 `#5b8cff`, 绿色 `#4ade80`），符合开发者工具/监控面板的审美。
    *   **霓虹点缀**: 使用高饱和度的颜色表示状态（在线绿、离线红、忙碌黄），并在卡片顶部使用彩色条带指示角色（Secretary/Accent, Worker/Green）。
    *   **玻璃拟态/层级**: 通过半透明遮罩 (`rgba(0,0,0,.6)`) 和模态框阴影营造深度感。

*   **核心文件**: 
    *   `lan_mesh/web/templates/dashboard.html`: 包含所有 HTML 结构、CSS 样式（`<style>` 标签内）和 JavaScript 逻辑。

*   **开发约定**:
    *   **CSS 变量**: 在 `:root` 中定义了一套完整的 Design Tokens（`--bg`, `--surface`, `--accent`, `--radius` 等），确保主题一致性。
    *   **动画效果**: 定义了简单的 CSS 动画（如 `pulse` 用于 WebSocket 状态点，`toast-life` 用于提示）。
    *   **单文件架构**: 样式、结构和逻辑耦合在同一个 HTML 文件中，便于 Python 后端直接渲染和部署，无需构建步骤。

### 总结与建议
*   **隔离性**: 两套前端系统完全隔离，没有共享样式文件或组件库。
*   **一致性缺失**: 目前缺乏跨应用的设计语言统一。如果未来需要统一品牌感，建议提取公共的 Design Tokens（如主色调、圆角、字体栈）。
*   **维护性**: QuickLAN 的 CSS 组织较为清晰，适合迭代；LAN Mesh 的单文件模式适合快速原型或轻量级管理页，但随着功能增加，样式维护成本会显著上升。