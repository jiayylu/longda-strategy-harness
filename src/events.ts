import {
  appendFileSync,
  writeFileSync,
  renameSync,
  existsSync,
  readFileSync,
} from "node:fs";
import { EventEmitter } from "node:events";
export type EventKind =
  | "run_started"
  | "memory_retrieved"
  | "model_started"
  | "model_activity"
  | "model_finished"
  | "proposal_rejected"
  | "construction_finished"
  | "solve_started"
  | "solve_progress"
  | "solve_finished"
  | "incumbent_updated"
  | "run_finished"
  | "error"
  | "auxiliary"
  | "checkpoint"
  | "task_started"
  | "task_finished";
export interface RunEvent {
  schema_version: 1;
  run_id: string;
  seq: number;
  at: string;
  elapsed_s: number;
  kind: EventKind;
  data: Record<string, unknown>;
}
export function save(file: string, data: unknown) {
  writeFileSync(file + ".tmp", JSON.stringify(data, null, 2));
  renameSync(file + ".tmp", file);
}
export class Events extends EventEmitter {
  private seq = 0;
  constructor(
    private file: string,
    readonly runId: string,
    readonly started: number,
  ) {
    super();
    if (existsSync(file)) {
      const lines = readFileSync(file, "utf8").trim().split("\n");
      this.seq = Number(JSON.parse(lines[lines.length - 1] || "{}").seq ?? 0);
    }
  }
  send(kind: EventKind, data: Record<string, unknown> = {}) {
    const event: RunEvent = {
      schema_version: 1,
      run_id: this.runId,
      seq: ++this.seq,
      at: new Date().toISOString(),
      elapsed_s: (performance.now() - this.started) / 1000,
      kind,
      data,
    };
    appendFileSync(this.file, JSON.stringify(event) + "\n");
    this.emit("event", event);
    return event;
  }
}
export function terminal(e: RunEvent) {
  const d = e.data;
  switch (e.kind) {
    case "auxiliary":
      console.log(`[辅助模型] ${d.message}`);
      break;
    case "task_started":
      console.log(`[任务] ${d.action} ${d.name}；外层并行 ${d.active}`);
      break;
    case "checkpoint":
      console.log(`[续跑] ${d.message}`);
      break;
    case "run_started":
      console.log(
        `[启动] ${d.orders} 单，首日目标 ${d.target_minutes} 分钟；Memory ${d.memory_mode}`,
      );
      break;
    case "memory_retrieved":
      console.log(`[记忆] 检索到 ${d.count} 条相关经验；必须重新求解与校验`);
      break;
    case "model_started":
      console.log(`[模型] 第 ${d.call} 次调用，正在选择搜索策略`);
      break;
    case "model_activity":
      console.log(
        `[模型] 已等待 ${d.wait_s} 秒，收到 ${d.chars} 个字符；尚未返回可执行策略`,
      );
      break;
    case "model_finished":
      console.log(
        `[模型] 策略答复已收到，用时 ${Number(d.seconds).toFixed(1)} 秒`,
      );
      break;
    case "construction_finished":
      console.log(
        `[构造] ${d.name}：${d.trials} 次尝试，${d.complete} 个完整候选；完整双重校验后可发布`,
      );
      break;
    case "solve_started":
      console.log(
        `[CP-SAT] ${d.mode}，${d.threads} 线程，本次额度 ${d.seconds} 秒；完整四天规则`,
      );
      break;
    case "solve_progress":
      console.log(`[CP-SAT] ${d.phase}，本次已运行 ${d.seconds} 秒`);
      break;
    case "solve_finished":
      console.log(
        `[CP-SAT] ${d.status}${d.scope === "local" ? "（仅当前局部范围）" : ""}；${d.valid ? "完整审计通过" : "没有可交付的新结果"}；${d.day_minutes ?? "—"} 分钟`,
      );
      break;
    case "incumbent_updated":
      console.log(
        `[完整合法方案] 首日 ${d.day_minutes} 分钟（${(Number(d.day_minutes) / 14.4).toFixed(2)}%），累计 ${e.elapsed_s.toFixed(1)} 秒`,
      );
      break;
    case "proposal_rejected":
      console.log(`[策略拒绝] ${d.reason}`);
      break;
    case "error":
      console.log(`[错误] ${d.message}`);
      break;
    case "run_finished":
      console.log(`[结束] ${d.reason}；结果 ${d.output}`);
      break;
  }
}
