# 隆达策略 Harness v4：修正与改善两个循环

服务器独立目录：`/home/huanzz/longda-strategy-harness-v4-20261016`。原 v3 目录保留。TypeScript/Node 负责模型、任务、反馈和恢复；Python 构造器与 OR-Tools 负责排程和校验。本版不修改工艺、设备、车辆硬约束。

## 现在怎么配合

```text
订单 + 当前在制状态 + 多条历史策略参考
          │
          ├─ 通用构造并行启动 → 完整双重校验 → 可交付基线
          │
          └─ 主模型提出策略 / rank 程序
                        ↓
                    执行构造
              ┌─────────┴──────────┐
              │失败                │完整且双重校验通过
              ↓                    ↓
       暂停此策略的 CP          发布候选 / 更新基线
       保存具体错误现场             ↓
              ↓                CP 按指定范围改善
       立即请求模型修正               ↓
              ↓                汇总实测结果和成本
       重新构造及校验                ↓
              └─────────→ 模型决定下一步
```

“立即”表示优先排入下一次主模型调用，不等待这条失败策略完成六次 CP；若主模型已有请求，先等待该请求结束。其他独立策略和已合法的基线仍可继续工作。

**修正循环**处理不支持的程序语法、构造死路、构造额度结束等情况。反馈包含错误类别、原程序/配置、已排工序、最近决策、车/炉/材料的预订状态、保温期限，以及候选被拒的原因。模型必须改变实际构造设置；只改名字、说明或 CP 重排比例不能冒充修复。失败不等于整个订单集无解。

**改善循环**只接收完整校验通过的构造结果。CP 真正搜索完整生产周期内的方案，按模型指定的放开订单、方向和比例执行。不会因失败次数增加而暗中扩大到全局。连续三次未改善、局部 INFEASIBLE 或重复任务会将该策略暂停，交回模型判断；UNKNOWN 仍是未解决。不同策略的独立 CP 任务仍可以并行。

## 运行示例（不会自动启动）

```bash
cd /home/huanzz/longda-strategy-harness-v4-20261016
.venv/bin/python start.py \
  --orders configs/orders20.json --vehicles 1 \
  --out runs/car1-two-loops-NEW \
  --model qwen3.8-max --effort high --max-tokens 32768 \
  --summary-model qwen3.7-plus --memory-model qwen3.7-plus \
  --memory read-write --memory-db data/experience-v3.sqlite \
  --cp-workers 2 --threads 8 --construct-workers 1 \
  --rounds 4 --strategy-trials 6 --repair-attempts 2 \
  --api-timeout 900 --wall-seconds 1800 \
  --require-planner-strategy --target 600
```

`NEW` 换成未使用的目录名。密钥在终端隐藏输入，不写日志、配置或 Memory。新目录中的经验库是旧库的一份独立 SQLite 快照；后续写入不会修改旧库。默认构造配置会从当前订单重新生成，不加载历史排程答案。

| 参数 | 默认 | 含义 |
|---|---:|---|
| `--rounds` | 4 | 正常策略规划调用次数；修正调用另计 |
| `--strategy-trials` | 6 | 正常策略反馈批次大小；构造失败优先修正，不强行凑够次数 |
| `--repair-attempts` | 2 | 同一失败策略及其修订链累计最多调用主模型修正几次；0 禁用自动修正，最大 8 |
| `--initial-strategy-file` | 无 | 导入策略 JSON 进行显式重放；不是导入排程答案 |
| `--stop-after-repair` | false | 修正策略执行一次后停止，供短测使用 |
| `--cp-workers / --threads` | 2 / 8 | CP 进程数 / 每进程内部线程数 |
| `--construct-workers` | 1 | 构造进程数 |
| `--solve-seconds` | 30 | 单次 CP 建模及搜索额度 |
| `--strategy-solve-max-seconds` | 120 | UNKNOWN 后单次额度逐步增加的上限；不自动改变搜索范围 |
| `--construct-seconds` | 3 | 扩展构造的额度；完整校验另计 |
| `--preference` | none | 可选 hold-near-min；先比较当日锻打，同分再比较额外保温 |
| `--api-timeout` | 900 | 单次主模型等待上限；仍受整场剩余时间约束 |
| `--wall-seconds` | 600 | 整场额度，包括模型、构造、CP、校验 |
| `--thinking-budget` | 0 | 0 使用 effort；正数改为显式思考 token 预算，不同时发送 effort |
| `--target` | 720 | 完整合法方案的当前日净锻打目标，单位分钟 |
| `--max-trials` | 0 | 0 不设尝试次数上限；没有旧的 160 次默认上限 |
| `--memory` | read-write | off / read-only / read-write |
| `--state` | 无 | 在制状态输入，见 [状态格式](docs/state-input.md) |
| `--resume` | false | 显式恢复同代码、同输入的检查点 |

