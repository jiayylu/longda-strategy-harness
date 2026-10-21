# v12 命令行参考

入口为 `.venv/bin/python start.py`，不是早期独立 CP-SAT 项目的 `longda-cpsat first/search`。本页默认值和取值范围对应 `src/cli.ts` 的 v12 参数解析；README 中的推荐命令会覆盖其中一部分默认值。

布尔开关只写参数名，例如 `--resume`。字符串开关必须带值，例如 `--partial-completion on`。多行命令每行末尾的 `\` 后不要再加空格；最后一行无需 `\`。

## 输入、输出与恢复

| 参数 | 默认 | 取值 / 含义 |
|---|---|---|
| `--orders` | `configs/orders20.json` | 订单及工艺 JSON，路径按项目根目录解析 |
| `--vehicles` | 使用订单文件配置 | 整数 1 或 2，覆盖车辆数量 |
| `--state` | 无 | 在制状态 JSON，覆盖订单文件内的 `production_state`，见 state-input.md |
| `--out` | 无，必填 | 新的运行输出目录；目录已存在时必须明确恢复 |
| `--resume` | false | 恢复同代码、同输入、同硬规则、同偏好的原输出目录 |
| `--python` | `.venv/bin/python` | Python worker 的解释器路径 |
| `--help` | false | 打印帮助，不启动搜索、不要求 token |

## 主模型、辅助模型和接口

| 参数 | 默认 | 取值 / 含义 |
|---|---|---|
| `--model` | `qwen3.8-max` | 主模型 ID；`off` 关闭主模型 |
| `--summary-model` | `qwen3.7-plus` | 独立摘要模型；`off` 关闭 |
| `--memory-model` | `qwen3.7-plus` | 独立经验更新模型；`off` 关闭；写入还需 Memory 模式允许 |
| `--aux-model` | 无 | 兼容参数，同时覆盖 summary-model 与 memory-model |
| `--base-url` | `https://dashscope.aliyuncs.com/apps/anthropic` | Anthropic 兼容接口基础地址，必须 HTTPS |
| `--effort` | `high` | 发给提供方的思考强度；`off` 禁用思考。非零 thinking-budget 优先；其他强度需接口支持 |
| `--thinking-budget` | `0` | 0 不显式设 token 预算；非零需整数 1024～32768 且小于 max-tokens；effort=off 时不启用思考 |
| `--max-tokens` | `32768` | 整数 1024～65536，最大输出额度，含提供方计入的思考部分 |
| `--api-timeout` | `900` | 主模型每轮调用流程额度，1～7200 秒；纠错和工具交互共享该轮额度，还受整场剩余额度限制 |
| `--aux-timeout` | `60` | 辅助模型调用额度，1～120 秒 |
| `--planner-format` | `schema` | `schema` 或 `json`，策略交付格式 |
| `--rounds` | `4` | 普通规划轮数，整数 0～100；不包含额外修正和协议请求 |
| `--repair-attempts` | `2` | 每个相关纠错流程最多修正次数，整数 0～8；不是整场修正调用总数 |
| `--require-planner-strategy` | false | 把模型有效交付并执行纳入交付条件；要求开启主模型且 rounds>=1，不保证接口一定成功 |
| `--summary-tail` | `6` | 摘要时保留的近期内容条数，整数 1～32 |
| `--summary-trigger-chars` | `6000` | 触发摘要的字符阈值，500～50000 |

仅 `--model off` 还会保留默认的辅助模型。完全离线应同时设置 `--model off --summary-model off --memory-model off`；推荐再加 `--rounds 0 --memory off`。token 通过 start.py 隐藏输入或进程环境的 `ANTHROPIC_AUTH_TOKEN` 提供，不落入项目文件。

## 目标和额度

| 参数 | 默认 | 取值 / 含义 |
|---|---|---|
| `--target` | `720` | 当前统计日净锻打分钟目标，0～1440；达到且满足交付要求可提前停止；不是硬约束、不保证可达 |
| `--preference` | `none` | `none` 或 `hold-near-min`；后者在首日/当前日锻打同分时减少超出下限的保温 |
| `--wall-seconds` | `600` | 本次调用整场额度，1～86400 秒；包含模型、构造、CP 和校验。恢复时给本次新额度 |
| `--max-trials` | `0` | 整数 0～100000；0 不限制尝试次数，但 wall-seconds 仍生效 |
| `--construct-seconds` | `3` | 单次构造额度，0.01～60 秒 |
| `--solve-seconds` | `30` | 普通 CP 单次求解额度，0.1～3600 秒，不等于端到端任务耗时 |
| `--completion-seconds` | `10` | 部分排程补全的单次额度，0.01～300 秒 |
| `--strategy-trials` | `6` | 每条模型策略进入下一次反馈前的一批尝试数，整数 1～1000；并非终身次数上限 |
| `--strategy-solve-max-seconds` | `120` | 模型策略后续 CP 任务的单次额度上限，0.1～3600 秒 |

目标按当前生产状态所在日统计，冷启动即第一天。净锻打取工序与当日 24 小时时间窗的重叠；跨日部分只计当天片段。保温 min-max、交期和设备规则始终是硬约束。

本版没有“无整场时限”开关，也没有通用的 `--first-time-limit`。`--wall-seconds 0` 不合法。冷却不等于无改善整体早停；可能停止的原因仍包括额度、目标、人工停止等。

## 并行、供给与搜索分配

