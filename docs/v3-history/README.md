# 隆达策略 Harness v3

订单、在制状态、多候选经验、主模型策略适配、构造/CP-SAT 与完整校验组成一条流程。服务器目录：`/home/huanzz/longda-strategy-harness-v3-20261015`。v1、v2 和正式 CP-SAT 项目均保留。

## 完整流程

```text
当前订单 + 已执行事实 + 当前炉/车/压机状态
                       ↓
状态校验 → 订单与设备状态摘要 → 检索多条相关策略和经验卡片
  ├─ 通用构造 / 完全匹配的历史策略 → 重新计算 → 完整校验 → 首份基线
  └─ 主模型：比较当前状态与多个历史案例，保留/组合/修改策略
                              ↓
                 校验当前订单数量、证据及策略权限
                              ↓
          构造任务 + 多个 CP-SAT 搜索任务并行执行
                              ↓
       原物理审计 + 完整 CP 模型逐项验算 + 在制事实校验
                              ↓
          提交合法改善、输出 HTML、记录真实收益与失败
                ├─ 独立摘要模型：裁剪旧反馈→压缩→保留近期事实
                └─ 独立 Memory updater：提出增/改/退役→程序校验应用
```

两个辅助角色默认都用 `qwen3.7-plus`，是不同请求、不同输入输出、不同权限。主模型默认 `qwen3.8-max`。模型等待不阻塞构造和 CP 搜索。

历史策略的“每组五单”**不会自动缩放后执行**。数量、状态或工艺不同的案例只作参考；主模型结合当前订单重新提出完整配置。只有场景完全匹配、没有历史订单专属选择、当前数量校验通过的策略可以直接重建。复用的是算法，不是历史排程答案。

## 启动

新目录已经装好依赖。重新部署时运行 `bash scripts/setup.sh`，需要 Node.js 22.13+、Python 3.10+。

```bash
cd /home/huanzz/longda-strategy-harness-v3-20261015
.venv/bin/python start.py \
  --orders configs/orders20.json --vehicles 2 \
  --out runs/orders20-demo \
  --model qwen3.8-max --effort high --max-tokens 32768 \
  --summary-model qwen3.7-plus --memory-model qwen3.7-plus \
  --memory read-write --memory-db data/experience-v3.sqlite \
  --cp-workers 2 --threads 8 --construct-workers 1 \
  --api-timeout 900 --require-planner-strategy \
  --wall-seconds 1800 --rounds 4 --strategy-trials 6 --target 720
```

终端隐藏输入 API token，不写入配置、日志或 Memory。服务器短测数据库在 `runs/`，默认生产库不预装本次实验答案或经验。

在制续排使用已含 `production_state` 的订单文件，或增加 `--state configs/examples/observed-state.json`。状态中的订单 ID 须属于本次订单集；新增订单一起加入 `orders`。[状态格式](docs/state-input.md) 说明字段及边界。

当前评分日由 `now_ticks` 决定。例如第 2 天接入时，优化第 2 天的锻打时间；已经发生的部分是常量，不能重新安排。

### 常用参数

