"""Bounded interleaved v8/v9 checks; never seed a historical timetable.

Memory is read-only and identical between warm arms. The live arm has a separate
output and a hard subprocess watchdog. All reported elapsed time includes models.
"""
import argparse
import getpass
import json
import os
from pathlib import Path
import signal
import subprocess
import time

p = argparse.ArgumentParser()
p.add_argument("--tag", required=True)
p.add_argument("--live-only", action="store_true")
args = p.parse_args()
root = Path(__file__).resolve().parents[1]
old = root.parent / "longda-strategy-harness-v8-20261020"
out = root / "runs" / args.tag
out.mkdir(parents=True, exist_ok=False)
if args.live_only and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
    os.environ["ANTHROPIC_AUTH_TOKEN"] = getpass.getpass("API token (not saved): ")
arms = (
    [("v9-live-warm", root, True, True)]
    if args.live_only
    else [
        ("v8-cold", old, False, False),
        ("v9-cold", root, False, False),
        ("v9-warm-1", root, False, True),
        ("v8-warm-1", old, False, True),
        ("v8-warm-2", old, False, True),
        ("v9-warm-2", root, False, True),
    ]
)
rows = []
for label, folder, live, warm in arms:
    dest = out / label
    seconds = 150 if live else 45
    cmd = [
        str(folder / ".venv/bin/python"), str(folder / "start.py"),
        "--orders", "configs/orders20.json", "--vehicles", "2", "--out", str(dest),
        "--model", "qwen3.8-max" if live else "off",
        "--effort", "low", "--thinking-budget", "1024", "--max-tokens", "8192",
        "--summary-model", "qwen3.7-plus" if live else "off",
        "--memory-model", "off", "--memory", "read-only" if warm else "off",
        "--memory-db", str(root / "data/experience-v9.sqlite"),
        "--cp-workers", "2", "--threads", "4", "--construct-workers", "1",
        "--rounds", "1" if live else "0", "--strategy-trials", "3",
        "--repair-attempts", "1", "--construct-seconds", "5", "--solve-seconds", "10",
        "--completion-seconds", "15", "--api-timeout", "130",
        "--wall-seconds", str(seconds), "--target", "1140",
    ]
    before = time.monotonic()
    load = os.getloadavg()
    print("START", label, flush=True)
    with (out / f"{label}.log").open("w") as log:
        proc = subprocess.Popen(cmd, cwd=folder, stdout=log, stderr=subprocess.STDOUT,
                                env=os.environ.copy(), start_new_session=True)
        try:
            proc.wait(timeout=seconds + 30)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGINT)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
    r = json.loads((dest / "report.json").read_text()) if (dest / "report.json").exists() else {}
    row = dict(
        arm=label, exit_code=proc.returncode, wall_limit_s=seconds,
        process_wall_s=time.monotonic() - before, load_start=load, load_end=os.getloadavg(),
        elapsed_s=r.get("elapsed_s"), first_valid_s=r.get("first_valid_s"),
        day_minutes=(r.get("best") or {}).get("day_minutes"),
        audit_valid=(r.get("best") or {}).get("audit_valid"),
        model_calls=r.get("api_calls"), model_requests=r.get("actual_main_model_requests"),
        planner_delivery=r.get("planner_delivery"), timing=r.get("timing"),
        completed_trials=r.get("completed_trials"),
        completion_attempts=r.get("completion_attempts"), completion_valid=r.get("completion_valid"),
        memory_hits=r.get("memory_hits"), quality_feedback=len(r.get("quality_feedback", [])),
        errors=r.get("errors"), reason=r.get("stop_reason"), statistics=r.get("statistics"),
    )
    rows.append(row)
    (out / "summary.json").write_text(json.dumps({
        "protocol": dict(
            orders=20, vehicles=2, cp_workers=2, threads=4, construct_workers=1,
            warm_memory="same reviewed v9 snapshot, read-only",
            known_schedule_answers=False,
            qualification="short diagnostic, not enough samples to establish speedup or optimality",
        ),
        "results": rows,
    }, ensure_ascii=False, indent=2))
    print(json.dumps({k: row[k] for k in [
        "arm", "elapsed_s", "first_valid_s", "day_minutes", "audit_valid",
        "model_calls", "completion_attempts", "reason", "exit_code",
    ]}, ensure_ascii=False), flush=True)
