"""Render measured run reports. No credentials or private reasoning are read."""

import argparse
import html
import json
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = ap.parse_args()
    root = args.root
    rows = []
    for path in sorted((root / "runs").glob("*/report.json")):
        r = json.loads(path.read_text())
        if "api_calls" not in r:
            continue
        b = r.get("best") or {}
        t = r.get("timing") or {}
        rows.append(
            dict(
                name=path.parent.name,
                status=r.get("stop_reason"),
                minutes=b.get("day_minutes"),
                utilization=None if not b else b["day_minutes"] / 14.4,
                total_s=r.get("elapsed_s"),
                api_s=t.get("api_wall_s"),
                cp_s=t.get("cp_solver_wall_s"),
                calls=r.get("api_calls"),
                solves=r.get("cp_solver_calls"),
                memory_hits=r.get("memory_hits"),
                construct_trials=r.get("construction_trials"),
                source_hash=r.get("source_hash"),
                path=f"../runs/{path.parent.name}/best/views",
            )
        )
    out = root / "docs"
    out.mkdir(exist_ok=True)
    (out / "benchmark.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2))

    def fmt(x):
        return (
            "—"
            if x is None
            else f"{x:.2f}" if isinstance(x, float) else html.escape(str(x))
        )

    table = "".join(
        "<tr>"
        + "".join(
            f"<td>{fmt(r[k])}</td>"
            for k in [
                "name",
                "status",
                "minutes",
                "utilization",
                "total_s",
                "api_s",
                "cp_s",
                "calls",
                "solves",
                "memory_hits",
            ]
        )
        + "</tr>"
        for r in rows
    )
    links = "".join(
        f'<li>{html.escape(r["name"])}：<a href="{r["path"]}/equipment.html">设备视角</a> · <a href="{r["path"]}/orders.html">订单视角</a></li>'
        for r in rows
        if r["minutes"] is not None
    )
    content = f"""<!doctype html><html lang="zh"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>隆达策略 Harness 实验</title><style>body{{font:16px/1.7 system-ui,sans-serif;color:#172b40;background:#f2f5f8;margin:0}}main{{max-width:1250px;margin:40px auto;padding:32px;background:white;border-radius:16px}}h1{{font-size:30px}}table{{border-collapse:collapse;width:100%;font-size:14px}}th,td{{padding:12px;text-align:left;border-bottom:1px solid #dde4eb}}th{{background:#edf4fa}}.flow{{background:#eaf6f1;padding:20px;border-radius:10px}}a{{color:#166ca2}}.scroll{{overflow:auto}}</style><main><h1>隆达：Memory＋策略规划＋CP-SAT</h1><p class="flow">检索相关经验 → 模型提出策略 → 构造提示 → 完整 CP-SAT 求解 → 独立审计 → 保存方案与经验</p><p>20 单、6 炉×5 支、1 台压机、2 辆车、4 天完成。硬约束沿用冻结原模型。首日使用率＝净锻打分钟数÷1440。</p><h2>实测结果</h2><div class="scroll"><table><thead><tr><th>运行</th><th>停止原因</th><th>首日/分钟</th><th>使用率/%</th><th>总耗时/s</th><th>模型/s</th><th>CP/s</th><th>模型调用</th><th>CP调用</th><th>记忆命中</th></tr></thead><tbody>{table}</tbody></table></div><p>总耗时包含模型思考、构造、建模、搜索、审计与输出。CP 耗时是其组成部分；不能把层级计时重复相加。UNKNOWN 不表示无解。</p><h2>查看排程</h2><ul>{links}</ul><h2>判断边界</h2><p>达到 720 分钟仅表示达到本轮 50% 目标，不代表全局最优。冷启动、已有 Memory 的复用、已知方案接线验证分别统计；单次结果不能证明对新订单普遍加速。</p><p>详细结论、旧版历史参考和限制见 <a href="experiment.md">实验说明</a>；运行原始数据见 <a href="benchmark.json">benchmark.json</a>。</p></main></html>"""
    (out / "benchmark.html").write_text(content)


if __name__ == "__main__":
    main()
