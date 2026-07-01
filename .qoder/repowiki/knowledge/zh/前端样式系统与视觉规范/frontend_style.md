该仓库包含两个独立的前端界面，分别采用不同的技术栈和视觉风格：

### 1. QuickLAN (Tauri + React)
- **技术栈**：React 18, TypeScript, Vite, Tauri 2.0。
- **样式方案**：纯 CSS (Vanilla CSS)。未使用 CSS 框架（如 Tailwind）或 CSS-in-JS。
- **设计语言**：
  - **色调**：浅色主题。背景色 `#f6f8fb`，主文字 `#17202a`，强调色 `#176bc3` (蓝色)。
  - **组件风格**：圆角卡片 (`border-radius: 8px`)，细边框 (`1px solid #dce4ed`)，阴影轻微。
  - **布局**：使用 CSS Grid 和 Flexbox 实现响应式布局。定义了 `.shell`, `.panel`, `.grid` 等语义化类名。
  - **图标**：使用 `lucide-react` 库提供矢量图标。
- **关键文件**：
  - `quicklan-main/src/styles.css`：全局样式定义，包含重置样式、布局类、组件类及媒体查询。
  - `quicklan-main/src/App.tsx`：React 组件结构，通过 className 引用样式。

### 2. LAN Mesh Dashboard (Python Backend + HTML Template)
- **技术栈**：原生 HTML5, CSS3, JavaScript (ES6+)。
- **样式方案**：内联 CSS (Single File Component 风格)。所有样式定义在 `dashboard.html` 的 `<style>` 标签中。
- **设计语言**：
  - **色调**：深色主题 (Dark Mode)。背景 `#0f1117`，表面色 `#1a1d27`，文字 `#e4e7ef`，强调色 `#5b8cff`。
  - **组件风格**：高对比度，霓虹感状态指示灯 (绿/红/黄)，玻璃拟态模态框。
  - **响应式**：包含针对移动端 (`max-width: 640px`) 的详细媒体查询，提供底部导航栏和全屏模态框适配。
  - **交互**：CSS 动画用于 WebSocket 连接状态脉冲 (`.ws-dot`) 和 Toast 提示。
- **关键文件**：
  - `lan_mesh/web/templates/dashboard.html`：包含完整的 UI 结构、样式逻辑和客户端脚本。

### 开发约定
- **QuickLAN**：新增组件时应在 `styles.css` 中定义语义化类名，避免在 JSX 中编写行内样式。保持 `.panel` 和 `.card` 的视觉一致性。
- **LAN Mesh**：修改 UI 需直接编辑 HTML 模板中的 `<style>` 块。注意深色主题下的对比度要求，状态颜色（在线/离线/忙碌）已固化为 CSS 变量。
- **无共享样式系统**：两个前端项目完全隔离，没有共享的设计令牌 (Design Tokens) 或组件库。