# 文件职责与参考来源

| 层 | 文件 | 做什么 |
|---|---|---|
| 启动 | start.py | 隐藏读取凭据，启动唯一入口 |
| 编排 | src/cli.ts | 构造/CP/主模型/两个辅助角色并发、合法基线提交、资源调度、停止恢复 |
| 订单与策略 | src/contracts.ts | 严格策略结构、订单数量守恒、当前订单配置展开 |
| 当前上下文 | src/context.ts | 状态差异、多候选适配说明、完全匹配资格；主模型输入剔除冗余历史 |
| 主模型 | src/prompts.ts、client.ts | 选/组合策略或生成受限 rank 程序；流式接口、截断检测及真实调用计时 |
| Memory | src/memory.ts | 原始事实、策略检索、使用和迁移关联、版本化经验卡片 |
| 反馈裁剪 | src/auxiliary.ts | 纯程序裁剪和结构化近期事实；不调用模型、不修改 Memory |
| 两个辅助角色 | src/auxiliary_roles.ts | 独立摘要与独立 updater，不同提示词/协议；证据映射与失败回退 |
| 搜索分配 | src/learning.ts | 当前订单通用构造、不同搜索方向收益/成本统计与探索保留 |
| 断点与日志 | src/checkpoint.ts、events.ts | 原子保存、输入/代码一致性、运行锁、事件先落盘后显示 |
| 进程边界 | src/worker.ts | 有限额度 Python 子进程、取消、日志、凭据不传给求解子进程 |
| 执行 | python/worker.py | 构造与真实 CP Solve；三类校验；完整结果输出 |
| 物理适配 | python/physical_model.py | 调用冻结模型；WIP 时保留物料真实身份，禁用不适用的冷启动身份对称化 |
| 在制状态 | python/production_state.py | 验证观察、追加事实约束、状态特征、独立状态审计、测试快照 |
| 状态工具 | python/state_cli.py | describe/snapshot 输入检查及转换 |
| 当前日目标 | python/scoring.py | 统计当前日的区间重叠；同分保温偏好；冷启动目标保持一致 |
| 构造 | python/constructor.py、legacy_constructor.py | 单双车、批次/滚动投料、分别接送、有限回退；legacy 保留冷启动对照 |
| 生成程序 | python/program.py | 纯 rank 函数的 AST 和计算量限制；候选和状态不可直接修改 |
| 校验与诊断 | python/verification.py、diagnostics.py | 完整原模型逐项验算、聚合失败与绩效；区别于真实 Solve |
| 冻结规则 | reference/、reference-manifest.json | 物理规则及原审计器，调用前后核对文件哈希 |
| 验证 | tests/、python/test_*.py | 订单、Memory、角色边界、状态续排、真实 CP、拒绝非法观察、回归 |
| 场景/对照 | python/prepare_context_cases.py、scripts/context_benchmark.py | 4/8/12/20 单与多种 WIP；隔离经验复用效应 |
| 实际模型测试 | scripts/live_context_check.py | 有限冷启动/经验参考对照，主模型时间计入 |

## Harness 在哪里

Harness 是 `src/cli.ts` 周围的运行控制层：准备当前问题、检索经验、组装上下文、接收模型策略、调用构造/CP 工具、核实结果、记录反馈、更新经验、选择下一次尝试、恢复中断。它不是另一个物理求解器，也不让模型直接宣判合法。

主模型、构造器、CP-SAT 和辅助模型各有单独角色。CP-SAT 内部仍用原完整物理模型；构造器更快地提供候选；两个辅助任务的失败不会阻塞搜索。并发结果提交时重比最新基线，避免过期结果重复奖励。

## 借鉴了什么

| 参考本地项目 | 文件 | 采用的机制 | 本项目适配 |
|---|---|---|---|
| Qwen-Live-Harness | packages/qwen-live-harness/src/memory/updater.ts | 独立 updater 读取记忆和新事实，返回结构化更新，由程序应用 | 用生产上下文、审计事实和收益替换聊天事实；add/update/retire 引用证据及版本 |
| Qwen-Live-Harness | packages/qwen-live-harness/src/memory/config.ts | Memory updater 的独立模型配置 | 默认 qwen3.7-plus；与主模型分开 |
| Qwen-Live-Harness | packages/qwen-live-harness/src/memory/retrieval.ts | 多条检索结果而非只取一条 | 先用设备/工艺/在制结构特征及验证结果排序；没有声称已接入其向量召回 |
| deepseek-harness | packages/compaction/compaction-basic/src/summarizer.ts | 独立摘要模型 | qwen3.7-plus 专门压缩反馈，不承担 Memory 写入 |
| deepseek-harness | packages/compaction/compaction-basic/README.md | 先程序裁剪，保留近期内容，再模型压缩 | 最近 N 条反馈保留结构化原值；较旧反馈先削减字段，再异步总结 |

原项目路径分别为 `/Users/mac/Desktop/ab-proj/Qwen-Live-Harness`、`/Users/mac/Desktop/ab-proj/deepseek-harness`，本轮只读取参考，没有修改。采用的是机制和接口边界，没有复制整个聊天产品。

## 权限和可追溯性

经验卡片是条件性建议；主模型必须结合本次订单和在制事实重新判断。原始试验事实不可被 updater 改写；数值统计由程序计算。旧卡片退役保留历史，不删除证据。

在制完整历史留在执行模型；Memory 存摘要特征，主模型读取当前承诺和摘要，历史未来答案不会进入新请求。构造失败、CP UNKNOWN 和全局 INFEASIBLE 分别记录。

`docs/v2-history/` 是原版文档和实验，不能作为 v3 本轮性能证据。
