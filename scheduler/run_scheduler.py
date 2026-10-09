"""在命令行中运行排程算法。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from generate_gantt import build_bars, render_html
from scheduler import load_device_rules, schedule_from_request


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RECIPES = PROJECT_ROOT / "ai" / "parsed_recipes_v5"
DEFAULT_DEVICES = PROJECT_ROOT / "para" / "设备参数清单参考.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="生成多菜并行烹饪计划")
    parser.add_argument("--request", type=Path, required=True, help="请求 JSON 文件")
    parser.add_argument("--output", type=Path, required=True, help="接口响应保存位置")
    parser.add_argument(
        "--gantt-output",
        type=Path,
        default=None,
        help="静态甘特图保存位置；默认与响应 JSON 放在一起",
    )
    parser.add_argument("--recipes", type=Path, default=DEFAULT_RECIPES, help="菜谱 JSON 目录")
    parser.add_argument("--devices", type=Path, default=DEFAULT_DEVICES, help="设备参数文件")
    parser.add_argument(
        "--max-human",
        type=float,
        default=30,
        help="希望单次连续人工操作不超过多少分钟，默认 30",
    )
    parser.add_argument(
        "--solver",
        choices=("cp_sat", "genetic", "heuristic"),
        default=None,
        help="内部静态求解器；不填写时默认使用 genetic",
    )
    args = parser.parse_args()

    request_items = json.loads(args.request.read_text(encoding="utf-8-sig"))
    plan = schedule_from_request(
        request_items,
        recipe_dir=args.recipes,
        device_path=args.devices,
        max_continuous_human=args.max_human,
        solver=args.solver,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(plan.response, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    gantt_output = args.gantt_output or args.output.with_name(
        f"{args.output.stem}_gantt.html"
    )
    rows, bars, colors = build_bars(plan, load_device_rules(args.devices))
    gantt_output.parent.mkdir(parents=True, exist_ok=True)
    gantt_output.write_text(
        render_html(rows, bars, colors, plan.metrics),
        encoding="utf-8",
    )

    print(f"排程完成：{args.output}")
    print(f"静态甘特图：{gantt_output}")
    print("评分指标：")
    for name, value in plan.metrics.items():
        print(f"  {name}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
