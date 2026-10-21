import { existsSync, mkdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { save } from "./events.js";
import { hash } from "./memory.js";
import type { Events } from "./events.js";
import { modelJson } from "./model_json.js";
import { normalizeEnvelope } from "./protocol.js";
import { requiresRefresh } from "./live_state.js";
import { validateToken } from "./credentials.js";

export interface ModelDiagnostics {
  role: string;
  requested_model: string;
  response_model: string;
  timeout_s: number;
  elapsed_s: number;
  thinking_chars: number;
  answer_chars: number;
  stage: "waiting" | "thinking" | "answer" | "complete";
  stop_reason?: string;
  usage: Record<string, unknown>;
  // Only visible answer text is retained. Thinking content is counted, never stored.
  answer_text: string;
}
export class ModelCallError extends Error {
  constructor(
    message: string,
    readonly code:
      | "api_timeout"
      | "cancelled"
      | "output_limit"
      | "incomplete"
      | "request_error",
    readonly diagnostics: ModelDiagnostics,
  ) {
    super(message);
    this.name = "ModelCallError";
  }
}
export class ModelClient {
  requestCount = 0;
  latestState?: () => any;
  validateProposal?: (text: string) => unknown;
  turnDirectory?: string;
  outputSchema?: Record<string, unknown>;
  toolRuntime?: {
    execute(name: string, args: any, signal: AbortSignal): Promise<any>;
  };
  constructor(
    readonly base: string,
    readonly model: string,
    private token: string,
    readonly effort: string,
    readonly maxTokens: number,
    readonly thinkingBudget?: number,
    readonly role = "planner",
  ) {
    this.token = validateToken(token);
  }
  async generate(
    system: string,
    payload: unknown,
    seconds: number,
    events: Events,
    signal: AbortSignal,
    onProgress?: (diagnostics: ModelDiagnostics) => void,
  ): Promise<{
    text: string;
    usage: Record<string, unknown>;
    response_model: string;
    stop_reason: string;
    wall_s: number;
    tool_steps?: number;
    turn_status?: string;
    delivery_reason?: string;
    delivery_diagnostics?: any;
    partial_results?: any[];
  }> {
    const started = performance.now();
    const key = hash({
      system,
      payload,
      model: this.model,
      effort: this.effort,
      maxTokens: this.maxTokens,
      thinkingBudget: this.thinkingBudget,
    });
    const file = this.turnDirectory
      ? join(this.turnDirectory, key + ".json")
      : null;
    if (this.turnDirectory) mkdirSync(this.turnDirectory, { recursive: true });
    const old =
      file && existsSync(file) ? JSON.parse(readFileSync(file, "utf8")) : null;
    const history: any[] = old && old.state !== "COMPLETED" ? old.history : [];
    const acceptedCandidates = new Map<string, any>(
      (old?.accepted_candidates ?? []).map((c: any) => [c.name, c]),
    );
    let acceptedState = old?.accepted_state ?? this.latestState?.();
    let refreshes = 0;
    let repeatedRead = false;
    let toolSteps = history.filter((x) => x.tool_call).length,
      formatErrors = old?.format_errors ?? 0,
      transportRetries = old?.transport_retries ?? 0;
    let last: any = {
      text: "",
      usage: {},
      response_model: this.model,
      stop_reason: "budget_limited",
    };
    const persist = (state: string) => {
      if (file)
        save(file, {
          state,
          key,
          history,
          tool_steps: toolSteps,
          format_errors: formatErrors,
          transport_retries: transportRetries,
          accepted_candidates: [...acceptedCandidates.values()],
          accepted_state: acceptedState,
          updated_at: Date.now(),
        });
    };
    const diagnostics = () => ({
      protocol_errors: formatErrors,
      physical_refreshes: refreshes,
      transport_retries: transportRetries,
      tool_steps: toolSteps,
    });
    const partial = (reason = "turn_budget_exhausted") => {
      persist("BUDGET_LIMITED");
      if (
        acceptedCandidates.size &&
        !requiresRefresh(acceptedState, this.latestState?.())
      )
        return {
          ...last,
          text: JSON.stringify({
            summary: "Valid candidates retained after correction allowance",
            candidates: [...acceptedCandidates.values()],
          }),
          turn_status: "COMPLETED",
          delivery_reason: "retained_valid_candidates",
          delivery_diagnostics: diagnostics(),
          partial_results: history,
          tool_steps: toolSteps,
          wall_s: (performance.now() - started) / 1000,
        };
      events.send("model_turn_partial", {
        tool_steps: toolSteps,
        key,
        artifact: file,
        reason,
        diagnostics: diagnostics(),
      });
      return {
        ...last,
        text: JSON.stringify({
          summary: "Turn budget ended; saved results remain available",
          candidates: [],
        }),
        turn_status: "BUDGET_LIMITED",
        delivery_reason: reason,
        delivery_diagnostics: diagnostics(),
        partial_results: history,
        tool_steps: toolSteps,
        wall_s: (performance.now() - started) / 1000,
      };
    };
    for (let request = 0; request < 11; request++) {
      signal.throwIfAborted();
      const left = seconds - (performance.now() - started) / 1000;
      if (left <= 0) return partial();
      const finalOnly =
        toolSteps >= 6 ||
        request >= 8 ||
        repeatedRead ||
        (toolSteps >= 2 && left < Math.min(60, seconds * 0.5));
      // Preserve exact current errors and the last two results. Old receipts remain retrievable.
      const compact = history.map((row, i) =>
        i >= history.length - 2 || JSON.stringify(row).length <= 5000
          ? row
          : {
              tool_call: row.tool_call,
              tool_result: {
                trial_id: row.tool_result?.trial_id,
                error: row.tool_result?.error,
                failure: row.tool_result?.failure,
                algorithm_hash: row.tool_result?.algorithm_hash,
                note: "Read the original via inspect_trial",
              },
            },
      );
      const snapshot = this.latestState?.();
      const currentPayload = snapshot
        ? {
            ...(payload as any),
            best_complete: snapshot.best_complete,
            latest_state: snapshot,
          }
        : payload;
      const pinned = new Map<string, any>();
      for (const row of history) {
        const call = row.tool_call;
        if (
          call?.name === "read_guide" ||
          (call?.name === "read_algorithm" &&
            call.arguments?.name !== "calendar_environment")
        )
          pinned.set(hash(call), {
            tool_call: call,
            tool_result: row.tool_result,
          });
      }
      const requestPayload =
        this.latestState || this.toolRuntime
          ? {
              ...(currentPayload as any),
              working_reference_sources: [...pinned.values()].slice(-2),
              delivery_budget: {
                remaining_turn_s: left,
                remaining_tool_steps: Math.max(0, 6 - toolSteps),
                next_action: finalOnly
                  ? "Submit one compact executable proposal using the available sources now; no more reads."
                  : "Reserve time for final JSON. Reuse provided source; do not repeatedly read unchanged files. Test/edit only what the evidence requires.",
              },
            }
          : currentPayload;
      persist("IN_PROGRESS");
      if (file && snapshot)
        save(file.replace(/\.json$/, `-request-${request}.json`), {
          system,
          payload: requestPayload,
          history: compact,
        });
      let answer: any;
      try {
        answer = await this.generateOnce(
          system +
            (finalOnly
              ? "\nTool allowance exhausted. Return final proposal only; tool_calls must be empty."
              : ""),
          history.length || finalOnly
            ? {
                task: requestPayload,
                tool_history: compact,
                final_proposal_only: finalOnly,
              }
            : requestPayload,
          left,
          events,
          signal,
          onProgress,
          finalOnly,
        );
      } catch (e) {
        const retry =
          e instanceof ModelCallError &&
          e.code === "request_error" &&
          /429|50[0234]|fetch failed|ECONNRESET|network/i.test(String(e));
        if (
          retry &&
          transportRetries++ < 1 &&
          seconds - (performance.now() - started) / 1000 > 1
        ) {
          events.send("model_retry", {
            key,
            attempt: transportRetries,
            reason: String(e),
            delay_ms: 250,
          });
          persist("BUDGET_LIMITED");
          await new Promise<void>((resolve) => {
            const timer = setTimeout(resolve, 250);
            signal.addEventListener(
              "abort",
              () => {
                clearTimeout(timer);
                resolve();
              },
              { once: true },
            );
          });
          continue;
        }
        persist("BUDGET_LIMITED");
        if (
          e instanceof ModelCallError &&
          ["api_timeout", "output_limit", "incomplete"].includes(e.code) &&
          history.some(
            (r) =>
              r.tool_call?.name === "run_algorithm" && r.tool_result?.trial_id,
          )
        ) {
          history.push({
            delivery_warning: e.code,
            diagnostics: e.diagnostics,
            instruction:
              "Tool results remain valid independent of the incomplete final response; inspect before resuming.",
          });
          return partial(e.code);
        }
        throw e;
      }
      last = answer;
      let parsed: any;
      try {
        const wire: any = modelJson(answer.text);
        parsed = normalizeEnvelope(wire, Boolean(this.toolRuntime));
        if (Array.isArray(wire.tool_calls) && wire.tool_calls.length === 0)
          events.send("protocol_normalized", {
            removed: ["tool_calls"],
            reason: "empty_wire_metadata",
          });
      } catch (e) {
        history.push({
          protocol_error: "INVALID_JSON",
          message: String(e),
          answer_excerpt: answer.text.slice(0, 1600),
          instruction:
            "Return one complete schema-valid JSON. Do not execute truncated code.",
        });
        if (++formatErrors > 2) return partial("protocol_rejected");
        continue;
      }
      if (!parsed.tool_calls?.length || !this.toolRuntime) {
        const latest = this.latestState?.();
        if (parsed.candidates?.length && requiresRefresh(snapshot, latest)) {
          events.send("proposal_refresh", {
            before_physical_revision: snapshot?.physical_revision,
            after_physical_revision: latest?.physical_revision,
            before: snapshot?.search_revision,
            after: latest?.search_revision,
            before_minutes: snapshot?.best_complete?.day_minutes,
            after_minutes: latest?.best_complete?.day_minutes,
          });
          history.push({
            stale_proposal: parsed,
            latest_state: latest,
            instruction:
              "Physical orders/WIP/equipment changed. Adapt this proposal to latest_state. Incumbent improvement alone never requires a new proposal.",
          });
          if (
            ++refreshes > 2 ||
            seconds - (performance.now() - started) / 1000 <= 1
          )
            return partial("physical_refresh_exhausted");
          continue;
        }
        if (parsed.candidates?.length && this.validateProposal) {
          try {
            if (acceptedCandidates.size) {
              const combined = new Map(acceptedCandidates);
              for (const c of parsed.candidates) combined.set(c.name, c);
              parsed.candidates = [...combined.values()];
            }
            this.validateProposal(JSON.stringify(parsed));
          } catch (error) {
            for (const c of (error as any).accepted ?? []) {
              const retained = new Map(acceptedCandidates);
              retained.set(c.name, c);
              try {
                this.validateProposal(
                  JSON.stringify({
                    summary: "Retained valid candidates",
                    candidates: [...retained.values()],
                  }),
                );
                acceptedCandidates.set(c.name, c);
                acceptedState = latest;
              } catch {
                /* Original result remains in history; dispatch still obeys proposal limits. */
              }
            }
            const details = String(error);
            events.send("proposal_validation_feedback", {
              details,
              attempt: formatErrors + 1,
            });
            history.push({
              protocol_error: "PROPOSAL_VALIDATION",
              details,
              invalid_proposal: parsed,
              instruction:
                "Correct these exact errors in this SAME turn. Preserve valid work, current orders and hard rules. Use issued evidence IDs verbatim; do not rerun successful tools.",
            });
            if (++formatErrors > 2) return partial("protocol_rejected");
            continue;
          }
        }
        if (acceptedCandidates.size && parsed.candidates?.length) {
          for (const c of parsed.candidates) acceptedCandidates.set(c.name, c);
          parsed.candidates = [...acceptedCandidates.values()];
          this.validateProposal?.(JSON.stringify(parsed));
          answer.text = JSON.stringify(parsed);
        }
        if (snapshot)
          events.send("proposal_fresh", {
            request_revision: snapshot.search_revision,
            current_revision: latest?.search_revision,
            refreshes,
          });
        answer.text = JSON.stringify(parsed);
        if (
          snapshot?.search_revision !== latest?.search_revision &&
          !requiresRefresh(snapshot, latest)
        )
          events.send("proposal_rebased", {
            request_revision: snapshot?.search_revision,
            current_revision: latest?.search_revision,
            instruction:
              "bind latest audited incumbent at execution; no physical changes",
          });
        persist("COMPLETED");
        return {
          ...answer,
          wall_s: (performance.now() - started) / 1000,
          tool_steps: toolSteps,
          delivery_reason:
            Array.isArray(parsed.candidates) && !parsed.candidates.length
              ? history.some((x) => x.tool_call)
                ? "tools_only"
                : "empty_proposal"
              : "delivered",
          delivery_diagnostics: diagnostics(),
          turn_status:
            Array.isArray(parsed.candidates) &&
            parsed.candidates.length === 0 &&
            history.some((x) => x.tool_call)
              ? "TOOLS_COMPLETED"
              : "COMPLETED",
          partial_results: history,
        };
      }
      if (parsed.candidates?.length)
        throw Error(
          "A response cannot both execute a tool and submit candidates",
        );
      if (finalOnly) return partial("tool_allowance_exhausted");
      if (!Array.isArray(parsed.tool_calls) || parsed.tool_calls.length !== 1) {
        history.push({
          protocol_error: "ONE_TOOL_REQUIRED",
          instruction: "Use one algorithm tool per step",
        });
        continue;
      }
      const call = parsed.tool_calls[0];
      const duplicateRead =
        ["read_algorithm", "read_guide"].includes(call.name) &&
        history.find(
          (r) =>
            r.tool_call?.name === call.name &&
            hash(r.tool_call.arguments ?? {}) === hash(call.arguments ?? {}),
        );
      if (duplicateRead) {
        repeatedRead = true;
        history.push({
          protocol_error: "UNCHANGED_SOURCE_ALREADY_AVAILABLE",
          tool_call: call,
          tool_result: duplicateRead.tool_result,
          instruction:
            "Identical immutable source was already read and is pinned. Submit a compact proposal now; do not read it again.",
        });
        events.send("duplicate_skipped", {
          action: call.name,
          reason: "unchanged_read",
          trial_id: duplicateRead.tool_result?.trial_id,
        });
        continue;
      }
      const result = await this.toolRuntime.execute(
        call.name,
        call.arguments ?? {},
        signal,
      );
      history.push({ tool_call: call, tool_result: result });
      toolSteps++;
      persist("BUDGET_LIMITED");
    }
    return partial("request_limit_exhausted");
  }
  private async generateOnce(
    system: string,
    payload: unknown,
    seconds: number,
    events: Events,
    signal: AbortSignal,
    onProgress?: (diagnostics: ModelDiagnostics) => void,
    finalOnly = false,
  ) {
    this.requestCount++;
    const started = performance.now();
    let text = "",
      thinkingChars = 0,
      usage: Record<string, unknown> = {},
      stopReason: string | undefined,
      responseModel = "";
    const safe = (s: string) => s.split(this.token).join("[REDACTED]");
    const diagnostics = (): ModelDiagnostics => ({
      role: this.role,
      requested_model: this.model,
      response_model: responseModel,
      timeout_s: seconds,
      elapsed_s: (performance.now() - started) / 1000,
      thinking_chars: thinkingChars,
      answer_chars: text.length,
      stage:
        stopReason === "end_turn"
          ? "complete"
          : text
            ? "answer"
            : thinkingChars
              ? "thinking"
              : "waiting",
      stop_reason: stopReason,
      usage,
      answer_text: safe(text),
    });
    let lastSaved = -Infinity;
    const progress = (force = false) => {
      if (force || performance.now() - lastSaved >= 10000) {
        lastSaved = performance.now();
        onProgress?.(diagnostics());
      }
    };
    const timer = setInterval(() => {
      const d = diagnostics();
      events.send("model_activity", {
        wait_s: Math.round(d.elapsed_s),
        chars: thinkingChars + text.length,
        thinking_chars: thinkingChars,
        answer_chars: text.length,
        stage: d.stage,
        timeout_s: seconds,
        role: this.role,
      });
      progress(true);
    }, 30000);
    const deadline = new AbortController();
    const deadlineTimer = setTimeout(
      () => deadline.abort(),
      Math.max(1, seconds * 1000),
    );
    const combined = AbortSignal.any([signal, deadline.signal]);
    const body = {
      model: this.model,
      max_tokens: this.maxTokens,
      stream: true,
      system,
      messages: [{ role: "user", content: JSON.stringify(payload) }],
      thinking: {
        type: this.effort === "off" ? "disabled" : "enabled",
        ...(this.effort !== "off" && this.thinkingBudget
          ? { budget_tokens: this.thinkingBudget }
          : {}),
      },
      ...((this.effort !== "off" && !this.thinkingBudget) || this.outputSchema
        ? {
            output_config: {
              ...(this.effort !== "off" && !this.thinkingBudget
                ? { effort: this.effort }
                : {}),
              ...(this.outputSchema
                ? {
                    format: {
                      type: "json_schema",
                      schema: finalOnly
                        ? {
                            ...this.outputSchema,
                            properties: {
                              ...(this.outputSchema.properties as any),
                              tool_calls: {
                                ...(this.outputSchema.properties as any)
                                  .tool_calls,
                                maxItems: 0,
                              },
                            },
                          }
                        : this.outputSchema,
                    },
                  }
                : {}),
            },
          }
        : {}),
    };
    try {
      progress(true);
      combined.throwIfAborted();
      const r = await fetch(this.base.replace(/\/$/, "") + "/v1/messages", {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "anthropic-version": "2023-06-01",
          Authorization: "Bearer " + this.token,
        },
        body: JSON.stringify(body),
        signal: combined,
      });
      if (!r.ok)
        throw Error(
          `Model HTTP ${r.status}: ${safe((await r.text()).slice(0, 1500))}`,
        );
      if (!r.body) throw Error("No response stream");
      const consume = (event: Record<string, any>) => {
        if (event.type === "error") throw Error(safe(JSON.stringify(event)));
        if (event.type === "message_start") {
          usage = { ...usage, ...event.message?.usage };
          responseModel = event.message?.model ?? "";
        }
        if (event.type === "content_block_start") {
          if (event.content_block?.type === "text")
            text += event.content_block.text ?? "";
          if (event.content_block?.type === "thinking")
            thinkingChars += String(event.content_block.thinking ?? "").length;
        }
        if (event.type === "content_block_delta") {
          const d = event.delta ?? {};
          if (d.type === "thinking_delta")
            thinkingChars += String(d.thinking ?? "").length;
          if (d.type === "text_delta") text += d.text ?? "";
        }
        if (event.type === "message_delta") {
          usage = { ...usage, ...event.usage };
          stopReason = event.delta?.stop_reason ?? stopReason;
        }
        progress();
      };
      if (!r.headers.get("content-type")?.includes("text/event-stream")) {
        const data = (await r.json()) as any;
        text = (data.content ?? [])
          .filter((b: any) => b.type === "text")
          .map((b: any) => b.text)
          .join("\n");
        thinkingChars = (data.content ?? [])
          .filter((b: any) => b.type === "thinking")
          .reduce(
            (n: number, b: any) => n + String(b.thinking ?? "").length,
            0,
          );
        usage = data.usage ?? {};
        stopReason = data.stop_reason;
        responseModel = data.model ?? "";
      } else {
        const decoder = new TextDecoder();
        let buffer = "";
        const reader = r.body.getReader();
        try {
          // A terminal stop_reason is sufficient. Do not wait for a proxy to close its socket.
          while (!stopReason) {
            combined.throwIfAborted();
            const { value, done } = await reader.read();
            if (done) {
              buffer += decoder.decode();
              break;
            }
            buffer += decoder.decode(value, { stream: true });
            buffer = buffer.replace(/\r\n/g, "\n");
            let end;
            while ((end = buffer.indexOf("\n\n")) >= 0) {
              const frame = buffer.slice(0, end);
              buffer = buffer.slice(end + 2);
              const data = frame
                .split("\n")
                .filter((x) => x.startsWith("data:"))
                .map((x) => x.slice(5).trim())
                .join("\n");
              if (data && data !== "[DONE]") consume(JSON.parse(data));
              if (stopReason) break;
            }
          }
          if (!stopReason && buffer.trim()) throw Error("Incomplete SSE frame");
        } finally {
          await reader.cancel().catch(() => {});
          reader.releaseLock();
        }
      }
      if (stopReason !== "end_turn")
        throw Error(
          `Model answer incomplete (${stopReason ?? "missing stop reason"})`,
        );
      progress(true);
      return {
        text: safe(text),
        usage,
        response_model: responseModel,
        stop_reason: stopReason,
        wall_s: (performance.now() - started) / 1000,
      };
    } catch (e) {
      const code = signal.aborted
        ? "cancelled"
        : deadline.signal.aborted
          ? "api_timeout"
          : stopReason === "max_tokens"
            ? "output_limit"
            : stopReason ||
                String(e).includes("Incomplete") ||
                String(e).includes("incomplete")
              ? "incomplete"
              : "request_error";
      progress(true);
      const message =
        code === "api_timeout"
          ? `API timeout after ${seconds.toFixed(1)}s; thinking=${thinkingChars}, answer=${text.length} characters; no executable strategy delivered`
          : code === "cancelled"
            ? "Model call cancelled by run stop; this is not a rejected strategy"
            : safe(String(e));
      throw new ModelCallError(message, code, diagnostics());
    } finally {
      clearInterval(timer);
      clearTimeout(deadlineTimer);
    }
  }
}
