# 隆达排程 Harness v11

v11 修复 Memory 的执行阶段归因、经验退役和认证输入检查。服务器目录：`/home/huanzz/longda-strategy-harness-v11-20261020`。旧版 v10 与其数据库保留。工艺、设备、车辆、输入数据及完整校验器未改。

## 本版变化

1. **分阶段记录和检索**：`construct`（完整合法构造）、`cp_complete`（部分安排补全）、`cp_improve`（从完整基线继续改善）。额外隔离无基线 CP 首解和无法分类的旧记录，不混作改善。首次投料策略只自动复用完整审计通过的构造证据；CP 经验在获得基线后用于 CP 方向/范围建议。补全经验供主模型评估补全任务，不载入历史部分排程作为当前答案。
2. **归因保留实际范围**：记录启动基线、发布时基线、实际释放的订单、额度及提示来源。CP 经验的模型输入去掉构造器 engine/admission 等无因果依据的字段。同阶段的检索、执行、完整合法、改善分别计数，跨阶段引用不算直接复用成功。
3. **退役有检查门槛**：必须给理由、证据、原始成功事实。由新卡替代时，新卡必须启用、同阶段并保留旧条件和证据。声称证据错误/规则失效，需要程序核实的 issue 记录；模型自己不能制造这种记录。UNKNOWN 不构成退役依据。更新也不能悄悄删除旧成功证据。请求会被记录，缺依据则拒绝。
4. **旧卡审查与恢复**：保留旧卡及全部版本，未经核实的旧总结设为待复核、不主动送给模型；已核实的 858 全量 CP 改善卡、局部 CP 卡、构造保底卡启用。858 卡已恢复，恢复和后续澄清均保留版本历史。原始试验仍完整保留。
5. **辅助模型看见旧事实**：近期记录之外，附上每张送审卡所引用的原始结果。持久化文字中的 E1 等临时别名转换为稳定证据 ID，防止下一轮张冠李戴。文字引用必须列入该更新的证据清单。每条更新独立校验，坏记录不拖累合法记录。
6. **认证提前检查**：隐藏输入和 Node 入口都拒绝中文标点、空白及引号；不回显 Token，不自动删除字符或猜测修正。错误发生在启动搜索之前。不会靠加 timeout 处理认证错误。

v10 的协议处理、任务池、补全限额、双重校验、状态刷新区分及检查点能力继续保留。

## 运行示例

```bash
cd /home/huanzz/longda-strategy-harness-v11-20261020
.venv/bin/python start.py \
  --orders configs/orders20.json --vehicles 2 \
  --out "runs/orders20-car2-v11-$(date +%Y%m%d-%H%M%S)" \
  --model qwen3.8-max --thinking-budget 4096 --max-tokens 32768 \
  --summary-model qwen3.7-plus --memory-model qwen3.7-plus \
  --memory read-write --memory-content cards --memory-db data/experience-v11.sqlite \
  --cp-workers 6 --threads 8 --construct-workers 1 \
  --partial-completion on --completion-seconds 30 \
  --completion-priority value --diversity-probes on --exploration-share 0.15 \
  --construct-seconds 8 --solve-seconds 45 \
  --rounds 2 --strategy-trials 6 --repair-attempts 2 \
  --api-timeout 300 --wall-seconds 1200 \
  --target 870
```

Token 只通过隐藏输入/进程环境提供，不写配置。主模型、构造、CP、校验都计入整场墙钟额度。上面的命令是供人工启动的示例，不随部署自动运行。

## Memory 和恢复

- `data/experience-v11.sqlite` 是 v10 数据库的一致性副本，升级为 schema 4。不要把 v11 指向旧版数据库；迁移只对副本进行。
- `--memory off`：不读取或写入历史 Memory；通用构造、CP、主模型仍按各自参数运行。
- `--memory read-only --memory-content raw`：只读取按阶段区分的原始策略及试验，不提供经验卡，不运行更新器。
- `--memory read-only --memory-content cards`：在相同原始记录基础上额外把已复核经验卡交给主模型。
- `--memory read-write --memory-content cards`：读取上述内容，保存新记录，并允许辅助模型提出更新；程序校验后才写入。
- `--retrieval-limit 8` 是每个阶段最多 8 个候选，不是总共 8 条。
- 原始记录多、检索命中多，不代表收益高。首次合法时间、方案质量和重复失败都要对照；合法方案中更高的首日分数也不是因果证明。
- `data/evidence-index.json` 继承历史证据索引；旧目录保留供按证据 ID 读原结果，不注入旧完整排程。
- `--resume` 只能恢复本版同代码/同输入的运行目录；恢复合法基线与任务队列，不恢复 CP-SAT 内部搜索树。不要用 v11 接着跑 v10 的输出目录。

## 输出与统计

- `best.json` / `best/views/`：当前最好完整合法排程及订单/设备视角。
- `report.json`、`search-statistics.json`：构造、局部搜索、全量搜索、补全分别统计；合法结果数、发布数与尝试数分开。
- `tasks/*/attempt-*/request.json`：执行当时的基线证据、物理版本和实际范围。
- `tasks/*/attempt-*/edit-contrast.json`：修改及观察差异，默认 `confounded`，不假装已经做了单因素因果实验。
- `model-turns/`：协议反馈与执行结果；思考内容只计长度，不保存。
- `checkpoint.json`：包含任务池覆盖范围和种子轮次；当前基线附近已试完一轮范围后可以新种子继续，不能宣布全局最优。

`--portfolio-refill on|off`（默认 on）控制新增滚动任务池；off 保留原有连续改善链，协议、诊断和经验改进仍生效。短测未证明新增池普遍提高分数，可用此参数做同版本对照。

资源分配的 cost 是任务墙钟×配置线程数的估算槽位秒，不是 OS 实测 CPU 秒。并行任务时长之和也不等于整场墙钟。

## 验证

```bash
npm test
.venv/bin/python -m unittest discover -s python -p 'test_*.py'
.venv/bin/python scripts/memory_ablation_v11.py --tag check-offline
.venv/bin/python scripts/memory_ablation_v11.py --live --tag check-live
```

离线对照三种设置各两次，每次 30 秒，顺序正反各一轮。模型关闭时，raw/cards 的差别不能证明经验摘要价值。真实模型对照每种一次、每次 100 秒，主模型及摘要的墙钟都计入；更新器关闭，避免对照组相互污染。使用统一只读 Memory 快照，不用旧排程答案。它们仅供人工启动，不会在部署后自动运行。短测不是泛化或最优性证明。

详见 `docs/acceptance-v11.md`。旧版验收文档保留作为历史记录，不代表 v11 当前结果。
