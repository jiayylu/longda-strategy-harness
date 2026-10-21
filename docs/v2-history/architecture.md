# v2 文件与职责

| 路径 | 职责 |
|---|---|
| `start.py` | 隐藏读取 API token，然后启动唯一主入口 |
| `src/cli.ts` | 并行任务队列、基线提交、主/辅助模型并发、停止与恢复 |
| `src/contracts.ts` | 输入策略结构、订单守恒检查及配置展开 |
| `src/prompts.ts` | 主模型生成策略/程序；辅助模型事实压缩与经验更新协议 |
| `src/client.ts` | Anthropic 兼容流式接口；截断/错误检测；不存隐藏思考 |
| `src/learning.ts` | 通用订单分组与自适应资源分配；保留探索机会 |
| `src/memory.ts` | SQLite 试验事实、经验卡片版本、使用/效果统计 |
| `src/auxiliary.ts` | Flash 摘要、证据别名映射、更新校验及非阻塞降级 |
| `src/checkpoint.ts` | 原子检查点、输入/代码一致性、运行锁 |
| `src/worker.ts` | 独立 Python 子进程、任务日志、超时/取消 |
| `src/events.ts` | 先落盘再显示的事实事件；续跑事件序号连续 |
| `src/provenance.ts` | 本次源代码摘要 |
| `python/worker.py` | 构造/真 CP 搜索/完整交付校验入口；所有模式统一基线约束 |
| `python/constructor.py` | 统一单双车的扩展构造：滚动投料、分别接送、有限回退 |
| `python/legacy_constructor.py` | 原贪心构造对照，恢复失败时的部分提示 |
| `python/program.py` | 大模型生成的纯决策程序校验与指令限额 |
| `python/verification.py` | 从实际排程推导辅助变量，对原 CP 模型逐项验算 |
| `python/diagnostics.py` | 排程指标与可反馈的诊断 |
| `reference/` | 冻结的原始物理模型、审计器和 HTML 输出依赖，哈希核对 |
| `python/benchmark_extensions.py` | 多订单、单双车、逐项开启构造能力的有限消融 |
| `tests/`、`python/test_*.py` | 两端回归测试 |
| `docs/v1-history/` | 旧版历史文档与实验，不是 v2 测试结果 |

## 权限边界

主模型只能给出策略对象和纯 `rank` 函数。它读不到历史答案文件，也不能直接改规则。候选来自构造引擎，构造结果必须通过原物理审计和原 CP 模型验算。CP-SAT 的真实搜索与验算区分记录。Flash 只能提出经验卡片更新；证据、版本和统计由程序校验。

## 并发与暂停

构造进程、多个 CP 进程、主模型请求、辅助模型请求彼此独立。主进程串行提交已完成结果，与最新基线再次比较，避免并发重复奖励或覆盖更优结果。

暂停时保存当前运行的基线、队列、在途任务、已尝试配置和资源分配统计。中断的子任务恢复后重跑；CP-SAT 内部搜索树不序列化。读取旧排程答案不是 Memory 经验复用，当前运行恢复的基线则属于正常检查点。
