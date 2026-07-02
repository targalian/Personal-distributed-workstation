## 系统概述
QuickLAN 的前端基于 **Tauri + React + Vite** 桌面应用，采用 **原生 CSS + BEM 风格类名** 的轻量级样式方案，未引入任何 UI 组件库或 CSS-in-JS 框架。图标统一来自 `lucide-react`，整体视觉风格为浅灰蓝调、圆角卡片式面板布局。

## 关键文件与包
- `quicklan-main/src/styles.css` — 全局样式与设计令牌集中定义处
- `quicklan-main/src/App.tsx` — 主界面逻辑与内联 JSX 结构（所有页面组件均在此文件中）
- `quicklan-main/package.json` — 依赖声明：react 18、lucide-react、@tauri-apps/*
- `quicklan-main/vite.config.ts` — Vite 构建配置，端口 1420，忽略 src-tauri 热重载
- `quicklan-main/index.html` — 入口 HTML

## 架构与约定
### 设计令牌（Design Tokens）
通过 `:root` CSS 变量集中管理主题色与字体：
- 主色：`#176bc3` / 激活态 `#1761a6`
- 背景：`#f6f8fb`，卡片白 `#ffffff`
- 边框/分割线：`#dce4ed` / `#d7e1eb`
- 文本：主文本 `#17202a`，次要 `#65798d`，链接 `#1f5fa8`
- 状态色：成功 `#15935d`，错误 `#a8241a`，警告 `#b42318`
- 字体栈：Inter → Segoe UI → Microsoft YaHei → system-ui

### 布局模式
- 根容器 `.shell` 使用 flex column + gap 16px 作为全局间距系统
- 通用布局类：`.grid.two`（双列栅格）、`.panel`（卡片容器）、`.stack`（纵向堆叠）
- 工具栏类：`.topbar`、`.tabs`、`.toolbar`、`.status-strip` 等统一使用 flex + align-items center

### 组件样式约定
- 按钮族：`.primary`（主操作）、`.secondary`（次操作）、`.icon-button`（图标按钮）、`.compact`（紧凑版）
- 标签徽章：`.badge` + 语义变体 `.completed` / `.failed` / `.transferring`
- 输入控件：统一 `border-radius: 7px`、`height: 34px`、`border: 1px solid #d7e1eb`
- 列表项：`.device`、`.resource`、`.file-row` 等采用 grid 两列布局（内容 + 操作区）
- 弹窗：`.modal-backdrop` + `.modal` 固定定位居中，带阴影遮罩
- 反馈：`.toast` 顶部居中动画提示，`.error` 红色条状错误提示

### 响应式策略
仅通过单一 `@media (max-width: 920px)` 断点，将双列栅格、顶栏、设置行等切换为单列堆叠。

### 图标与交互
- 图标全部来自 `lucide-react`，以 `<Icon size={16} />` 形式内联传入
- 无 hover/focus 动效，禁用态统一通过 `opacity: 0.6; cursor: not-allowed` 表达

## 开发者应遵循的规则
1. **新增样式优先复用现有类名**：如 `.panel`、`.stack`、`.primary`、`.badge` 等，避免重复定义相同外观
2. **颜色必须从 `:root` 取值**：不得在组件中硬编码十六进制色值，保持主题一致性
3. **BEM 命名规范**：类名采用语义化小写连字符（如 `device.selected`、`transfer-head`），不使用 CSS Modules 或 scoped 样式
4. **尺寸与间距**：统一使用 7px/8px 圆角、16px 基础间距、34px 输入高度，新增元素需对齐此规范
5. **图标使用 lucide-react**：禁止引入其他图标库，size 建议 15–17px
6. **响应式只处理 920px 以下**：移动端适配集中在该断点，无需编写多断点媒体查询
7. **JSX 结构与样式分离**：所有样式在 `styles.css` 中定义，组件文件不嵌入 style 对象或 CSS-in-JS