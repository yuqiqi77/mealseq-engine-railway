"""离线审查全部解析菜谱的步骤依赖，不调用模型或网络。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from parse_recipes import (
    _check_device_sessions,
    _check_semantic_dependencies,
)


DEFAULT_RECIPES = Path(__file__).resolve().parent / "parsed_recipes_v5"


def audit_recipe(path: Path) -> tuple[int, int]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    steps = data.get("steps")
    source_steps = data.get("source_steps")
    ingredients = data.get("ingredients")
    if not isinstance(steps, list) or not steps:
        raise ValueError("steps 必须是非空数组")
    if not isinstance(source_steps, list) or len(source_steps) != len(steps):
        raise ValueError("source_steps 必须与 steps 一一对应")
    if not isinstance(ingredients, list):
        raise ValueError("ingredients 必须是数组")

    seen: set[str] = set()
    dependency_count = 0
    for index, step in enumerate(steps, start=1):
        step_id = step.get("step_id")
        if step_id != f"s{index}":
            raise ValueError(f"步骤编号不连续：预期 s{index}，实际 {step_id}")
        dependencies = step.get("depends_on")
        if not isinstance(dependencies, list):
            raise ValueError(f"{step_id} 的 depends_on 不是数组")
        if len(dependencies) != len(set(dependencies)):
            raise ValueError(f"{step_id} 存在重复依赖")
        invalid = [item for item in dependencies if item not in seen]
        if invalid:
            raise ValueError(f"{step_id} 引用了不存在或非前序步骤：{invalid}")
        dependency_count += len(dependencies)
        seen.add(step_id)

    # 复用解析器的业务校验：同食材状态流转、前序结果引用、分支关系、
    # 通用准备步骤，以及放入—运行—取出的连续设备会话。
    _check_semantic_dependencies(steps, source_steps, ingredients)
    _check_device_sessions(steps, source_steps)
    return len(steps), dependency_count


def main() -> int:
    parser = argparse.ArgumentParser(description="审查全部菜谱 JSON 的 depends_on")
    parser.add_argument("--recipes", type=Path, default=DEFAULT_RECIPES)
    args = parser.parse_args()

    paths = sorted(
        path
        for path in args.recipes.glob("*.json")
        if path.name != "failed_recipes.json"
    )
    failures: list[str] = []
    total_steps = 0
    total_dependencies = 0
    for path in paths:
        try:
            step_count, dependency_count = audit_recipe(path)
            total_steps += step_count
            total_dependencies += dependency_count
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            failures.append(f"{path.name}: {exc}")

    if failures:
        print(f"依赖审查失败：{len(failures)}/{len(paths)} 道")
        for item in failures:
            print(f"- {item}")
        return 1
    print(
        f"依赖审查通过：{len(paths)} 道菜，{total_steps} 个步骤，"
        f"{total_dependencies} 条直接依赖。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
