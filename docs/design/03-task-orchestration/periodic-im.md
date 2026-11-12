# 持久周期任务与统一IM入口（iter-120）

## 日历与运行语义

周期计划属于一个已存在的对话，其project_id从服务器会话元数据获取，不能由任务模板覆盖。支持daily（IANA timezone与hour/minute）和interval（60秒至366天，UTC固定间隔）。UTC保存next_run，界面按计划时区显示。

每天的当地时间落入春季夏令时空档时跳过当天，秋季重复小时只取fold=0一次。interval的anchor是间隔起点，第一个触发为anchor+seconds，不随夏令时改变间隔。`catchup=skip`在距原定时刻超过60秒时跳过漏跑；`once`仅补跑最近一次，不重放整个停机积压。`concurrency=forbid`在上一触发仍待提交、待核对或关联任务未终结时跳过本次；allow允许不同触发并行。暂停计划不取消已创建任务；恢复保留next_run并按补跑策略处理。

SQLite schema17新增periodic_schedules、periodic_runs、periodic_submissions和im相关表。BEGIN IMMEDIATE保护抢占next_run与触发账本，schedule_id+due_at唯一标识一次触发。每次触发派生固定task_id，首次插入不覆盖旧任务；预算、选主机、本地/远程PM仍由既有TaskSchedulingService执行。

提交前预算等失败可以退避重试。已经进入远端派发但进程退出/响应丢失时，账本保留review_required；既有普通队列也不能绕过该记录自动重发。操作者需核对实际PM/副作用，再通过任务恢复流程处理。不宣称跨数据库与外部动作的严格exactly-once；保证同一触发不会因重试创建第二个任务。

PeriodicService由Station拥有，在Secretary激活、旧任务恢复后启动；失去Secretary角色时停止产生新任务。应用停止信号或Yomk deinit终止轮询。一个本地账本可防并发抢占，不把多个独立Secretary数据库当作共识系统；计划未纳入自动跨站主备迁移。

## API与界面

- GET/POST `/api/schedules?conv_id=...`：列出/创建计划。
- PUT `/api/schedules/{id}?conv_id=...`：启停，或target_conv_id迁移到同项目对话；待提交/待核对记录未处理时拒绝迁移。
- GET `/api/schedules/{id}/runs?conv_id=...`：最近100个触发及task_id。
- Web“周期计划”与React“计划”共用页面，支持计划创建、启停、触发历史、IM绑定与消息处理记录；移动导航包含计划和协作。

沿用现有role中间件：viewer只能读；operator可操作周期任务与聊天；IM房间绑定位于`/api/station/`，仅boss可写。权限体系仍是既有角色内共享项目模型，没有额外实现私有用户项目ACL。

## IM接入与去重

POST `/api/station/im-bindings?conv_id=...`配置channel/room_id/enabled。POST `/api/im/{channel}/messages`接收规范化`event_id/room_id/message`，可指定kind=decision及pm_id；请求仍需本项目API/mesh/user认证。未绑定、停用或绑定项目发生变化的房间被拒绝。服务器从绑定解析conv/project，忽略消息中的伪造作用域。普通消息复用ChatRequest→ConversationService→ChatHandler的原有意图/授权/任务逻辑；决策只可投递给绑定对话的PM。

`channel,event_id`为收件唯一键，并保存内容与对话/项目指纹；相同事件重试返回原结果，不再次调用聊天或创建任务。发生不确定异常或进程中断后不盲目重放动作，返回待核对。历史按原conv/project读取，不随房间重新绑定泄漏到另一个项目。GET `/api/im/events?conv_id=...`返回最近事件与回复状态。

企业微信、飞书及自定义通道通过**已认证桥接**使用统一规范化接口；这不是直接暴露给公网的厂商原生回调。厂商签名校验、加解密和原始事件格式转换由桥接端完成，本轮没有宣称安装即接通这些厂商账号。

Telegram原生轮询仅接收配置chat_id，陌生聊天在执行和回复前被拒绝。配置房间首次接入创建独立对话并绑定；不会混入Web当前活跃对话。新消息和Inline决策接入同一持久收件账本。游标仅在持久接纳/明确拒绝后推进；重启重复getUpdates仍去重。轮询HTTP超时10秒。

Telegram回复按event与分段编号入持久outbox。明确429或API ok=false可以重试，成功回复不重发；HTTP结果不明/连接中断/崩溃后的sending保留待核对，避免丢确认后自动重复发信。回复重试不会重复执行原任务。本次验证只用模拟Telegram响应，没有向真实用户发送消息。

## 验证

专项覆盖纽约夏令时空档/重复小时、上海时区、无效计划、停机补跑、并发触发、重复提交与数据库重开、派发中断保护、预算恢复重试、归属迁移、IM并发去重、角色权限、房间迁移历史隔离、陌生Telegram拒绝、回复429重试不重复任务。真实Chrome检查Web/React入口、计划时区/补跑保存、暂停刷新恢复、IM房间绑定/事件重复以及390px移动布局。物理双节点故障切换、厂商线上账号和真实模型仍属于独立环境验收。

实测收尾：19项新增专项、全量820 passed/1 skipped（143.60s）；Chrome五组界面行为、TS/Vite、wheel静态资产验证通过。门禁0 Blocker/21 Warning。未部署或发送外部消息。