| 参数 | 默认 | 含义 |
|---|---:|---|
| `--orders` | configs/orders20.json | 当前全部订单、路线、交期与规则 |
| `--state` | 无 | 已执行前缀与当前设备状态；不传即冷启动 |
| `--vehicles` | 输入文件 | 1、2 辆；在制时须与已观察车辆一致 |
| `--memory` | read-write | off / read-only / read-write |
| `--memory-db` | data/experience-v3.sqlite | 独立 SQLite 策略经验库 |
| `--retrieval-limit` | 8 | 返回多个候选，最多 32 条 |
| `--model` | qwen3.8-max | 主模型；off 禁用 |
| `--summary-model` | qwen3.7-plus | 独立反馈摘要；off 禁用 |
| `--memory-model` | qwen3.7-plus | 独立经验更新；off 禁用 |
| `--aux-model` | 无 | 显式设置时覆盖上述两个辅助模型，兼容旧参数 |
| `--summary-tail` | 6 | 最近多少条反馈保留为未压缩的结构化事实 |
| `--summary-trigger-chars` | 6000 | 程序裁剪后的旧反馈长度达到此阈值再压缩 |
| `--memory-update-every` | 8 | 至少新增多少条本次事实后再更新经验 |
| `--aux-timeout` | 60 秒 | 单次辅助调用上限；失败回退，不阻塞求解 |
| `--effort` | high | 主模型思考档位；辅助模型不启用思考 |
| `--thinking-budget` | 0 | 0 使用 effort；正值改用显式思考 token 上限，不同时发送 effort |
| `--max-tokens` | 32768 | 主模型总输出额度，包括思考；正思考预算须小于此值 |
| `--cp-workers` | 2 | 独立 CP 搜索进程；0 仅供隔离构造实验 |
| `--threads` | 8 | 每个 CP 搜索进程内部线程数 |
| `--construct-workers` | 1 | 并行构造进程数 |
| `--solve-seconds` | 30 秒 | 单次 CP 尝试额度，包含建模 |
| `--construct-seconds` | 3 秒 | 单次扩展构造额度；完整审计另计 |
| `--wall-seconds` | 600 秒 | 本次总额度，包含模型思考、构造、搜索及校验 |
| `--rounds` | 4 | 本次主模型调用上限；每轮先执行并汇总模型策略，再调用下一轮。设 1 时仍充分执行，但不再让模型修改策略。 |
| `--strategy-trials` | 6 | 每条模型策略的反馈批次大小；含后续任务，默认至少 3 次 CP（启用时）。这是反馈节奏，不是整场搜索次数上限。 |
| `--strategy-solve-max-seconds` | 120 秒 | 模型策略的 CP 尝试遇到 UNKNOWN 后逐步增加额度，默认 30→60→120；受整场剩余时间限制，不低于显式 solve-seconds。 |
| `--api-timeout` | 900 秒 | 主模型单次等待上限，最大 7200；有效值仍受整场剩余时间限制 |
| `--require-planner-strategy` | false | 要求本次至少一条模型策略进入构造或 CP 并执行完一次尝试；通用方案达标时先保留，不提前结束，并保留一个试验名额 |
| `--max-trials` | 0（不限次数） | 默认不限制搜索尝试次数；仅显式设置正整数时启用累计次数上限，续跑也累计。总时间、目标和手动停止仍生效。 |
| `--target` | 720 分钟 | 当前统计日锻打目标；完整校验达标才停止 |
| `--preference` | none | hold-near-min：先提高当日锻打，同分时再减少额外保温 |
| `--allocation` | adaptive | 收益/成本与探索并用；round-robin 作对照 |
| `--bootstrap-limit` | 40 | 根据当前订单生成的通用配置数量；0 隔离主模型路径 |
| `--resume` | false | 显式继续同一运行 |

`--thinking-budget 4096` 是短测的显式配置，**不代表仍发送 high**；API 不接受两个设置同时发送。默认 high 保留，不能把短思考实验当作相同推理设置下的提速。

纯构造/CP 对照加 `--model off --aux-model off`，不会调用 API。总 CPU 申请为 `cp-workers × threads + construct-workers`，超过可用 CPU 会报错。

## 模型策略的执行与反馈循环

1. 主模型返回策略后，每条分配固定 `round-N/strategy-M` 编号。构造、CP 子任务、换种子重试和明确标记的回退任务都继承编号；Memory 的试验记录也保存来源。
2. 默认每条策略先完成 6 次尝试（CP 启用时保留至少 3 次 CP 机会）；多条策略分别获得测试机会，通用任务产生多少反馈都不会提前触发下一轮。
3. CP 可以并行运行不同种子。UNKNOWN 后给后续尝试更长额度；连续没有改善时沿用扩大邻域/转 broad 的机制。每次是新的求解，保留合法基线与提示，不保存求解器内部搜索树。实际方向、比例、种子和额度都记录。
4. 当模型任务和通用任务同时待执行，按新任务分配次数约 3:1 优先模型任务。它不是固定的 CPU 时间比例；外层进程数和每任务线程数仍由原参数控制。
5. 批次的任务完成并收齐结果后，保存 `calls/N/evaluation.json`，包含原基线、完整合法数、实际改善、UNKNOWN/失败类别、每次参数和耗时。下一轮 `request.json` 的 `previous_strategy_evaluation` 原样带入这份反馈，让模型决定继续、扩大还是换方向。
6. 模型轮数用完后，已有策略仍可继续搜索；批次完成不导致整场退出。目标达成、明确全局证明、总时间或手动停止可以提前结束，尚未完成的批次不会被称为已充分测试。

