该项目采用**双轨制（Dual-Track）**前端视觉体系，分别服务于桌面级文件快传应用（QuickLAN）和分布式计算网格管理后台（LAN Mesh）。两者在技术栈、设计语言和交互模式上存在显著差异。

### 1. QuickLAN：Tauri + React 现代极简风格
**技术栈**：React 18, TypeScript, Vite, Tauri 2, `lucide-react` 图标库。
**核心文件**：`quicklan-main/src/styles.css`, `quicklan-main/src/App.tsx`

*   **设计语言**：
    *   **色彩系统**：采用高亮度的“办公/生产力”配色。背景为浅灰蓝 (`#f6f8fb`)，卡片为纯白 (`#ffffff`)，主色调为科技蓝 (`#176bc3`)，辅助色包括成功绿 (`#15935d`) 和警告红 (`#a8241a`)。
    *   **布局策略**：基于 CSS Grid 和 Flexbox 的响应式布局。使用 `.shell` 作为全局容器，`.grid.two` 实现双栏自适应（左窄右宽）。
    *   **组件美学**：
        *   **圆角与阴影**：统一使用 `8px` 或 `7px` 的圆角 (`border-radius`)，配合极细的边框 (`1px solid #dce4ed`) 营造轻量感。
        *   **状态反馈**：通过 `.toast` 动画和 `.modal-backdrop` 遮罩层提供清晰的异步操作反馈。
        *   **拖拽交互**：`.dropzone` 区域采用虚线边框 (`2px dashed`) 和大面积留白，强调文件拖入的交互意图。
    *   **字体排印**：优先使用 `Inter` 和系统默认无衬线字体，强调数字和标签的可读性（如 `.eyebrow` 类用于小写大写标签）。

*   **开发约定**：
    *   **样式隔离**：未使用 CSS Modules 或 Scoped CSS，而是通过语义化类名（如 `.device`, `.resource`, `.transfer`）进行全局命名空间管理。
    *   **图标集成**：深度集成 `lucide-react`，所有图标尺寸统一控制在 `16px` - `17px`，确保视觉密度一致。

### 2. LAN Mesh：原生 HTML/CSS 深色监控风格
**技术栈**：原生 HTML5, CSS3 (Variables), Vanilla JavaScript。
**核心文件**：`lan_mesh/web/templates/dashboard.html`

*   **设计语言**：
    *   **色彩系统**：典型的“深色模式”监控仪表盘风格。背景为深黑蓝 (`#0f1117`)，表面层为稍亮的深蓝灰 (`#1a1d27`)，强调色为荧光蓝 (`#5b8cff`) 和状态绿 (`#4ade80`)。
    *   **数据可视化**：
        *   **进度条**：使用 `.pbar` 和 `.pfill` 类展示 CPU/内存/磁盘占用率，并根据负载动态切换颜色（绿/黄/红）。
        *   **徽章系统**：通过 `.badge` 类区分主机角色（Secretary/Worker）和状态（Idle/Busy/Archived）。
    *   **布局策略**：
        *   **卡片网格**：使用 `grid-template-columns: repeat(auto-fill, minmax(360px, 1fr))` 实现主机卡片的自动换行排列。
        *   **表格视图**：在 Station Director 面板中使用紧凑的 `.fleet-table` 展示主机舰队状态。
    *   **交互细节**：
        *   **悬停效果**：卡片悬停时产生轻微上浮 (`translateY(-2px)`) 和边框高亮，增强点击欲望。
        *   **WebSocket 状态**：顶部导航栏集成脉冲动画的 WebSocket 连接指示灯 (`.ws-dot`)。

*   **开发约定**：
    *   **CSS 变量驱动**：所有颜色、圆角、间距均通过 `:root` 下的 CSS 变量（如 `--bg`, `--surface`, `--accent`）定义，便于后续主题切换。
    *   **单文件交付**：样式、结构和逻辑全部内联在 `dashboard.html` 中，旨在减少依赖，方便通过 Python `http.server` 或内置 API 直接分发。

### 3. 架构对比与总结
| 维度 | QuickLAN (Tauri) | LAN Mesh (Web Dashboard) |
| :--- | :--- | :--- |
| **视觉目标** | 亲和力、易用性、现代感 | 专业性、信息密度、实时监控 |
| **背景基调** | 浅色明亮 (`#f6f8fb`) | 深色沉浸 (`#0f1117`) |
| **核心技术** | React + CSS (BEM-like) | Vanilla JS + CSS Variables |
| **响应式** | 媒体查询断点 `920px` | 媒体查询断点 `640px` |
| **图标方案** | `lucide-react` (SVG) | Unicode Emoji / 简单 SVG |

**开发者建议**：
1.  **QuickLAN 扩展**：新增组件时应复用 `.panel`, `.primary`, `.secondary` 等基础类，保持 `8px` 圆角和 `1px` 边框的一致性。
2.  **LAN Mesh 维护**：修改配色时仅需调整 `:root` 中的 CSS 变量，避免硬编码颜色值。
3.  **跨端一致性**：虽然两者风格迥异，但都遵循了“状态色语义化”原则（绿=在线/成功，红=离线/错误，蓝=主操作），在跨模块开发时应保持这一认知习惯。