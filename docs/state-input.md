# 在制状态：输入与约束

状态进入实际构造器、CP-SAT 和独立校验器。Memory 只保存其摘要特征；主模型看到当前承诺与摘要，不接收完整未来排程。

## 两种输入方式

- 订单 JSON 内含 `production_state`。
- `--orders orders.json --state snapshot.json`，后者覆盖订单文件中的状态。

当前模型以原计划起点为 0、每分钟 3 ticks。`now_ticks=150` 是第 50 分钟。不是从当前时刻重新把时间清零；订单交期与历史记录共用同一轴。

```json
{
  "schema_version": 1,
  "cut": "before_events",
  "now_ticks": 6,
  "operations": [
    {"order":"C430-01","fire":1,"heat_start":15,"furnace":0}
  ],
  "transports": [
    {"kind":"cold_to_furnace","order":"C430-01","fire":1,
     "source":0,"destination":1,"start":0,"end":15,"vehicle":0}
  ],
  "furnaces": [
    {"id":0,"temperature":1150},{"id":1,"temperature":1150},
    {"id":2,"temperature":1150},{"id":3,"temperature":1150},
    {"id":4,"temperature":1150},{"id":5,"temperature":1150}
  ],
  "vehicles": [
    {"id":0,"location":1,"available_at_ticks":15},
    {"id":1,"location":0,"available_at_ticks":6}
  ]
}
```

含义：当前第 2 分钟，车 0 已在第 0 分钟带 C430-01 出发，确定第 5 分钟到 F1，因此该入炉时刻属于已开始动作的确定结果。其他未来任务尚未指定。车 1 仍空载在冷料区。

## 字段与编号

| 字段 | 内容 |
|---|---|
| `operations` | 已观察工序的已知字段：order、fire、heat_start、heat_end、forge_start、forge_end、furnace；未知字段省略 |
| `transports` | 已开始载料运输，start 必须严格小于 now_ticks；保存实际车辆、来源、目的地及确定终点 |
| `furnaces` | 全部六炉；id 从 0 到 5；当前实测温度可为整数或有理数字符串 |
| `vehicles` | 全部车辆；id 从 0 起；location 与 available_at_ticks 描述当前已承诺移动结束后的可用地点/时刻 |
| `empty_move` | 空车正在移动时附在该车：source、destination、start、end；起点已发生且终点未发生 |

**两种编号不能混淆：**工序中的 `furnace=0` 表示 F1；运输地点则为 `0=冷料区，1…6=F1…F6，7=压机，8=成品区`。车辆均 0 起编号。

运输 kind：cold_to_furnace、furnace_to_press、press_to_furnace、press_to_finished。`fire` 指本次运输关联的锻打火次：press_to_furnace 对应刚完成的火次，确定下一火的入炉。

## 状态怎样进入求解

1. 根据已开始运输重建已知入炉/出炉/锻打事实，核对 operations 一致性，拒绝额外塞入未开始的未来时刻。
2. 检查车链、保温上下限、炉容量/温度、锻后是否已按时接走。明显矛盾直接报输入错误；不当作搜索难题。
3. 在完整原模型上固定这些事实，未开始运输不得早于当前时刻；当前车的位置/可用时刻和炉温限制后续动作。
4. 保温中的料沿用已累计时间；锻打中不能重排已开始加工；车上材料按已承诺目的地到达。后续火次、选炉、车辆、时间仍由构造/CP 决定。
5. 完整输出再次与原事实逐项比对，并通过原物理审计和完整模型验算。

`before_events` 表示在当前时刻的事件前切片：恰好此刻出发的任务尚未发生，可以重排；先前已出发的任务不能撤销。区间端点采用不重叠的事件顺序处理。已经发生历史的订单保留身份，不能像冷启动同类订单那样随意互换。

## 当前日评分

若每分钟刻度为 s=3，则统计窗起点为 `floor(now_ticks/(1440*s))*1440*s`，长度 `1440*s`。逐道锻打取与此窗的重叠；过去已发生部分固定。到第二天续排时，窗移动，炉、车、料的状态不会清零。

## 测试快照与真实接入的区别

`python/state_cli.py snapshot` 可以从**已经校验的执行轨迹**截取测试状态，只导出已经开始的动作与可观测设备状态。测试脚本保留完整源轨迹作为测试证据，但查询、Memory 和主模型都不加载该轨迹的未执行后半段。

目前输入要求已执行装取记录，尚未提供仅凭不完整 MES 快照推断历史的接口。设备故障、未建模的暂停或工艺修改也不能靠伪造历史来适配，应另行增加明确的状态/规则版本。
