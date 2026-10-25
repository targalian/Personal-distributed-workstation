# 模型资源与密钥边界（iter-108）

ModelRouter 的请求、候选和降级链进入 ModelRoutingService；评分和难度分类保留普通计算函数。
ModelResourceManager 保留资源状态与兼容方法，配置、池选择、记账、上报和预警进入五个服务。
资源与密钥 14 个 HTTP 操作由同步 ResourceApiService / SecretExchangeService 执行，
统一 HTTP 适配器将其放入线程池，避免余额探测、配置 IO 和全网对齐占用事件循环。
HTTP 和 Worker WebSocket 继续共用幂等批量记账函数。

AgentRuntime 和 ModelRouter 可显式注入资源管理器，未指定时使用原进程级兼容默认实例。
配置文件仍是密钥权威源；运行环境注入沿用原语义，未将明文凭据放入 YomkContext。
原 AES-GCM 加密、指纹校验、信任根收敛、热重载与 API 路径保留。
资源对象 repr 不输出 api_key，WS 启动日志去除鉴权 query，服务内省仅返回服务名和操作名。
配置向导读取明文配置的已有管理接口仍依赖原访问控制，没有改变其 API 行为。

执行入口使用 usage_context 保存并恢复线程的 task_id/project_id，成功、取消与失败均恢复，
并恢复当前技能，防止线程池复用造成错误费用归因。服务路由地址是位置参数，业务消息可以
正常包含 url 字段；回归覆盖了上报目标的该冲突。

后台上报、预警线程的统一退出与热重载排空将在生命周期阶段处理。本轮没有据此宣称多节点
重复记账、真实供应商余额或线上模型延迟已完成实机验证。
