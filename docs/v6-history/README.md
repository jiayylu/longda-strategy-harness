# 隆达排程算法 Harness v6

服务器：`/home/huanzz/longda-strategy-harness-v6-20261019`。旧 v5/v4 保留。

本版改进构造执行效率和模型工具闭环；工艺、炉容量、温度、压机、车辆、交期与双重校验不变。默认先从当前订单重新生成混合工艺批次，尝试快速构造，再保留按需展开的资源日历搜索。不是读取历史排程答案。

## 运行

```bash
cd /home/huanzz/longda-strategy-harness-v6-20261019
.venv/bin/python start.py \
  --orders configs/orders20.json --vehicles 1 \
  --out runs/car1-v6-new \
  --model qwen3.8-max --effort low --max-tokens 8192 \
  --summary-model qwen3.7-plus --memory-model qwen3.7-plus \
  --memory read-write --memory-db data/experience-v6.sqlite \
  --cp-workers 1 --threads 8 --construct-workers 1 \
  --rounds 1 --strategy-trials 2 --repair-attempts 1 \
  --construct-seconds 8 --solve-seconds 10 \
  --api-timeout 90 --wall-seconds 120 --target 600
```

密钥只从进程环境或隐藏输入读取，不写日志或文件。每次用新的输出目录；恢复同一运行需显式 `--resume`，必须保持订单、规则、代码和目标一致。整场时间包含模型、构造、CP、工具与审计；各环节可能并行，不能把其耗时简单相加。主模型不保证在一个短回合内产生完整排程。

首次只要看合法方案，可关闭模型和 CP：

```bash
.venv/bin/python start.py --orders configs/orders20.json --vehicles 1 \
  --out runs/car1-first-new --model off --aux-model off --memory off \
  --cp-workers 0 --construct-workers 1 --rounds 0 \
  --construct-seconds 8 --wall-seconds 25 --target 1
```

这里 `target 1` 仅用于首解试验：完整合法排程达到至少1分钟当日锻打后结束，不表示生产目标。

## 主要变化

| 部分 | v6 做法 |
|---|---|
| 测量 | 分组件累计/独占耗时、峰值 RSS、单次最大候选数、状态数量、检查点字节数 |
| 默认冷启动 | 按当前订单推导混合工艺批次；`--legacy-bootstrap` 可关闭此优先入口 |
| 分支 | 按需生成子状态，未选候选保留；不隐藏删掉前四名以外的分支 |
| 状态存储 | 父状态＋动作记录；只淘汰可重建的实体缓存，句柄和待探索分支保留 |
| 规则检查 | 检查受影响工序、炉和车；压机/交换等相关关系仍检查；可开启与原全量检查逐动作对照 |
| 候选 | 相同状态和参数复用候选；已签发且通过检查的动作不再重复全量检查 |
| CP 前缀检查 | 同一个环境内复用基础模型再克隆；不声称复用了 CP 的内部搜索树；UNKNOWN 不剪枝 |
| 执行额度 | 子进程保护、Linux 时间/内存额度；逐指令跟踪默认关闭，可用 LONGDA_ALGORITHM_TRACE=opcode 对照 |
| 模型闭环 | JSON 错误反馈、有限瞬时故障重试、工具额度结束保留成果、最后一次提交机会 |
| 工具去重 | 代码、当前订单/在制、环境版本、偏好和执行额度组成身份；rerun=true 是显式新尝试 |
| 策略归因 | 工具试跑与正式任务使用同一种结果统计，重复回执不重复执行或记功 |
| 恢复 | 模型回合日志、工具回执、算法控制器和可重建状态、最好完整排程分别保存 |

`LONGDA_ALGORITHM_MEMORY_MB` 默认2048：Linux 生成算法开始时，在已加载运行时的虚拟内存基础上增加的额度；不是整台机器配置，也不等同 RSS。原完整审计不被删减。候选仍是关键事件加显式延迟，并非全部排程穷举。

## 目录

- `src/cli.ts`：编排构造、模型、CP、反馈和输出。
- `src/client.ts`：流式调用、结构化反馈、回合保存及有限重试。
- `src/algorithm_tools.ts` / `tool_receipts.ts`：模型算法工具、去重与原始回执。
- `src/strategy_cycles.ts` / `learning.ts`：策略归因与资源分配。
- `src/memory.ts` / `auxiliary.ts`：多候选经验与独立摘要/记忆更新，延续原实现。
- `python/default_algorithm.py`：可被模型读取和替换的完整默认构造程序。
- `python/calendar_engine.py`：不可由模型修改的资源状态和动作 API。
- `python/state_store.py` / `runtime_metrics.py`：可重建状态与性能测量。
- `python/algorithm.py`：受限代码校验、执行与检查点。
- `python/physical_model.py`、`verification.py`、`production_state.py`、`scoring.py`、`reference/`：冻结规则、观测状态与完整审计。
- `runs/`：每次输入、模型输出、工具、策略、审计、最好排程和 HTML。

## 验收

详见 `docs/acceptance.md` 和 `docs/acceptance.json`。不同测试回答不同问题：

1. 相同动作回放，比较执行引擎开销。
2. 相同快速分组代码，比较构造与完整审计总耗时。
3. 新旧默认算法，评估默认选择带来的首解差异。
4. 实际启动入口的首解时间。
5. 两版各一次真实模型失败修订，包含思考时间；不能用程序测试替代模型效果。

真实模型短测未找到新的完整方案，不宣称模型搜索已优于原构造器。所有测试有限运行，交付时无持续搜索。
