# 隆达排程 Strategy Harness v12

根据订单、工艺和当前在制状态生成完整合法排程，优先提高当前统计日的净锻打时间。快速构造、CP-SAT 和大模型策略并行运行；只有通过独立物理审计和完整约束验算的方案才发布为基线。

**仓库名称保留 `longda-strategy-harness-v2-20261015`，当前代码版本为 `12.0.0`。** 原 v2 提交保留在 Git 历史及 `v2-before-v12` 标签中。服务器工作目录为 `/home/huanzz/longda-strategy-harness-v12-20261020`。

## 安装

需要 **Node.js >= 22.13、Python >= 3.10**。服务器运行时为 Node.js 22.22.0、Python 3.10.12；CP-SAT 使用 OR-Tools 9.15.6755。

```bash
git clone https://github.com/jiayylu/longda-strategy-harness-v2-20261015.git
cd longda-strategy-harness-v2-20261015
bash scripts/setup.sh
.venv/bin/python start.py --help
```

`setup.sh` 安装依赖并编译 TypeScript，不启动搜索。系统默认的 `python3` 需满足版本要求；已安装的服务器目录无需重复安装。新 clone 的目录名与服务器目录不同，下面各命令的 `cd` 应按实际安装位置替换。

## 双车：大模型 + 构造 + CP-SAT，最多 20 分钟

```bash
cd /home/huanzz/longda-strategy-harness-v12-20261020

.venv/bin/python start.py \
  --orders configs/orders20.json --vehicles 2 \
  --out "runs/orders20-car2-v12-$(date +%Y%m%d-%H%M%S)" \
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

首次运行提示 `API token (not saved):`，在终端粘贴 token 后回车，输入不回显。不要把 token 写进命令、配置文件或 Git。默认接口为 `https://dashscope.aliyuncs.com/apps/anthropic`，可用 `--base-url` 指定兼容接口。

| 参数组合 | 实际含义 |
|---|---|
| `--vehicles 2` | 两辆车；单车改为 `1`，并使用新的输出目录 |
| `--cp-workers 6 --threads 8 --construct-workers 1` | 最多 6 个 CP 任务，各 8 个内部线程，另有 1 个构造任务；需至少 49 个 Node 检测到的可用并行度，槽位不保证时刻占满 |
| `--thinking-budget 4096 --max-tokens 32768` | 主模型显式思考 token 预算和最大输出额度；预算必须小于输出额度 |
| `--rounds 2` | 最多两轮普通规划；修正、工具交互及协议重试可能产生额外模型请求 |
| `--strategy-trials 6` | 每条模型策略先执行一批尝试后反馈；不是最多只尝试六次，也不保证六次都是构造 |
| `--construct-seconds 8 --solve-seconds 45` | 单次构造和普通 CP 求解额度；模型策略后续 CP 另受 `--strategy-solve-max-seconds` 控制，默认上限 120 秒 |
| `--completion-seconds 30` | 部分排程交给 CP 补全的单次额度，还受价值筛选与资源份额限制 |
| `--cp-no-gain-limit 3 --cp-cooldown-seconds 90` | 同类通用任务连续无收益后暂缓，随后可恢复；不会整体停滞早停，也不是无解证明 |
| `--wall-seconds 1200` | 本次整场最多约 20 分钟，模型、构造、搜索及校验都计入；收尾可稍超时 |
| `--target 870` | 完整合法方案的当前日锻打目标 870 分钟，即 60.42%；目标达到且满足交付条件可提前结束，不代表全局最优 |
| `--preference none` | 只比较当前日锻打时间；改成 `hold-near-min` 后，同日分数相同时优先保温更接近下限的方案 |

如果要改用强度设置，把 `--thinking-budget 4096` 替换为 `--thinking-budget 0 --effort high`。显式思考预算非零时，代码优先发送预算，不同时发送 effort；接口支持的强度取决于提供方。

## 单车与关闭大模型

单车复制上面的完整命令，把 `--vehicles 2` 改成 `--vehicles 1`，输出目录中的 `car2` 改成 `car1`。例如 `--target 600` 对应当前日使用率 41.67%。单车和双车分别执行真实的车辆行程衔接约束。

下面关闭主模型、摘要模型和 Memory 更新模型，仍运行程序构造与 CP-SAT；不需要 API token，也不读取历史 Memory。它不是枚举全部合法排程。

```bash
cd /home/huanzz/longda-strategy-harness-v12-20261020

.venv/bin/python start.py \
  --orders configs/orders20.json --vehicles 2 \
  --out "runs/orders20-car2-v12-no-llm-$(date +%Y%m%d-%H%M%S)" \
  --model off --summary-model off --memory-model off --rounds 0 \
  --memory off \
  --cp-workers 6 --threads 8 --construct-workers 1 \
  --partial-completion on --completion-seconds 30 \
  --completion-priority value --diversity-probes on --exploration-share 0.15 \
  --portfolio-refill on --cp-retry-policy on \
  --cp-no-gain-limit 3 --cp-cooldown-seconds 90 \
  --preference none --construct-seconds 8 --solve-seconds 45 \
  --wall-seconds 1200 --target 870
```

## 暂停、恢复与时间额度

