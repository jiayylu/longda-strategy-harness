# v8 实现与参考来源

借鉴 Harness² 的做法是“短常驻任务说明＋按需证据读取”“版本化改动”“具体校验错误进入修正”“区分观察与因果证据”。没有把其通用代码修改工具直接开放给模型修改工厂规则。

| 参考点 | 本地参考位置 | 隆达适配 |
|---|---|---|
| 每个阶段建立上下文、校验失败反馈 | harness2/core/recursion.py | 每个 API 步重新采样搜索状态；最终提交做基线新鲜度检查 |
| 轨迹、变更和按需上下文 | harness2/core/context.py、proposer.py | 原始试验保留，证据分页读取，算法变化记录 |
| 经验 helped/hurt/no_effect/confounded | harness2/core/prompts.py | 实际提示来源、基线和未验证归因进入记录与摘要 |
| Qwen 独立 Memory updater | Qwen-Live-Harness 的 memory/updater.ts | 沿用独立辅助模型提议，程序核对证据 ID 与版本后应用 |
| DeepSeek 程序裁剪再模型摘要 | deepseek-harness 的 compaction-basic | 保留近期原始反馈，旧记录压缩；本版补充质量反馈与提示来源 |

第二次实测发现模型重复读取同一默认源码；新增关键源码保留、重复读取检测、最终答复时间保护。已有合法工具结果与最终文字答复分别记账，最终答复超时不丢弃已审计结果。

Harness² 的阶段快照不等于后台实时刷新；隆达需要额外处理“模型还在思考，构造/CP 已更新基线”的情况。本版通过请求/工具/提交边界更新，不能向正在生成的同一 HTTP 流中插入新的生产状态。

主要文件：

- `src/live_state.ts`：输入版本与搜索版本、过时判定、策略概要、算法差异。
- `src/search_policy.ts`：质量反馈、补全资源账本、提示来源。
- `src/client.ts`：实际请求采样、提交前重新确认、请求记录。
- `src/algorithm_tools.ts`：最新状态和证据/API 读取工具。
- `src/cli.ts`：任务分配、修正队列、账本与检查点、输出。
- `src/strategy_cycles.ts`：取消强制 CP 配额和自动额度翻倍。
- `src/auxiliary*.ts`：把质量反馈与归因信息提供给独立摘要/经验模型。
- `tests/v8.test.ts`：动态基线、状态隔离、质量分类、补全配额、提示来源、提交刷新。

全部 Python 推演与物理校验源文件沿用 v7，不通过调整保温上限或运输规则提高分数。搜索层的资源策略不是合法性证明，低分/UNKNOWN 的原始证据均保留。
