import {
  appendFileSync,
  writeFileSync,
  renameSync,
  existsSync,
  readFileSync,
} from "node:fs";
import { EventEmitter } from "node:events";
export type EventKind =
  | "portfolio_refilled"
  | "generic_cp_cooldown"
  | "generic_cp_resumed"
  | "planner_turn_outcome"
  | "protocol_normalized"
  | "proposal_rebased"
  | "diversity_probe_decision"
  | "diversity_probe_unavailable"
  | "actual_search_result"
  | "resource_allocation"
  | "proposal_validation_feedback"
  | "proposal_refresh"
  | "proposal_fresh"
  | "quality_feedback"
  | "partial_completion_skipped"
  | "partial_completion_queued"
  | "partial_completion_finished"
  | "model_turn_partial"
  | "model_retry"
  | "algorithm_tool_call"
  | "algorithm_tool_result"
  | "tool_call"
  | "tool_result"
  | "repair_requested"
  | "repair_started"
  | "repair_answered"
  | "duplicate_skipped"
  | "run_started"
  | "memory_retrieved"
  | "model_started"
  | "model_activity"
  | "model_finished"
  | "model_wait_policy"
  | "model_failed"
  | "model_cancelled"
  | "planner_delivery"
  | "strategy_reused"
  | "strategy_batch_started"
  | "strategy_batch_evaluated"
  | "strategy_totals"
  | "proposal_rejected"
  | "proposal_normalized"
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
  /** Explicit resume closes unanswered tool calls as unknown, never as failed feasibility. */
  recoverInterruptedTools() {
    const pending = new Map<string, Record<string, unknown>>();
    if (!existsSync(this.file)) return;
    for (const line of readFileSync(this.file, "utf8").trim().split("\n")) {
      if (!line) continue;
      const e = JSON.parse(line) as RunEvent;
      if (e.kind === "tool_call") pending.set(String(e.data.call_id), e.data);
      if (e.kind === "tool_result") pending.delete(String(e.data.call_id));
    }
    for (const [call_id, data] of pending)
      this.send("tool_result", {
        call_id,
        action: data.action,
        strategy_id: data.strategy_id,
        status: "OUTCOME_UNKNOWN",
        valid: false,
        proof_scope: "none",
        reason:
          "Interrupted before a durable tool result; explicit resume rechecks existing candidates and may rerun read-only search tasks.",
      });
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
    case "planner_turn_outcome":
      console.log(
        `[模型交付] 第 ${d.call} 次：${d.status}；原因 ${d.reason}；候选 ${d.candidate_count} 个`,
      );
      break;
    case "proposal_rebased":
      console.log(
        "[策略接入] 订单及设备状态未变；策略执行时使用最新合法基线，无需重新思考",
      );
      break;
    case "portfolio_refilled":
      console.log(
        `[搜索任务池] 新增 ${d.added} 个不同范围的任务，排队 ${d.queued_cp} 个；种子轮次 ${Number(d.seed_pass ?? 0) + 1}${d.rolled_over ? "（已换轮）" : ""}；按收益分配执行`,
      );
      break;
    case "generic_cp_cooldown":
      console.log(
        `[搜索换向] ${d.direction} 范围连续 ${d.consecutive_no_gain} 次无新发布，暂缓约 ${Math.ceil(Number(d.remaining_seconds))} 秒；其他范围继续，不表示无解${d.applies_to_current_baseline ? "" : "（旧基线记录，不限制新基线）"}`,
      );
      break;
    case "generic_cp_resumed":
      console.log(`[搜索再探] ${d.direction} 范围冷却结束，允许新尝试`);
      break;
    case "proposal_refresh":
      console.log(
        `[物理状态更新] 订单、在制或设备输入发生变化；提交前重新核对策略。`,
      );
      break;
    case "quality_feedback":
      console.log(
        `[构造反馈] 合法候选 ${(d.details as any)?.candidate_minutes} 分钟，当前基线 ${(d.details as any)?.baseline_minutes} 分钟；反馈修正构造，暂停该策略的普通 CP（不表示无解）。`,
      );
      break;
    case "partial_completion_queued":
      console.log(
        `[CP补全排队] 已安排 ${d.scheduled_forges} 道锻打；使用当前部分安排作可修改提示；额度 ${d.seconds} 秒`,
      );
      break;
    case "partial_completion_finished":
      console.log(
        `[CP补全结果] ${d.valid ? "找到合法补全（不表示最优）" : d.status}；${d.valid ? "完整校验通过，首日 " + d.day_minutes + " 分钟" : "未交付完整方案，具体结果进入修正反馈"}`,
      );
      break;
    case "partial_completion_skipped":
      console.log(
        `[CP补全暂不执行] ${d.reason}；保留原始部分安排和原因，不表示无解`,
      );
      break;
    case "algorithm_tool_call":
      console.log(`[算法工具] ${d.id}：${d.name}`);
      break;
    case "algorithm_tool_result":
      console.log(
        `[算法工具结果] ${d.id}：${d.name}；${Number(d.elapsed_s).toFixed(2)} 秒；状态 ${d.valid === null ? "已读取" : d.valid ? "检查通过" : "未通过，已反馈"}`,
      );
      break;
    case "repair_requested":
      console.log(
        `[待修正] ${d.strategy_id}：${d.kind}；${d.message}；该策略暂停 CP，具体失败直接交给主模型`,
      );
      break;
    case "repair_started":
      console.log(
        `[模型修正] ${d.repair_id}，第 ${d.call} 次调用；正在读取原始失败与现场状态`,
      );
      break;
    case "repair_answered":
      console.log(
        `[修正已收到] ${d.strategy_id} ${d.name}；${d.hypothesis}；接下来重新构造验证`,
      );
      break;
    case "duplicate_skipped":
      console.log(
        `[重复任务跳过] ${d.strategy_id ?? "通用搜索"}；有效 CP 输入已执行或正在执行，不再重复求解`,
      );
      break;
    case "auxiliary":
      console.log(`[辅助模型] ${d.message}`);
      break;
    case "task_started":
      console.log(
        `[任务] ${d.action} ${d.name}${d.strategy_id ? `；策略 ${d.strategy_id}` : ""}；外层并行 ${d.active}`,
      );
      break;
    case "checkpoint":
      console.log(`[续跑] ${d.message}`);
      break;
    case "run_started":
      console.log(
        `[启动] ${d.orders} 单，第 ${d.day_index ?? 1} 天目标 ${d.target_minutes} 分钟；Memory ${d.memory_mode}`,
      );
      break;
    case "memory_retrieved":
      console.log(`[记忆] 检索到 ${d.count} 条相关经验；必须重新求解与校验`);
      break;
    case "model_wait_policy":
      if (d.effective_thinking_mode)
        console.log(
          d.thinking_budget
            ? `[模型配置] 显式思考预算 ${d.thinking_budget} token；不同时发送 effort。`
            : `[模型配置] effort=${d.effective_thinking_mode}。`,
        );
      console.log(
        `[模型等待] 单次上限 ${d.configured_timeout_s} 秒；本次实际最多 ${Number(d.effective_timeout_s).toFixed(1)} 秒（受整场剩余时间限制）${d.required ? "；要求至少执行一次主模型策略" : ""}`,
      );
      if (Number(d.effective_timeout_s) < Number(d.configured_timeout_s))
        console.log(
          "[模型等待] 整场剩余时间短于单次模型上限；若希望等满，请同时增大 --wall-seconds。",
        );
      break;
    case "model_failed":
      console.log(
        `[模型失败] 第 ${d.call} 次，${d.kind}：${d.reason}；诊断已保存，已有合法排程继续保留`,
      );
      break;
    case "model_cancelled":
      console.log(
        `[模型取消] 第 ${d.call} 次，整场停止原因 ${d.reason}；不是接口超时或策略校验失败`,
      );
      break;
    case "strategy_reused":
      console.log(
        `[策略接入] ${d.name}：${d.message}；方向 ${d.direction}，重排比例 ${Number(d.fraction) * 100}%`,
      );
      break;
    case "strategy_batch_started":
      console.log(
        `[策略批次] ${d.strategy_id} ${d.name}：正常批次最多先尝试 ${d.required_trials} 次；构造失败立即修正，完整审计通过后才进入 CP 改善`,
      );
      break;
    case "strategy_batch_evaluated":
      console.log(
        `[策略反馈] 第 ${d.round} 轮：${d.status}；本轮批次已结束，可进入下一轮思考`,
      );
      for (const x of d.strategies as any[])
        console.log(
          `[策略统计] ${x.strategy_id}：${x.completed_trials} 次（构造 ${x.construction_trials} / CP ${x.cp_trials}）；完整校验通过 ${x.audited_valid_trials} 次；累计实际改善 ${x.published_gain_minutes.toFixed(2)} 分钟；尝试耗时合计 ${x.summed_attempt_wall_s.toFixed(1)} 秒（并行耗时相加）`,
        );
      break;
    case "strategy_totals":
      for (const x of d.strategies as any[])
        console.log(
          `[策略累计] ${x.strategy_id} ${x.name}：${x.completed_trials} 次（构造 ${x.construction_trials} / CP ${x.cp_trials}）；完整合法 ${x.audited_valid_trials} 次；实际改善 ${x.published_gain_minutes.toFixed(2)} 分钟`,
        );
      break;
    case "planner_delivery":
      console.log(
        `[模型参与] 完整答复 ${d.completed_calls} 次；有新任务的策略 ${d.accepted_candidates} 个；已执行搜索尝试 ${d.evaluated_trials} 次（含策略后续任务）${d.required && !d.evaluated_trials ? "；本次未完成模型策略交付，不能声称已由主模型引导" : ""}`,
      );
      break;
    case "model_started":
      console.log(`[模型] 第 ${d.call} 次调用，正在选择搜索策略`);
      break;
    case "model_activity":
      console.log(
        `[${d.role ?? "模型"}] 已等待 ${d.wait_s}/${Number(d.timeout_s ?? 0).toFixed(0)} 秒；思考 ${d.thinking_chars ?? 0} 字符、正文 ${d.answer_chars ?? 0} 字符；${d.stage === "thinking" ? "仍在思考，尚未输出策略正文" : d.stage === "answer" ? "正在输出正文，完整返回并校验后执行" : "等待响应"}`,
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
        `[完整合法方案] 第 ${d.day_index ?? 1} 天 ${d.day_minutes} 分钟（${(Number(d.day_minutes) / 14.4).toFixed(2)}%），累计 ${e.elapsed_s.toFixed(1)} 秒`,
      );
      break;
    case "proposal_rejected":
      console.log(`[策略拒绝] ${d.reason}`);
      break;
    case "proposal_normalized":
      console.log(
        `[说明裁剪] ${d.fields}；仅缩短展示文字，完整答复已保存，可执行参数不变`,
      );
      break;
    case "error":
      console.log(`[错误] ${d.message}`);
      break;
    case "run_finished":
      console.log(`[结束] ${d.reason}；结果 ${d.output}`);
      break;
  }
}
