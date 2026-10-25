# 前端通信与源模块（iter-111）

后端服务化继续使用原有 HTTP/WS 契约。仪表盘脚本拆为 auth、shell、主机任务、
Agent/工具、资源、项目、聊天、会话、PM/团队、工作台、Station、技能/Bot、P2P、DAG、
运行时和 bootstrap 十六个源文件。`dashboard_assets.load_dashboard_script` 按固定顺序组装，
经 `/static/dashboard.js` 返回为一个 classic script；保留原来的全局函数、函数提升和
inline click handler。DOM、CSS 与非认证业务源码的 SHA-256 契约不变。

认证与 raw fetch 由 `mesh_transport.js` 单一实现。仪表盘保持 Response 返回接口；React
保留 `apiFetch<T>` 的 JSON/错误适配。两端共享并发 bootstrap Promise、个人 token 优先、
登录世代隔离与失败重试；Headers/AbortSignal 透传，FormData 不被强加 JSON Content-Type。
React 的认证、类型、优化业务分别拥有模块，原 api.ts 只做兼容导出。QuickLAN 的 Tauri API
按设备、传输、设置、共享四域拆分，27 个 native command 与参数载荷保持原样。

仪表盘源拆分后，PWA 壳缓存升级至 v2，预缓存 transport 与组装脚本，API 仍 network-only。
React `/spa` 构建路径和 hash 路由保留。API 版本由后端发布版本提供；QuickLAN 自身版本
属于独立子项目，未冒用 LAN Mesh 的版本号。

验证：6 项 Node 行为/IPC 契约；React 类型检查与生产构建、QuickLAN 类型检查与构建；
Chromium 桌面、390px 手机、React 任务和 DAG 四页均通过，37 个请求携带正确认证，
page_errors=[]，手机无横向溢出。截图位于 `temp_resault/phase8_ui/`。
测试使用隔离本机服务与固定数据，没有向实际 Bot、模型或其他用户发送消息。
本轮未修改 Rust，也未重建 Tauri 原生安装器；QuickLAN 验证范围是 TS/IPC 契约和前端产物。