每条策略的累计统计在 `report.json` 的 `strategy_campaigns`，批次历史在 `strategy_cycles`，逐次记录在 `history`。累计改善只记该任务发布时超出共享最好方案的部分，不能把其他任务的贡献算给当前策略。尝试耗时相加和并行运行总历时分开；CPU-slot-seconds 是占用额度估算，不是硬件实测 CPU 时间。UNKNOWN 仍表示未解决。

检查点保留批次进度、来源、种子计数与未完成任务；同代码续跑不会清零这些统计。旧版源码备份在 `backups/before-strategy-loop-20261016.tgz`。本次升级改变代码身份，请新建输出目录；旧结果和 Memory 可保留使用，旧检查点需要旧代码才能恢复。

## Memory 的复用与更新

1. **兼容过滤**：设备容量、车辆规则、时间单位、规则哈希、目标偏好一致；工艺不同则明确展示适配差异。
2. **状态检索**：订单路线与数量、剩余工序/净锻打工作量、保温温度/剩余上下限、炉位、压机、车辆位置与可用时刻、当天及交期剩余时间。
3. **多候选保留**：同策略证据聚合，保留不同构造方向；不是只找第一名，也不是只取最近记录。相似度是排序分，不冒充成功概率。
4. **适配**：主模型读取多条策略、原状态、当前差异及经验卡片，说明采用/修改原因并引用证据。程序重新核对订单守恒。可提出受限的 `rank(candidates,state)` 程序，不能改硬规则。
5. **反馈归因**：记录实际策略/种子、原基线、收益、成本、审计结果与失败类别；关联“哪条历史经验被用于哪个新场景”。失败不永久删除其他备选。
6. **经验更新**：独立 updater 提出 add/update/retire；程序核对来源、物理规则域及版本，重算统计。原始事实不删除，旧版本保留；卡片是条件性假设，不是工艺约束。

Memory 保存上下文特征、策略和试验结果，不保存排程时刻数组或设备分配答案。本次运行的检查点可以保留已审计排程以便恢复，不属于经验复用。生成程序也不得硬编码历史时间表。

## 物理规则与边界

- 冻结 `reference/` 原模型、审计器和依赖，任务前后校验哈希。
- 六炉每炉五支、一台压机、1/2 辆车；空驶/载料均五分钟；不堆料、不交换、锻完即接走、空炉才可换温；保温上下限与交期照旧。
- 保留原 3 ticks/minute 精度和换温耗时。本次没有改成五分钟决策网格。
- 沿用简化：无包棉、分切、开门温降。
- 在制状态增加“事实不能改、未执行动作不能倒填”约束。已有历史的物料不再做冷启动同类订单身份交换；这是身份处理，不是放松设备/工艺规则。
- 支持保温中、锻打中、运输中、续排加入新订单。需要已执行装取记录，尚未实现只凭任意不完整 MES 快照自动补历史。
- 日期切片不清零设备和保温状态；已开始动作保留确定终点，未开始的未来安排不从历史答案加载。

构造失败、CP UNKNOWN、局部无解均不能说成全局无解。有基线后保留当天优先比较，完整合法后才提交改善；不能用未排完当天的低累计值误剪枝。

## 恢复与输出

Ctrl+C、输出目录 STOP、总额度到期都会保存检查点，不自动重启。恢复必须显式 `--resume`，且订单、状态、代码与偏好一致：

```bash
.venv/bin/python start.py --orders configs/orders20.json --vehicles 2 \
  --out runs/orders20-demo --resume --wall-seconds 600
```

中断的 CP 任务重新开始求解，不保存求解器内部树；队列、已试策略、收益、摘要和当前基线可恢复。运行锁防止重复启动。