按 `Ctrl+C` 请求停止并保存已审计最好方案与检查点。进程完全退出后，用**原命令、原输出目录**重新运行并加 `--resume`。例如原目录为 `runs/orders20-car2-v12-20261021-120000`，恢复时将 `--out` 固定为该目录，不再用 `$(date ...)`，并加 `--resume`。完整恢复命令见 [docs/cli.md](docs/cli.md)。

恢复要求订单、在制输入、算法代码、硬规则和偏好一致，不能直接恢复旧版运行目录。可恢复基线、队列和冷却账本，不能恢复 CP-SAT 内部搜索树。`--wall-seconds` 是**本次调用的新额度**，报告 `elapsed_s` 累计历史运行时间。已有输出目录不加 `--resume` 会报错。

本版 `--wall-seconds` 范围为 1～86400 秒，没有关闭整场时限的参数。`--max-trials 0` 只是不限制尝试次数，不能取消时间额度。目标达到、人工停止等也可以提前结束。

## 结果在哪里看

输出保存到 `--out` 指定目录；尚未找到合法方案时不一定存在 `best/`。

| 文件 | 内容 |
|---|---|
| `progress.json` / `report.json` | 实时进度 / 最终报告、停止原因、模型交付情况 |
| `best.json` | 最好完整合法方案的索引与指标 |
| `best/best_schedule.json` | 完整排程 |
| `best/best_audit.json` | 独立物理审计与逐日净锻打使用率 |
| `best/exact_model_verification.json` | 完整模型约束逐项验算 |
| `best/result_summary.json` | 完工历时、逐日及平均使用率 |
| `best/views/orders.html` / `equipment.html` | 订单 / 设备两个视角 |
| `search-statistics.json` | 构造、补全、局部 CP、全局 CP 的独立统计 |
| `events.jsonl` | 任务、策略、冷却、恢复和基线发布事件 |
| `checkpoint.json` | 基线、待办任务、经验归因和调度账本 |

查看双视角（另开终端，替换为实际目录）：

```bash
.venv/bin/python -m http.server 8770 \
  --bind 127.0.0.1 \
  --directory runs/orders20-car2-v12-20261021-120000/best/views
```

服务器部署时，在本机执行 `ssh -N -L 8770:127.0.0.1:8770 huanzz@39.96.162.45`，打开 `http://127.0.0.1:8770/orders.html` 或 `equipment.html`。

`budget` 是运行额度用完，`user_stop` 是人工停止，`target_reached` 是完整合法方案达到目标。`UNKNOWN` 不能当无解；局部 `OPTIMAL` 不能当整场全局最优。“完整合法结果”计数不保证对应互不相同的排程。

## Memory 和历史证据

- 经验按 `construct`、`cp_complete`、`cp_improve` 分阶段归因与检索，退役需有理由和证据。
- **全新 Git clone 不含历史 SQLite、858 经验卡和原始运行证据。** 首次 `read-write` 创建新库；服务器现有 `data/experience-v12.sqlite` 继续保留历史经验。
- 迁移 Memory 应使用 SQLite 一致性备份，并保留或配置 `--evidence-index` 及原始证据路径。不要把整个运行目录上传到仓库。
- `--memory off` 不读写；`read-only` 要求数据库存在；`read-write` 允许更新。`--memory-content raw` 提供原始阶段记录，`cards` 额外提供已启用的经验卡。
- 主模型默认 `qwen3.8-max`；独立摘要和 Memory updater 默认 `qwen3.7-plus`。模型不能修改受保护的物理规则。

## 当前模型与目录

示例为四张工艺卡各五单，共 20 单、160 道锻打工序，四天完成；六炉各五支、一台压机、一或两辆车。载料和异地点空驶均为五分钟；禁止压机等车、同车交换料；换温需空炉，保留准确换温耗时。暂不做包棉、分切和开门温降。

内部每分钟 3 ticks（20 秒）用于准确表示时间。`max_replay_step_minutes=5` 是回放步长上限，不是 CP-SAT 决策网格。当前独立 CP/日历模型尚未接入 Stroma。

```text
start.py                  # 交互读取凭据、启动 Node
src/                      # 模型、策略、任务池、Memory、检查点和统计
python/                   # 构造、在制状态、CP 任务与校验
reference/                # 受保护的物理模型和 HTML 模板
configs/                  # 脱敏示例订单、策略与在制测试输入
tests/                    # TS 测试；Python 测试在 python/
scripts/                  # 安装、诊断和历史对照脚本
docs/                     # 参数、状态输入、版本说明和历史记录
data/、runs/、dist/、.venv/ # 本地生成，不随 Git 分发
```

v12 新增任务池持续换轮、通用 CP 无收益冷却、同范围并发限制与检查点记录。一次已完成的双车运行：1200.99 秒，首解 5.37 秒，首日最好 856 分钟（59.44%），全部完成 2 天 21 小时 13 分钟，双重校验通过。这是单次结果，不证明提速或全局最优。

完整参数见 [docs/cli.md](docs/cli.md)，运行证据摘要见 [docs/run-v12-summary.md](docs/run-v12-summary.md)，在制输入见 [docs/state-input.md](docs/state-input.md)，改动见 [docs/release-v12.md](docs/release-v12.md)，原 v2 说明见 [docs/v2-history/README.md](docs/v2-history/README.md)。旧版验收需按版本区分，历史对照脚本可能依赖服务器原始运行目录。
