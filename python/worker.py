"""JSON-file worker: unmodified rules, real CpSolver.Solve, separate audit."""

import copy, hashlib, json, os, signal, sys, time, threading, re
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "reference")]
from ortools.sat.python import cp_model
from longda_cpsat.model import build_model, flatten, extract_schedule
from longda_cpsat.audit import audit
from longda_cpsat.objectives import add_objective, evaluate_schedule, objective_key
from longda_cpsat.search_tasks import select_orders
from longda_cpsat.result_summary import build_summary
from longda_cpsat.html_export import export_run_html
from constructor import construct as construct_extended
from legacy_constructor import construct
from verification import verify
from diagnostics import summarize


def save(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    tmp.replace(path)


def integrity():
    expected = json.loads((ROOT / "reference-manifest.json").read_text())
    actual = {
        str(p.relative_to(ROOT / "reference")): hashlib.sha256(
            p.read_bytes()
        ).hexdigest()
        for p in (ROOT / "reference").rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    }
    if expected != actual:
        raise ValueError("Reference rules changed")
    return hashlib.sha256(json.dumps(actual, sort_keys=True).encode()).hexdigest()


def full_hint(model, sc, s):
    """Derive auxiliary hint values; never fix their domains or call them a solution."""
    ops = flatten(sc)
    tasks = sorted(copy.deepcopy(s["transport"]), key=lambda t: t["id"])
    base = 2 if sc["transport"]["vehicle_count"] == 1 else 2 * len(tasks) + 1
    incoming, outgoing, pin, pout = {}, {}, {}, {}
    by = defaultdict(list)
    for q in ops:
        by[q["order"]].append(q["index"])
    for t in tasks:
        i = t["op"]
        t["_pe"] = t["start"] * base + t["pickup_rank"]
        t["_de"] = t["end"] * base + t["dropoff_rank"]
        if t["kind"] == "cold_to_furnace":
            incoming[i] = t
        if t["kind"] == "furnace_to_press":
            outgoing[i] = pin[i] = t
        if t["kind"].startswith("press_to_"):
            pout[i] = t
            if t["kind"] == "press_to_furnace":
                incoming[by[t["order"]][by[t["order"]].index(i) + 1]] = t
    A, S, F, E, X = (
        s[k] for k in ["heat_start", "forge_start", "furnace", "forge_end", "heat_end"]
    )
    metrics = evaluate_schedule(sc, s)
    D = 1440 * sc["ticks_per_minute"]

    def value(name):
        if name == "shared_day_score":
            return metrics["score_ticks"]
        if name == "shared_hold_excess":
            return metrics["hold_excess_ticks"]
        if name.startswith("shared_clip_"):
            return min(D, S[int(name.rsplit("_", 1)[1])])
        if name.startswith("shared_busy_"):
            i = int(name.rsplit("_", 1)[1])
            return max(0, min(ops[i]["duration"], D - S[i]))
        m = re.fullmatch(
            r"(same_car|car_before|same_furnace|before|in)(\d+)_(\d+)", name
        )
        if m:
            k, i, j = m.group(1), int(m.group(2)), int(m.group(3))
            return int(
                {
                    "same_car": lambda: tasks[i]["vehicle"] == tasks[j]["vehicle"],
                    "car_before": lambda: tasks[i]["_pe"] < tasks[j]["_pe"],
                    "same_furnace": lambda: F[i] == F[j],
                    "before": lambda: A[i] <= A[j],
                    "in": lambda: F[i] == j,
                }[k]()
            )
        m = re.fullmatch(r"(.+?)(\d+)", name)
        if not m:
            raise ValueError("Unknown hint variable " + name)
        k, i = m.group(1), int(m.group(2))
        if k in ("pickup_rank", "dropoff_rank", "vehicle"):
            return tasks[i][k]
        if k in ("pickup_event", "dropoff_event"):
            return tasks[i]["_pe" if k == "pickup_event" else "_de"]
        return {
            "a": lambda: A[i],
            "s": lambda: S[i],
            "f": lambda: F[i],
            "e": lambda: E[i],
            "x": lambda: X[i],
            "h": lambda: X[i] - A[i],
            "heat_event_duration": lambda: outgoing[i]["_pe"] - incoming[i]["_de"],
            "press_event_duration": lambda: pout[i]["_pe"] - pin[i]["_de"],
            "cold_pickup": lambda: A[i] - 15,
            "finished_delivery": lambda: E[i] + 15,
        }[k]()

    for i, v in enumerate(model.proto.variables):
        x = int(value(v.name))
        model.add_hint(model.get_int_var_from_proto_index(i), x)
    return len(model.proto.solution_hint.vars)


def audit_delivery(sc, schedule, out, source="constructor"):
    """Publish only after physical audit AND independent complete-model evaluation."""
    start = time.monotonic()
    checked = audit(sc, schedule)
    proof = verify(sc, copy.deepcopy(schedule)) if checked["valid"] else {"valid": False}
    valid = checked["valid"] and proof["valid"]
    result = dict(status="AUDITED" if valid else "AUDIT_FAILED", valid=valid,
                  audit_valid=valid, cp_solver_called=False, source=source,
                  audit_s=time.monotonic()-start, primary_optimal=False)
    save(out / "best_audit.json", checked)
    save(out / "exact_model_verification.json", proof)
    if not valid:
        result["errors"] = checked.get("errors", [])[:10] + proof.get("errors", [])[:10]
        return result
    metric = evaluate_schedule(sc, schedule)
    result.update(metrics=metric, day_minutes=metric["score_ticks"]/sc["ticks_per_minute"],
                  diagnostics=summarize(sc, schedule, flatten(sc)),
                  schedule_path=str(out/"best_schedule.json"))
    save(out/"best_schedule.json", schedule)
    save(out/"scenario.json", sc)
    save(out/"best.json", dict(schedule=schedule, audit=checked, **metric, source=source))
    save(out/"result_summary.json", build_summary(sc, checked))
    save(out/"report.json", result)
    export_run_html(out)
    return result


def construct_batch(sc, req, out):
    rows, best, best_metric, partial = [], None, None, None
    start = time.monotonic()
    for number, config in enumerate(req["configs"]):
        config = dict(config)
        cfg = {**config, "seconds": max(.01, req.get("seconds", 30)-(time.monotonic()-start)),
               "incumbent": req.get("incumbent_metrics"), "preference": req["preference"]}
        t = time.monotonic()
        try:
            schedule, err, stats = construct_extended(sc, **cfg)
            partial_candidate = stats.pop("partial", None)
            if partial_candidate: partial = partial_candidate
            row = dict(config=config, status="construction_deadend", error=err, **stats)
            if schedule is not None:
                checked = audit(sc, schedule)
                if checked["valid"]:
                    metric = evaluate_schedule(sc, schedule)
                    row.update(status="physical_seed", day_minutes=metric["score_ticks"]/sc["ticks_per_minute"])
                    if best is None or objective_key(metric, req["preference"]) > objective_key(best_metric, req["preference"]):
                        best, best_metric = schedule, metric
                else:
                    row.update(status="audit_failed", errors=checked.get("errors", [])[:4])
                    partial = schedule
        except (ValueError, TypeError, KeyError, ZeroDivisionError, IndexError, MemoryError, SyntaxError, NameError, OverflowError, RecursionError) as e:
            row = dict(config=config, status="program_or_construction_error", error=str(e))
        row["construction_s"] = time.monotonic()-t
        rows.append(row)
        if time.monotonic()-start >= req.get("seconds", 30): break
    result = dict(trials=len(rows), complete=sum(r["status"]=="physical_seed" for r in rows),
                  rows=rows, construction_s=time.monotonic()-start, valid=False,
                  cp_solver_called=False, seed_path=None, partial_path=None)
    if partial:
        save(out/"partial.json", partial);result["partial_path"]=str(out/"partial.json")
    if best:
        verified = audit_delivery(sc,best,out)
        result.update(verified)
        if verified["valid"]:
            result.update(seed_path=verified["schedule_path"],seed_day_minutes=verified["day_minutes"])
            for row in rows:
                row["selected_seed"] = row.get("day_minutes")==verified["day_minutes"]
    return result


def solve(sc, req, out):
    begin = time.monotonic()
    phase = ["model_build"]
    done = threading.Event()

    def heartbeat():
        while not done.wait(10):
            print(
                json.dumps(
                    {
                        "event": "progress",
                        "phase": phase[0],
                        "seconds": round(time.monotonic() - begin, 1),
                    }
                ),
                flush=True,
            )

    threading.Thread(target=heartbeat, daemon=True).start()
    solver = None
    cancelled = [False]

    def cancel(*_):
        cancelled[0] = True
        if solver:
            solver.stop_search()

    signal.signal(signal.SIGTERM, cancel)
    signal.signal(signal.SIGINT, cancel)
    try:
        model, v, ops = build_model(sc)
        base_variables = len(model.proto.variables)
        base_constraints = len(model.proto.constraints)
        inc = (
            json.loads(Path(req["incumbent_path"]).read_text())
            if req.get("incumbent_path")
            else None
        )
        seed = (
            json.loads(Path(req["seed_path"]).read_text())
            if req.get("seed_path")
            else inc
        )
        if inc and not audit(sc, inc)["valid"]:
            raise ValueError("Invalid incumbent")
        if seed and not audit(sc, seed)["valid"]:
            raise ValueError("Invalid complete seed")
        mode = req.get("mode", "seeded_search")
        scope = "global"
        selected = [o["id"] for o in sc["orders"]]
        if inc and mode == "local_improve":
            selected = req.get("focus_orders") or select_orders(
                sc, inc, req["direction"], req["fraction"], req.get("seed", 0)
            )
            if not set(selected) <= set(o["id"] for o in sc["orders"]):
                raise ValueError("Unknown neighborhood order")
            scope = "local" if len(selected) < len(sc["orders"]) else "global"
            for q in ops:
                if q["order"] not in selected:
                    for k in ("heat_start", "forge_start", "furnace"):
                        model.add(v[k][q["index"]] == inc[k][q["index"]])
        objective = add_objective(
            model, v, ops, sc, preference=req["preference"], first=True
        )
        weight = objective["lex_weight"]
        expr = (
            objective["score"]
            if req["preference"] == "none"
            else weight * objective["score"] - objective["hold_excess"]
        )
        if inc:
            metric = evaluate_schedule(sc, inc)
            baseline = (
                metric["score_ticks"]
                if req["preference"] == "none"
                else weight * metric["score_ticks"] - metric["hold_excess_ticks"]
            )
            model.add(expr > baseline)
        model.maximize(expr)
        hint_count = 0
        if seed:
            hint_count = full_hint(model, sc, seed)
        elif req.get("partial_path"):
            p = json.loads(Path(req["partial_path"]).read_text())
            for k in ("heat_start", "forge_start", "furnace"):
                for var, x in zip(v[k], p.get(k, [])):
                    domain = list(var.proto.domain)
                    if type(x) is int and any(
                        lo <= x <= hi for lo, hi in zip(domain[::2], domain[1::2])
                    ):
                        model.add_hint(var, x)
                        hint_count += 1
        error = model.validate()
        if error:
            raise ValueError(error)
        build_s = time.monotonic() - begin
        solver = cp_model.CpSolver()
        solver.parameters.num_search_workers = req["threads"]
        solver.parameters.max_time_in_seconds = max(0.01, req["seconds"] - build_s)
        solver.parameters.random_seed = req.get("seed", 0)
        solver.parameters.log_search_progress = True
        solver.parameters.log_to_stdout = False
        solver.parameters.hint_conflict_limit = 10000
        solver.parameters.cp_model_probing_level = req.get("probing_level", 0)
        # An audited seed already at target only needs acceptance by Solve.
        # No presolve is faster in this measured case; genuine improvement
        # attempts retain presolve. All physical constraints remain present.
        presolve_mode = req.get("presolve", "auto")
        if presolve_mode not in ("auto", "standard", "off"):
            raise ValueError("Unknown presolve mode")
        target_seed = bool(
            seed
            and req.get("stop_at_target", True)
            and evaluate_schedule(sc, seed)["score_ticks"]
            >= req["target_minutes"] * sc["ticks_per_minute"]
            and mode == "seeded_search"
        )
        if presolve_mode == "off" or (presolve_mode == "auto" and target_seed):
            solver.parameters.cp_model_presolve = False
        with open(out / "solver.log", "w") as log:

            def log_line(line):
                log.write(line + "\n")
                log.flush()

            solver.log_callback = log_line

            class Observe(cp_model.CpSolverSolutionCallback):
                def __init__(self):
                    super().__init__()
                    self.count = 0
                    self.first_s = None

                def on_solution_callback(self):
                    self.count += 1
                    if self.first_s is None:
                        self.first_s = time.monotonic() - begin
                    if (
                        req.get("stop_at_target", True)
                        and self.value(objective["score"])
                        >= req["target_minutes"] * sc["ticks_per_minute"]
                    ):
                        self.stop_search()

            observe = Observe()
            phase[0] = "cp_sat_search"
            t = time.monotonic()
            if cancelled[0]:
                return {
                    "status": "CANCELLED",
                    "valid": False,
                    "cp_solver_called": False,
                }
            status = solver.solve(model, observe)
            solve_s = time.monotonic() - t
        result = {
            "status": solver.status_name(status),
            "valid": False,
            "cp_solver_called": True,
            "scope": scope,
            "mode": mode,
            "released_orders": selected,
            "original_variables": base_variables,
            "original_constraints": base_constraints,
            "hint_values": hint_count,
            "fixed_hint_values": False,
            "model_build_s": build_s,
            "solver_wall_s": solve_s,
            "solution_callbacks": observe.count,
            "first_cp_solution_s": observe.first_s,
            "branches": solver.num_branches,
            "conflicts": solver.num_conflicts,
            "objective_bound": solver.best_objective_bound,
            "preference": req["preference"],
            "probing_level": solver.parameters.cp_model_probing_level,
            "presolve_enabled": solver.parameters.cp_model_presolve,
            "target_seed_fast_path": target_seed
            and not solver.parameters.cp_model_presolve,
            "bound_is_encoded": req["preference"] != "none",
            "cancelled": cancelled[0],
        }
        if status in (cp_model.FEASIBLE, cp_model.OPTIMAL):
            phase[0] = "independent_audit"
            t = time.monotonic()
            s = extract_schedule(solver, v)
            checked = audit(sc, s)
            proof = (
                verify(sc, copy.deepcopy(s)) if checked["valid"] else {"valid": False}
            )
            metric = evaluate_schedule(sc, s)
            result.update(
                valid=checked["valid"] and proof["valid"],
                audit_valid=checked["valid"] and proof["valid"],
                audit_s=time.monotonic() - t,
                day_minutes=metric["score_ticks"] / sc["ticks_per_minute"],
                metrics=metric,
                diagnostics=summarize(sc, s, ops),
                primary_optimal=status == cp_model.OPTIMAL and scope == "global",
            )
            if not result["valid"]:
                raise ValueError("Solver candidate failed independent audit")
            save(out / "best_schedule.json", s)
            save(out / "scenario.json", sc)
            save(out / "best_audit.json", checked)
            save(out / "exact_model_verification.json", proof)
            save(
                out / "best.json",
                dict(schedule=s, audit=checked, **metric, source="cp_sat_solve"),
            )
            save(out / "result_summary.json", build_summary(sc, checked))
            save(out / "report.json", result)
            export_run_html(out)
            result["schedule_path"] = str(out / "best_schedule.json")
        elif status == cp_model.INFEASIBLE:
            result["proof_scope"] = (
                "original_model"
                if scope == "global" and not inc
                else (
                    "no_improvement_global"
                    if scope == "global"
                    else "restricted_neighborhood"
                )
            )
        else:
            result["proof_scope"] = "none"
        return result
    finally:
        done.set()


def main():
    req = json.loads(Path(sys.argv[1]).read_text())
    out = Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    try:
        rule_hash = integrity()
        sc = json.loads(Path(req["scenario_path"]).read_text())
        if req["action"] == "construct":
            # Generated programs have no credentials or I/O; also bound process resources.
            import resource
            resource.setrlimit(resource.RLIMIT_AS, (8*1024**3,8*1024**3))
            resource.setrlimit(resource.RLIMIT_CPU, (max(10,int(req.get("seconds",30))+10),max(11,int(req.get("seconds",30))+11)))
            result = construct_batch(sc, req, out)
        elif req["action"] == "audit":
            result = audit_delivery(sc, json.loads(Path(req["schedule_path"]).read_text()), out)
        elif req["action"] == "solve":
            result = solve(sc, req, out)
        else:
            raise ValueError("Unknown action")
        result.update(rule_hash=rule_hash, elapsed_s=time.monotonic() - start)
        integrity()
        save(out / "result.json", result)
    except Exception as e:
        save(
            out / "result.json",
            {
                "status": "WORKER_ERROR",
                "valid": False,
                "error": str(e),
                "elapsed_s": time.monotonic() - start,
            },
        )
        raise
    print(
        json.dumps({"event": "done", "result_path": str(out / "result.json")}),
        flush=True,
    )


if __name__ == "__main__":
    main()
