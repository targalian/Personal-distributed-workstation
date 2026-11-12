# YomkGraph 本机服务图

YomkGraph 0.1.0 随包放在 `lan_mesh/_vendor/yomk_graph/`，来源、许可证和本地资源路径适配见其中的 UPSTREAM.md。无 CDN、独立安装或个人技能目录运行依赖。

```yaml
observability:
  yomk_graph_enabled: true
  yomk_graph_port: 18765
```

正常启动 `python main.py station` 或 `python main.py worker`，浏览器访问 http://127.0.0.1:18765。默认关闭，不启动监听。仅监听 loopback IPv4；远程可使用 `ssh -L 18765:127.0.0.1:18765 user@host`。同机多应用需配置不同端口。端口占用会使应用启动失败并走原有 stop 清理路径。

图对应当前 Station/Worker 主 ServiceRuntime 的真实注册服务和函数，包括未调用函数和框架基础服务。每次注册完成后缓存 YomkServerInfo 元信息；HTTP 读取缓存，不执行服务请求。主实例服务按明确名单分为任务与 PM、对话与项目、资源与能力、工作站与同步；未配置名称保留“未分组服务”。组件各自持有的独立 ServiceRuntime 与远端节点不自动合并，此范围也在快照 warnings 中展示。

ServiceRuntime._invocation 显式 span 覆盖 request_sync 及 request 的整个 await，ContextVar 保持嵌套与线程池来源。计数是完成的请求，耗时含等待，异常和取消计为失败；正常返回中的业务拒绝不自动识别。原生 server.request、直接对象方法及自建线程来源不会自动补全。不记录参数、返回正文、Context 值、凭据或异常文本。最多保留 2000 种观测边，溢出在 telemetry 标示。

运行时创建时启用采集；FastAPI startup 开放查看器；shutdown_application 先停止并排空查看器，再沿原逻辑停止生产者和关闭运行时。未 monkey-patch 框架，采集器寿命由 runtime 持有，退出中的请求仍能完成统计。基础资源 Context 值与跨实例拓扑不在本次范围内。

包数据包含 HTML/JS/CSS、许可证与来源说明。测试使用临时端口、真实随包框架与隔离请求，不启动真实模型或派发外部任务。