修正额度耗尽后会保留失败证据，进入正常规划反馈或报告任务耗尽；不会把失败策略重新投入同样的 CP 空转。若模型接口超时，程序不能保证交付策略，也不会把半截 JSON 当成成功。

## 什么被保留，什么被改变

- 保留六炉、每炉五支、一台压机、单/双车、五分钟载料与空驶、不堆料、不交换、锻完即接走、空炉换温、保温范围和交期规则。
- 保留原时间刻度及精确换温；不在本次改为五分钟网格。原有包棉、分切、开门降温简化也没有变化。
- 保留通用构造、模型生成受限 rank 程序、多候选 Memory、独立摘要和独立 Memory updater、完整审计和双视角 HTML。
- 改变任务控制：构造失败阻断其 CP；原始失败优先反馈；CP 按实际输入去重；不再暗中扩大重排范围。
- 去重比较当前订单/规则、基线、提示内容、实际放开订单、种子、线程、额度等。只改策略名字不产生新的 CP 搜索；改基线、种子或额度可以产生新任务。跳过重复任务不等于证明无解。
- 失败的部分排程与现场状态保留在本次运行中。可复用 Memory 只存状态特征、策略、失败类别和实测证据，不存具体排程时刻答案。

## 输出、审计与恢复

| 文件 | 内容 |
|---|---|
| `report.json / progress.json` | 最好合法方案、时间、修正调用、策略累计、重复跳过数 |
| `calls/N/request.json / response.json / plan.json / error.json` | 模型输入、完整答复、可执行策略或明确失败 |
| `calls/N/evaluation.json` | 正常策略批次及后代任务反馈 |
| `tasks/.../request.json / result.json` | 每次真实构造或 CP 的输入与结果 |
| `cp-search-registry.json` | 实际 CP 输入的运行内去重记录 |
| `events.jsonl` | 有 call_id 的工具调用/结果，修正请求、模型回复、校验事实 |
| `checkpoint.json` | 已审计基线、队列、待修正问题、策略修订关系、统计 |
| `best/views/orders.html / equipment.html` | 已审计排程的两个视角 |

Ctrl+C、STOP 文件或额度结束保存检查点。恢复必须显式 `--resume`，不会自行重启。未落盘的工具结果标记 OUTCOME_UNKNOWN；不会伪造失败结论。CP 内部搜索树不保存；恢复后该任务重新求解，模型中断请求也可能重发。新旧代码检查点不混用；旧结果用旧目录恢复。

## 验证与参考

```bash
npm test
.venv/bin/python -m unittest discover -s python -p 'test_*.py' -v
node scripts/smoke-repair.mjs runs/smoke-repair-NEW
node scripts/smoke-strategy-cycles.mjs runs/smoke-improvement-NEW
node scripts/smoke-repair-resume.mjs runs/smoke-resume-NEW
.venv/bin/python scripts/regression-v4.py
```

三项 smoke 使用测试模型响应，但构造、CP 与校验是真实的。真实模型测试单独记录，不能混作模型有效性的证据。

[模块职责](docs/architecture.md) · [参考实现与取舍](docs/reference-adaptation.md) · [本轮验收](docs/acceptance.md)

旧 v3 文档在 `docs/v3-history/`，只作历史记录，不代表 v4 当前行为。