| 输出 | 内容 |
|---|---|
| report.json / progress.json | 首份合法耗时、最好值、分项计时、状态和适配统计 |
| scenario.json | 完整本次输入，含观察状态 |
| memory-retrieved.json | 多候选、来源状态、差异、可否直接使用 |
| calls/*/request.json、plan.json | 主模型输入和校验后的策略；无隐藏思考 |
| summary-*.json | 独立摘要、证据、降级记录 |
| memory-update-*.json | 更新建议、实际应用与拒绝原因 |
| memory-stats.json | 执行收益、经验迁移关联与使用次数 |
| tasks/ | 构造、真 CP 搜索和完整校验记录 |
| best/views/orders.html、equipment.html | 同一份已校验方案的两个视角 |
| checkpoint.json、events.jsonl | 恢复状态和事实事件 |

并行任务耗时不能相加当总耗时；看 first_valid_s、target_elapsed_s、elapsed_s。

## 参考与测试

Qwen Live Harness：独立 updater，读取已有经验及新事实，提出结构化更新，由程序应用；辅助模型采用 qwen3.7-plus。DeepSeek Harness：独立摘要模型，程序先裁剪旧输出、再压缩，并保留近期内容。技术栈保持 TypeScript/Node 编排和 Python/OR-Tools 执行，没有搬入聊天产品或其完整向量检索框架。

```bash
npm test
.venv/bin/python -m unittest discover -s python -p 'test_*.py' -v
```

[文件职责](docs/architecture.md) · [状态格式](docs/state-input.md) · [实测结果与限制](docs/acceptance.md)

## 主模型等待与至少一次策略交付（2026-10-16）

建议使用 `--rounds 1 --api-timeout 900 --wall-seconds 1800 --require-planner-strategy`。
主模型 high/xhigh 和总输出额度保持调用者设置；不会偷偷降低思考强度。首轮提示要求先交付一个紧凑配置，后续轮次再按反馈写新排序程序。

`api-timeout` 是单次接口上限；`wall-seconds` 是整场上限。整场结束、用户 Ctrl+C/STOP、达到尝试次数仍会停止，不会因模型请求隐式突破预算。启动及每次调用均打印实际等待额度；整场时间不足时明确提示。

要求模型参与时，程序保留至少一个构造尝试名额，通用构造先达标也会等待模型策略执行。收到且通过格式/订单覆盖校验还不算实际执行；最终报告区分完整答复数、有效新策略数和执行次数。接口超时/无效输出仍无法保证模型交付，轮数耗尽报告 `planner_not_delivered`；总时间耗尽则是 `budget` 并注明未完成交付。执行过策略也不意味着得到合法方案或带来改善。

每次模型调用目录 `calls/N/progress.json` 保存等待额度、思考字符数、正文字符数和可见正文；`error.json` 区分 `api_timeout`、`cancelled`、`output_limit`、无效格式等。隐藏思考只计数，不保存内容。收到 `end_turn` 后立即交给策略校验，不依赖 HTTP 代理关闭连接。不完整/截断的 JSON 不能执行。

检查点仍校验代码一致性。本次修改前的代码备份在服务器 `backups/before-model-timeout-20261016.tgz`；旧排程与 Memory 保留。旧检查点必须使用原代码恢复；新代码请使用新输出目录。

短测（不调用真实模型 API）：`node scripts/smoke-model-delivery.mjs runs/model-delivery-smoke-NEW`。它使用模拟流式模型、真实构造与双重校验，检查晚到策略的执行、试验名额预留，以及超时无法交付时的明确退出。它不是 high/xhigh 的效果对比实验。

### 构造与 CP 策略分别去重

相同构造配置不再直接丢弃整条模型策略。只改 `direction`、`fraction`、`focus_orders` 或求解 seed 时，可以复用本次构造产物，再单独排入 CP 搜索任务。CP 去重还区分基线与提示方案；模型首次任务按提议的重排范围执行，后续普通重试才按失败次数扩大范围。终端 `策略接入` 会说明是否复用构造结果，模型参与计数涵盖实际执行的构造和 CP 搜索。

回归流程新增“构造相同、CP 比例变为 0.5”案例，检查不重新构造、确实调用 CP 并执行所提比例。这是模拟模型返回和真实求解的控制测试，不代表该策略一定提升真实 20 单成绩。
