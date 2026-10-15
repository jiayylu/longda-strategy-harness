# 隆达策略 Harness v2

Node.js 22 / TypeScript 负责调度、模型接口、Memory 和断点；Python / OR-Tools 负责构造、完整搜索和校验。旧版本没有覆盖。

## 一条完整流程

```text
当前订单 + 当前规则 + 策略经验（不含历史排程答案）
  ├─ 通用构造 / 检索到的算法 → 原物理审计 + 完整 CP 模型验算 → 可交付基线
  └─ 主模型分析订单 → 分组策略 / 新 rank 程序 → 受限执行与构造
                           ↓
            多个独立 CP-SAT 任务并行搜索、修复与改善
                           ↓
             完整双重校验 → 更新基线 / HTML
                           ↓
       事实记录 → Flash 压缩 / 经验更新建议 → 证据及版本校验
                           ↓
                按实际收益分配下一轮资源
```

主模型和辅助模型调用均不阻塞构造及 CP 搜索。已验证的构造结果可以直接成为基线，不必再等待一次 CP 求解。CP 模型验算会逐项计算原模型的约束；它与真正执行 `Solve()` 分别计时、分别记录。

## 安装与启动

```bash
cd /home/huanzz/longda-strategy-harness-v2-20261015
bash scripts/setup.sh
.venv/bin/python start.py \
  --orders configs/orders20.json --vehicles 2 \
  --out runs/my-cold-run \
  --model qwen3.8-max --effort high --max-tokens 32768 \
  --aux-model qwen-flash \
  --cp-workers 2 --threads 8 --construct-workers 1 \
  --solve-seconds 30 --construct-seconds 3 \
  --wall-seconds 600 --rounds 4 --target 720 \
  --memory read-write --memory-db data/experience-v2.sqlite
```

终端隐藏输入 API token，不写入配置、日志或 Memory。`--vehicles 1` 使用同一入口；规则里没有将单车伪装成双车。

### 参数

| 参数 | 默认 | 含义 |
|---|---:|---|
| `--vehicles` | 订单文件 | 支持 1、2 辆；改变物理输入 |
| `--cp-workers` | 2 | 同时运行几个独立 CP 求解进程 |
| `--threads` | 8 | 每个 CP 求解进程的内部线程数 |
| `--construct-workers` | 1 | 同时运行几个构造进程 |
| `--solve-seconds` | 30 | 单次 CP 尝试额度，包含建模；不是整场预算 |
| `--construct-seconds` | 3 | 单次扩展构造预算；审计另计，外层总时限仍生效 |
| `--wall-seconds` | 600 | 本次调用总墙钟预算，包含模型思考及校验 |
| `--rounds` | 4 | 主模型调用上限 |
| `--max-trials` | 160 | 同一运行累计任务完成数的停止阈值（含续跑）；并行在途结果可能略超 |
| `--target` | 720 | 首日锻打目标分钟数，完整审计达标后停止 |
| `--preference` | none | `none` 或 `hold-near-min`；先提高首日锻打，再比较额外保温 |
| `--allocation` | adaptive | 按近期改善/预留线程秒及探索机会分配；`round-robin` 作对照 |
| `--bootstrap-limit` | 40 | 通用策略初始数量；每次基于输入重新计算 |
| `--model` | qwen3.8-max | `off` 可禁用主模型 |
| `--aux-model` | qwen-flash | `off` 可禁用辅助模型；默认不启用思考 |
| `--aux-timeout` | 30 | 单次辅助调用上限，失败使用程序事实摘要 |
| `--memory` | read-write | `off` / `read-only` / `read-write` |
| `--resume` | false | 从同一输出目录显式续跑 |
| `--program-file` | 无 | 加入一份纯 `rank(candidates,state)` 程序用于实验 |

总 CPU 申请为 `cp-workers × threads + construct-workers`。程序拒绝超过主机可用 CPU 的配置。并行任务不会相互覆盖基线：提交结果时重新与最新基线比较。

### 断点续跑

```bash
.venv/bin/python start.py --out runs/my-cold-run --resume \
  --orders configs/orders20.json --vehicles 2 \
  --cp-workers 2 --threads 8 --wall-seconds 600
```

需保持订单、规则、代码和目标偏好一致。可调整资源和这次调用的预算。恢复时重新校验当前运行保存的基线，恢复任务队列、已尝试策略及分配统计。被打断的 CP 任务重新开始求解；**不保存 CP-SAT 内部搜索树**。恢复当前运行的方案不等于从 Memory 加载历史答案。

Ctrl+C 或在输出目录创建 `STOP` 会保存检查点。不会自行恢复；显式 `--resume` 才重新开始，并移除该目录的 STOP。运行锁阻止两个程序同时使用一个输出目录。

