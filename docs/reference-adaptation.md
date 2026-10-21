# DeepSeek / Qwen 参考审查与适配

v5沿用此前源码审查，并扩展算法开发工具。审查涵盖仓库架构、包分组及以下相关执行链源码；不声称逐行读完 DeepSeek 的全部包，也没有把整套聊天应用搬进排程项目。采用 TypeScript 编排 + Python 求解的现有技术栈，适配机制，不引入它们整个 SDK。

参考快照：

- DeepSeek Harness：`571902028a75c64c1d767f4ae5fd099485f0ddf0`，本地 `/Users/mac/Desktop/ab-proj/deepseek-harness`。
- Qwen Live Harness：`b2d17b8706b226ff7792683ecd8f050f18f9752e`，本地 `/Users/mac/Desktop/ab-proj/Qwen-Live-Harness`。

## 与这次失败直接相关的机制

| 参考模块 / 文件 | 原实现思路 | 隆达的落点 | 状态 |
|---|---|---|---|
| DeepSeek `core/agent-loop/src/agent.ts`、`tool-calls.ts` | 工具结果进入下一模型步骤；工具失败不是空白结果 | 构造失败单优先触发修正调用，不等六次 CP | 本次新增 |
| DeepSeek `core/tools/src/index.ts` | 结构化错误归一化；结果是权威事实 | 分开程序语法、构造死路、额度结束、worker 错误；保留完整具体原因 | 本次新增/完善 |
| DeepSeek `core/session/src/repair.ts` | 区分未执行、结果未知与已提交结果；不把中断当无解 | call_id/result 配对，显式恢复补 OUTCOME_UNKNOWN，保留已审计基线 | 本次完善 |
| DeepSeek `guard/repeat-tool-reminder` | 根据规范化工具输入发现重复，保留原结果并提醒 | 按实际放开订单、基线、提示、种子、线程和额度去重；重复转为反馈 | 本次新增；原项目是提醒，我们增加跳过策略 |
| DeepSeek `guard/timeout-policy` | 限额归属工具，取消与超时分类；不能把超时当失败证明 | API、构造、CP、整场分别计时；取消保留未知任务，限制修正链调用数 | 已有能力 + 本次修正链 |
| DeepSeek `compaction/compaction-tool-result-pruner` | 大工具结果先裁剪，原文可追溯 | 旧反馈程序裁剪；最新错误现场单独保留，原始结果保存在任务文件 | 已有 + 本次优先保留失败 |
| DeepSeek `compaction/compaction-basic` | 独立摘要模型，保留近期上下文、目标和未解决问题 | 独立 qwen3.7-plus 摘要；当前原始错误不让摘要覆盖 | 保留/适配 |
| DeepSeek `session/session-persistence-jsonl`、`session-checkpoint-policy` | 事实日志和派生状态分离，持久记录用于恢复 | append-only 事实事件 + 原子检查点；本次修正单与策略谱系可恢复 | 部分机制适配；不是完整 event-sourcing 重建 |
| Qwen `src/realtime/realtime-session.ts` | 工具调用必须有明确输出回执，不能把不确定交付当成功 | 每次工具调用有对应结果；审计成功、失败、未知分别统计 | 本次完善 |
| Qwen `src/memory/updater.ts` | 独立 updater 读取既有记忆和新证据，提出结构化操作 | qwen3.7-plus 提出 add/update/retire，程序验版本及证据后应用 | 保留 |
| Qwen `src/realtime/context-compaction.ts` | 压缩上下文同时保留近期关键事实和未解决事项 | 旧事实摘要、最近事实、当前失败分别提供给主模型 | 保留/适配 |

上述目录均相对各自仓库。没有将参考项目名称写进模型提示来代替实现。

## 没有照搬的部分及原因

- 浏览器、语音、终端 UI、编辑文件等通用代理工具与本排程任务无关；v5生成代码可以实现完整 construct()，但不能获得任意 shell 或修改硬规则的权限。
- DeepSeek 的插件容器、全量会话投影/迁移系统和 OpenTelemetry 后端未接入；本项目仍使用已有 CLI、JSONL、SQLite 和文件检查点。
- 供应商缓存、向量检索、模型训练没有新增；不把缓存命中或相似度排序称为已学习的成功概率。
- 不使用辅助模型宣告可行或最优；合法性仍由原审计和完整模型验算决定。
- 目前没有把任意失败自动归纳为物理规则。保温、温度、车辆等边界只能来自输入规则，不接受模型“放宽约束”的修复。

## 实测揭示的限制

工具反馈机制能保证错误被模型看到、修正被重新执行；不能保证模型一次就写出合法程序或好策略。模型调用时间、修订结果和最终完整校验分别统计。真实模型与固定响应回归分开报告，见 [验收记录](acceptance.md)。

## v5新增的算法开发回路

`src/algorithm_tools.ts` 提供读源码、语法验证、实际执行、读取失败、比较试验。`ModelClient.generate` 可在一次规划/修正回合内连续接收工具请求、返回真实结果、再调用模型，最多6次工具步骤。程序保留原文，裁剪较旧大输出，最新错误不被摘要覆盖。模型可最终提交完整算法，再进入任务队列执行。

完整构造代码运行在 `python/algorithm.py`，设备状态与候选合法性在 `calendar_engine.py`，最终规则和校验仍在冻结模块。这里借鉴的是工具循环、上下文分层和持久反馈，不声称复刻DeepSeek的全插件系统。
