# 同任务对照基准

`scripts/task_benchmark.py` 是纯离线记录工具。目录文件必须包含版本、fixture 说明和完整任务；加载时会重算 `input_hash`，并按 `{"acceptance": acceptance, "acceptance_criteria": acceptance_criteria}` 重算验收哈希，篡改立即失败。fixture 只用于流程测试，不代表真实模型成绩或费率。

`template` 生成严格记录模板。`validate` 要求必填字段、非空版本/模型名、参数对象、枚举、非负有限数，并拒绝 bool 充数字、NaN/Infinity、列表 task ID、重复 run ID、缺失或空字段。`verified` 必须同时提供独立引用和 `method`、`verifier`、`verified_at` 元数据；仅文字 `completed` 或导入引用不会自动证明文件内容，验证器必须实际核对证据。

`compare` 只接受已通过同一 `validate` 的记录；无效输入返回非零且不产生成功统计。记录按 system version、task、输入/模型参数/验收哈希分组，fixture 记录与真实记录由 `fixture` 字段隔离。每组报告独立证据完成率、耗时 count/mean/min/max、人工介入比例、费用已知覆盖率、恢复成功率和费用缺失数。费用未知保留 null；同组混合币种标记不可比并拒绝成本比较。

CLI 的 `template`、`validate`、`compare` 遇到结构错误均输出简短错误并返回非零，不泄露 traceback。

统计口径：completion_rate 为全部记录中带导入 verified 证据声明的比例，不是工具自行复验的成功率；引用不会被打开。human_intervention_count 是发生人工介入的运行数，不代表每次介入事件数。恢复率仅以 succeeded/failed 为分母，not_assessed 不计入；分母为空返回 null。费用输出已知小计、覆盖率及完整总计；缺失费用或混合币种时完整总计为 null，comparable 仅表达成本数据是否完整且同币种。输出包含模型参数和两个哈希，跨系统比较前必须相同；fixture 与真实记录永不合并。

CLI 接收单条记录、记录数组或 {"records": [...]}；validate/compare 必须给 --input。加载及直接 compare 均复验目录哈希，拒绝空目录、bool 版本和未知嵌套字段。模板可直接 round-trip 到 validate。
本轮 35 项纯行为测试已通过；尚无 LAN Mesh/DS Bot 在线同模型实测记录，不给出性能或费用胜负。
