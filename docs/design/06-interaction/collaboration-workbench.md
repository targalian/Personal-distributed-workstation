# 协作工作台与人工接管（iter-119）

现有仪表盘“协作工作台”和React“协作”共用同一页面及MeshTransport认证。按任务查看PM、团队成员、执行主机、DAG等待关系、澄清问题、模型自评及可分页读取的来源。L1/L2链接定位已有对话与PM线程。无新信息时仅刷新当前视图，同一Toast五秒内合并；接管状态转换的通知在SQLite事务中去重。

## 任务接管

`GET /api/collaboration/{task_id}`汇总视图；POST追加`takeover`、`release`、`reply`。沿用Secretary启用检查及现有boss/operator/viewer中间件。没有新增按用户私有项目ACL；本项目既有角色内可见范围继续适用。

接管先持久化`pausing`，调用原PM暂停链，取消运行时并向已派发子Agent传播取消。派发入口串行栅栏防止暂停扫描后再排入任务；持久投递将paused PM的未发任务判为过时；晚到完成/交付不覆盖暂停。Runtime按真正进入execute的次数统计在途执行。只有本机Runtime无在途执行、PM线程退出、每个已登记远端Agent均明确回复quiescent，才变为`human`。旧Worker、断网、丢回复、在途CLI/工具返回慢都保持暂停未确认，可重复确认。不会用HTTP 200或“已请求取消”冒充退出。

暂停不回滚已发生的文件、网络与外部系统副作用，不能证明用户代码自行创建的脱离父进程后台程序已退出。恢复前应核对这些副作用。`release`仅结束接管，不自动恢复任务；现有恢复入口及PM本机恢复服务拒绝接管仍未结束的任务。SQLite schema16只新增`collaboration_takeovers/notices`，未改变旧表主键。人工接管状态跨重启保留。

## 独立浏览器能力

可选安装`pip install 'lan-mesh[browser]'`并运行`python -m playwright install chromium`。未安装时创建会话返回明确503；启动普通Station不要求此依赖。

`POST /api/browser/sessions`接收当前主机已有task_id及可选allowed_origins。每任务独立非持久Chromium context；每主机最多8会话。同任务重试创建复用已有会话。返回host_id/session_id，访问必须匹配task/project及所在主机。Session不迁移到其他主机，不复用用户浏览器登录状态。

`POST /api/browser/sessions/{session_id}/{actions|takeover|release|close}`沿用认证；请求含task_id。浏览器默认自动控制，用户需先接管才能手动导航、点击画面、输入和回车。自动工具只支持snapshot/navigate/click/fill；没有任意JS执行、文件上传、下载或OS桌面控制。HTTP(S)来源需要用户指定或手动导航授权，跳转及子资源同样检查来源。工具描述、网页内容不授予新增来源权限。

浏览器的动作、创建及控制权切换由所属Yomk组件的一条专用线程顺序处理。人工接管响应返回前，先前已接纳的动作已完成；之后的自动动作均返回409。交回控制才允许自动继续。任务接管和浏览器接管分别显示，不能把暂停PM误当浏览器已转手。

PM本机及本机子Agent绑定所属Station；ReAct下一次执行时仅对明确为同一task/project创建的会话注册browser_action。可显式传入_browser_session_id，但仍需同任务校验。分布式任务需调度到会话所属主机；不会自动转发浏览器登录状态。关闭应用通过Yomk deinit关闭context/浏览器/线程；重启后旧session_id无效，必须用户重新创建。

## 验证边界

专项包含执行退出栅栏、暂停晚到状态、远端未确认、持久化、通知并发去重、浏览器跨任务拒绝、来源限制、手动控制阻断排队自动动作及HTTP动作映射。真实Chrome隔离HTTP实测覆盖Web/React、390px移动端、刷新恢复、浏览器接管/手动导航/交回后的自动点击。未连接线上节点或用户已登录浏览器，也未宣称物理多机暂停实测完成。

最终验证：10项新增专项；全量801 passed/1 skipped/130.20s，编译导入、名称扫描、239条目docs门禁通过，门禁0 Blocker/20既有Warning。真实Chrome6组场景通过；截图与报告在本机/tmp/lan-mesh-collaboration-qa/。