| 参数 | 默认 | 取值 / 含义 |
|---|---|---|
| `--cp-workers` | `2` | 并行 CP 任务数，整数 0～16；0 不启动 CP worker |
| `--threads` | `8` | 每个 CP 任务内部线程数，整数 1～128 |
| `--construct-workers` | `1` | 并行构造任务数，整数 1～8 |
| `--allocation` | `adaptive` | `adaptive` 按收益分配，`round-robin` 轮流分配 |
| `--exploration-share` | `0.15` | 探索份额，程序数值校验范围 0.01～0.5 |
| `--portfolio-refill` | `on` | `on/off`，持续生成不同方向与范围的通用 CP 任务 |
| `--cp-retry-policy` | `on` | `on/off`，通用任务冷却及同请求并发限制 |
| `--cp-no-gain-limit` | `3` | 同类任务连续无新发布阈值，整数 1～100 |
| `--cp-cooldown-seconds` | `90` | 暂缓同类任务，1～3600 秒；冷却后可再次探索 |
| `--partial-completion` | `on` | `on/off`，允许可用的部分方案交给 CP 寻找完整后续 |
| `--completion-priority` | `value` | `value` 按价值排序，`fifo` 按队列顺序；都保留资源准入限制 |
| `--diversity-probes` | `on` | `on/off`，为低分但结构不同的构造保留少量改善探测 |
| `--probing-level` | `0` | CP 探测强度，整数 0～2 |
| `--presolve` | `auto` | `auto`、`standard` 或 `off`，CP 预处理策略 |

并行请求需满足 `cp-workers × threads + construct-workers <= availableParallelism()`。这只是配置检查，不能把配置线程数当作实测 CPU 使用率。默认仍有构造 worker；只关闭大模型不等于去掉构造器。

## Memory 与证据

| 参数 | 默认 | 取值 / 含义 |
|---|---|---|
| `--memory` | `read-write` | `off`、`read-only`、`read-write` |
| `--memory-content` | `cards` | `raw` 原始阶段记录；`cards` 加上经过审查的经验卡 |
| `--memory-db` | `data/experience-v12.sqlite` | SQLite 路径；read-only 需要已存在且已迁移的 schema 4 库 |
| `--evidence-index` | `data/evidence-index.json` | 跨运行原始证据索引；原始目录仍需可访问 |
| `--retrieval-limit` | `8` | 每阶段检索候选上限，整数 1～32，不是全部阶段总上限 |
| `--memory-update-every` | `8` | 经验更新检查间隔，整数 2～100；不等于同步 API 调用次数 |

新克隆的仓库不带数据库、858 卡或运行轨迹。服务器已有库不因 Git 更新自动复制或清空。模型生成的卡片更新要经过阶段、引用证据和退役理由检查。

## 开发与兼容参数

| 参数 | 默认 | 取值 / 含义 |
|---|---|---|
| `--initial-strategy-file` | 无 | 注入需要解析和验证的初始策略 JSON |
| `--program-file` | 无 | 启动时使用的排序程序文件，仍由执行器限制与物理模型校验 |
| `--algorithm-development` | false | 构造算法开发验收模式，要求模型使用 calendar 并实际修改算法源码；不推荐日常运行打开 |
| `--stop-after-repair` | false | 纠错验收用，完成修正流程后结束 |
| `--legacy-bootstrap` | false | 切回旧的保底策略初始化路径，供对照；不是更改硬约束 |
| `--bootstrap-limit` | `40` | 保底初始化策略限制，整数 0～500 |

## 完整恢复示例

假定要恢复的目录已经存在且已停止，代码、输入与偏好均未变。请把示例目录换成自己的原目录。此命令补给本次最多 20 分钟额度，不会启动一份新的独立 run。

```bash
cd /home/huanzz/longda-strategy-harness-v12-20261020

.venv/bin/python start.py \
  --orders configs/orders20.json --vehicles 2 \
  --out runs/orders20-car2-v12-20261021-120000 --resume \
  --model qwen3.8-max --thinking-budget 4096 --max-tokens 32768 \
  --summary-model qwen3.7-plus --memory-model qwen3.7-plus \
  --memory read-write --memory-content cards --memory-db data/experience-v12.sqlite \
  --cp-workers 6 --threads 8 --construct-workers 1 \
  --partial-completion on --completion-seconds 30 \
  --completion-priority value --diversity-probes on --exploration-share 0.15 \
  --portfolio-refill on --cp-retry-policy on \
  --cp-no-gain-limit 3 --cp-cooldown-seconds 90 \
  --preference none --construct-seconds 8 --solve-seconds 45 \
  --rounds 2 --strategy-trials 6 --repair-attempts 2 \
  --api-timeout 300 --wall-seconds 1200 --target 870
```

已有进程持锁时恢复会被拒绝；不要删除活跃锁强行启动第二份。历史模型调用轮次会保留，普通规划轮数不会因为恢复自动清零。最终方案保留不依赖主模型最终文字答复成功。

## 查看日志、提示词和结果

```bash
# 替换成自己的输出目录
.venv/bin/python scripts/inspect_run.py run runs/orders20-car2-v12-20261021-120000
.venv/bin/python scripts/inspect_run.py prompts runs/orders20-car2-v12-20261021-120000
.venv/bin/python scripts/inspect_run.py algorithms runs/orders20-car2-v12-20261021-120000
```

`prompts` 输出实际发给模型的请求内容，可能包含订单、策略和证据摘要；属于运行资料，不应整体提交 Git。`scripts/inspect_run.py memory data/experience-v12.sqlite` 查看原始经验记录；它不是启用经验卡的完整展示，也不代表那些记录都被模型采用。