## 构造算法与大模型的权限

- `legacy`：保留旧批次贪心构造作为对照。
- `extended + rolling`：上一组未全部结束，可投入下一组。
- `vehicle_policy=split`：送入和锻后接走可以由不同车辆完成，各自仍受空驶和载重限制。
- `backtrack_budget`：卡住后尝试前面有限数量的其他选择。
- `program_source`：主模型可编写新决策程序，联合评价合法的入料、出炉、选炉及车辆动作；支持分支、循环、聚合、状态相关规则。

生成程序不只是选择固定策略编号，但也不是任意修改整个项目。统一引擎产生候选动作、执行状态转移及有限回退；程序返回动作优先分数。禁止导入、文件/网络访问和修改规则，采用 AST 白名单、指令预算、独立进程、内存/CPU/墙钟限制。坏程序单独记为失败，明确回退到内置程序并反馈主模型。它是可信服务内的受限算法插件，不是面向任意敌意代码的通用沙箱。

本轮不增加：包棉、分切、开门温降、带料换温、炉外堆料、车辆交换料。这些原硬约束和简化口径保留。`reference-manifest.json` 在每个任务前后核对原模型和物理审计器哈希。

## 首日剪枝与结论边界

有完整基线后，所有 CP 改善任务都必须优于基线，包含带构造提示的任务。保温偏好开启时可保留首日得分相同、额外保温更少的改善。

扩展构造还使用保守上界：已固定的首日锻打 + `min(当天剩余时间, 剩余必需锻打量)`。上界都不能改善时剪枝。这个上界允许理想化并行，偏宽但不会因估计过低而错误排除更好分支。构造器并不枚举所有物理合法排程。

| 结果 | 能说明什么 |
|---|---|
| 构造失败 / 回退预算耗尽 | 此次构造没有完成，不能证明原问题无解 |
| CP UNKNOWN / 中断 | 未解决；不当作无解 |
| 局部 INFEASIBLE | 仅该固定邻域无改善 |
| 完整模型无更好方案 | 才能在该模型及目标下说明没有更好方案 |
| 达到用户目标 | 有完整合格方案，不自动等于全局最优 |

## Memory 和 Flash

事实试验记录只保存策略、原始状态类别、基线起点、改善量、耗时、失败类别和证据编号；不保存时间表、物料开始时刻数组或设备分配答案。构造阶段的失败及成功均单独记录，不以是否轮到 CP 为条件。

Flash 负责短反馈、经验卡片增改/退役和简短说明。每个更新必须引用存在的同场景证据、匹配卡片版本；统计由程序从事实重算，辅助模型不能改成绩。卡片是有适用条件及例外的假设，不成为工艺硬约束。更新保留版本历史；拒绝陈旧更新并继续搜索。检索保留场景差异，复用策略必须在新订单上重新计算与校验。

分配收益以完整审计后的全局改善为准；并发过期结果不重复奖励。冷启动获得第一份方案、已有方案上的改善分别记录。没有改善的方向会扩大改排范围或切到 broad，同时保留探索机会。

## 结果文件

- `report.json` / `progress.json`：首份合法时间、最好成绩、构造/CP/主模型/辅助模型分项耗时。
- `events.jsonl`：先落盘后展示的真实事件。
- `checkpoint.json`：任务、当前基线和分配状态。
- `best/views/orders.html`、`best/views/equipment.html`：双视角结果。
- `tasks/*/attempt-*/`：每次尝试、两份审计或求解日志。
- `calls/`：主模型请求及最终响应；不保存隐藏思考。
- `aux-*.json`：反馈摘要、更新接受/拒绝记录。
- `memory-stats.json`：使用次数、收益和失败类别。

并行阶段的耗时会重叠，分项相加不等于总耗时。应以 `first_valid_s`、`target_elapsed_s` 和 `elapsed_s` 判断用户等待时间。

## 参考与验证

借鉴 Qwen Live Harness 的 SQLite 事实/经验分层、结构化更新及过期更新处理；借鉴 DeepSeek Harness 的事件流程、独立摘要模型与先程序裁剪再压缩。不直接搬入它们的聊天产品和用户画像功能。

```bash
npm test
.venv/bin/python -m unittest discover -s python -p 'test_*.py'
.venv/bin/python python/benchmark_extensions.py runs/ablation-new
```

消融覆盖 4/8/20 单、1/2 辆车、两种种子，构造功能逐项开启，每次最多 1 秒构造，完整结果逐项审计。测试用手写 rank 程序标为 `program-hook-control`，不会冒充大模型生成结果。在线主模型/Flash 测试另存报告。冷启动和经验复用要用相同代码与资源比较；短测无解不能推论真实订单不可行。

本轮完成情况与实测数据：[验收记录](docs/acceptance.md)。
