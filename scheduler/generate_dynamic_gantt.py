"""把动态实验的三个 JSON 生成为一个可交互甘特图页面。"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEDULER_DIR = Path(__file__).resolve().parent


def read_json(path: Path) -> dict:
    """读取 PowerShell 或 Python 生成的 UTF-8 JSON。"""
    return json.loads(path.read_text(encoding="utf-8-sig"))


def parse_interval(value: str) -> tuple[float, float]:
    """把接口中的 ``0-6`` 时间段拆成开始和结束分钟。"""
    start_text, end_text = value.split("-", 1)
    return float(start_text), float(end_text)


def clean_action(value: str) -> str:
    """去掉接口为了展示添加的前后缀，方便反查菜谱和步骤编号。"""
    value = re.sub(r"^(准备：|出锅/装盘：)", "", value.strip())
    value = value.split("；使用", 1)[0]
    return re.sub(r"[，。；、\s‘’“”\"']", "", value)


def recipe_step_index(response: dict) -> list[dict]:
    """建立“步骤文字 -> 菜名、步骤编号”的索引。"""
    index: list[dict] = []
    for recipe in response.get("recipeDetail", []):
        for number, step in enumerate(recipe.get("cookingSteps", []), start=1):
            index.append({
                "recipe": recipe["name"],
                "stepId": f"s{number}",
                "action": clean_action(step.get("describe", "")),
            })
    return index


def infer_step(action: str, index: list[dict]) -> tuple[str, str]:
    """接口部分人工步骤没有 recipeNames，通过步骤原文找回所属菜谱。"""
    cleaned = clean_action(action)
    exact = next((item for item in index if item["action"] == cleaned), None)
    if exact:
        return exact["recipe"], exact["stepId"]

    similar = next(
        (
            item
            for item in index
            if item["action"]
            and cleaned
            and (item["action"] in cleaned or cleaned in item["action"])
        ),
        None,
    )
    if similar:
        return similar["recipe"], similar["stepId"]
    return "未识别菜品", "?"


def resource_name(step_type: int, title: str) -> str:
    """把接口步骤类型转换为甘特图资源泳道。"""
    if step_type in (1, 6):
        return "人工"
    if step_type == 2:
        return "等待/腌制"
    if step_type == 4:
        return "灶具"
    if step_type == 5 and "烘" in title:
        return "烤箱"
    if step_type == 5 and ("蒸" in title or "煮" in title):
        return "蒸箱"
    if step_type == 3:
        return "设备预热"
    return title or "其他"


def timeline_tasks(response: dict) -> list[dict]:
    """把 detailTimeline 转成前端绘制甘特条所需的扁平任务列表。"""
    index = recipe_step_index(response)
    tasks: list[dict] = []
    for detail in response.get("detailTimeline", []):
        start, end = parse_interval(detail["timeInterval"])
        actions = detail.get("list") or [detail.get("title", "未命名步骤")]
        explicit_names = detail.get("recipeNames") or []

        for action_number, action in enumerate(actions):
            if action_number < len(explicit_names):
                recipe = explicit_names[action_number]
                _, step_id = infer_step(action, index)
            elif explicit_names:
                recipe = explicit_names[0]
                _, step_id = infer_step(action, index)
            else:
                recipe, step_id = infer_step(action, index)

            tasks.append({
                "start": start,
                "end": end,
                "duration": end - start,
                "recipe": recipe,
                "stepId": step_id,
                "resource": resource_name(int(detail.get("type", 0)), detail.get("title", "")),
                "title": detail.get("title", ""),
                "action": action,
            })
    return tasks


def build_payload(
    initial: dict,
    dynamic: dict,
    notice_data: dict,
    initial_tasks: list[dict] | None = None,
    dynamic_tasks: list[dict] | None = None,
    initial_metrics: dict | None = None,
    dynamic_metrics: dict | None = None,
) -> dict:
    """整理三个 JSON，页面不再依赖外部文件即可直接打开。"""
    return {
        "initial": {
            "label": "加菜前排程",
            "overview": initial.get("overview", {}),
            "metrics": initial_metrics or {},
            "tasks": initial_tasks if initial_tasks is not None else timeline_tasks(initial),
        },
        "dynamic": {
            "label": "加菜后排程",
            "overview": dynamic.get("overview", {}),
            "metrics": dynamic_metrics or {},
            "tasks": dynamic_tasks if dynamic_tasks is not None else timeline_tasks(dynamic),
        },
        "notifications": notice_data.get("notifications", []),
        "changes": notice_data.get("changes", []),
    }


HTML_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>食序引擎 · 动态排程对比</title>
<style>
:root {
  color-scheme: light;
  --bg: #f3f7f8;
  --surface: #ffffff;
  --surface-2: #edf6f5;
  --text: #102a35;
  --muted: #687b83;
  --line: #dbe5e7;
  --shadow: rgb(23 57 69 / 8%);
  --teal: #21b6a8;
  --teal-dark: #087f8c;
  --navy: #0b2b3a;
  --c1: #2f78d4;
  --c2: #e07b32;
  --c3: #2f9d69;
  --c4: #8c62d6;
}
* { box-sizing: border-box; }
body { margin: 0; background: radial-gradient(circle at 8% 0,#dff5f2 0,transparent 31%),var(--bg); color: var(--text); font: 14px/1.5 Inter,"Microsoft YaHei",sans-serif; }
.shell { max-width: 1500px; margin: 0 auto; padding: 24px; }
.brand-header { display:flex;align-items:center;justify-content:space-between;gap:20px;margin-bottom:18px;padding:20px 24px;border-radius:20px;background:linear-gradient(125deg,#092838,#0d4551 60%,#087f8c);color:#fff;box-shadow:0 16px 40px #11354229; }
.brand-lockup { display:flex;align-items:center;gap:14px; }.brand-mark { display:grid;place-items:center;width:52px;height:52px;border:1px solid #ffffff45;border-radius:15px;background:#ffffff16;font-weight:800;letter-spacing:.08em; }.eyebrow { color:#8ce3d9;font-size:10px;font-weight:800;letter-spacing:.17em; }
h1 { margin: 2px 0 1px; font-size: 24px; font-weight: 700; }
.subtitle { margin: 0; color: #d8ecee; }.view-chip { padding:8px 12px;border:1px solid #ffffff30;border-radius:999px;background:#ffffff14;font-weight:700;white-space:nowrap; }
.tabs { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 16px; }
.tab { border: 1px solid var(--line); border-radius: 8px; padding: 8px 14px; background: var(--surface); color: var(--text); font: inherit; cursor: pointer; }
.tab.active { border-color: transparent; background: linear-gradient(135deg,var(--teal-dark),var(--teal)); color: #fff; box-shadow:0 6px 16px #087f8c27; }
.metrics { display: grid; grid-template-columns: repeat(3, minmax(140px, 1fr)); gap: 10px; margin-bottom: 14px; }
.metric { padding: 12px 14px; border:1px solid #dfe9ea; border-radius: 11px; background: linear-gradient(180deg,#fff,#fcfefe); box-shadow: 0 5px 18px var(--shadow); }
.metric-label { color: var(--muted); font-size: 12px; }
.metric-value { margin-top: 2px; font-size: 21px; font-variant-numeric: tabular-nums; }
.legend { display: flex; flex-wrap: wrap; gap: 12px; margin: 12px 0; color: var(--muted); }
.legend-item { display: inline-flex; align-items: center; gap: 6px; }
.swatch { width: 11px; height: 11px; border-radius: 3px; background: var(--color); }
.panel { padding: 16px; border:1px solid #dfe9ea; border-radius: 16px; background: linear-gradient(180deg,#fff,#fcfefe); box-shadow: 0 8px 28px var(--shadow); }
.chart-scroll { overflow-x: auto; padding-bottom: 6px; }
.chart { min-width: 1180px; }
.axis, .lane { display: grid; grid-template-columns: 300px minmax(840px, 1fr); }
.axis-label { padding: 0 12px 8px 0; color: var(--muted); text-align: right; }
.axis-track { position: relative; height: 34px; border-bottom: 1px solid var(--line); }
.tick { position: absolute; bottom: 0; height: 100%; border-left: 1px solid var(--line); color: var(--muted); font-size: 11px; }
.tick span { position: absolute; top: 0; transform: translateX(-50%); white-space: nowrap; }
.lane-label { min-height: 44px; padding: 11px 12px 8px 0; overflow-x: auto; border-bottom: 1px solid var(--line); text-align: right; white-space: nowrap; }
.lane-resource { font-weight: 600; }
.lane-recipe { color: var(--muted); font-size: 12px; }
.lane-track { position: relative; min-height: 44px; border-bottom: 1px solid var(--line); background-image: linear-gradient(to right, var(--line) 1px, transparent 1px); background-size: var(--grid-size) 100%; }
.bar { position: absolute; top: 7px; height: 29px; min-width: 0; overflow: hidden; border: 1px solid var(--surface); border-radius: 6px; background: var(--color); color: #fff; cursor: pointer; }
.bar:hover, .bar:focus-visible { z-index: 2; outline: 2px solid var(--text); outline-offset: 1px; }
.bar.waiting { border: 2px dashed var(--color); background: transparent; color: var(--color); }
.bar.preparation-marker { top: 15px; width: 13px !important; height: 13px; min-width: 13px; transform: translateX(-50%) rotate(45deg); border-radius: 2px; }
.bar.preparation-marker .bar-content { display: none; }
.bar-content { display: block; padding: 4px 7px; overflow: hidden; text-overflow: clip; white-space: nowrap; font-size: 12px; }
.detail { margin-top: 12px; min-height: 48px; padding: 10px 12px; border-radius: 8px; background: var(--surface-2); color: var(--muted); }
.detail strong { color: var(--text); }
.hidden { display: none; }
@media (max-width: 700px) {
  .shell { padding: 14px; }
  .brand-header { align-items:flex-start;flex-direction:column; }
  .metrics { grid-template-columns: 1fr; }
}
</style>
</head>
<body>
<main class="shell">
  <header class="brand-header"><div class="brand-lockup"><div class="brand-mark">MS</div><div><div class="eyebrow">MEALSEQ ENGINE · 食序引擎</div><h1>动态排程前后对比</h1><p class="subtitle">查看加菜前后任务插入、资源占用与关键指标变化</p></div></div><div class="view-chip">动态重调度</div></header>
  <div class="tabs" role="tablist" aria-label="甘特图视图">
    <button class="tab active" data-view="initial" type="button">加菜前</button>
    <button class="tab" data-view="dynamic" type="button">加菜后</button>
  </div>
  <section id="schedule-view">
    <div class="metrics" id="metrics"></div>
    <div class="legend" id="legend"></div>
    <div class="panel">
      <div class="chart-scroll"><div class="chart" id="gantt"></div></div>
      <div class="detail" id="task-detail">点击任意任务条查看步骤详情。</div>
    </div>
  </section>
</main>
<script>
const DATA = __DATA__;
const recipes = [...new Set([
  ...DATA.initial.tasks.map(x => x.recipe),
  ...DATA.dynamic.tasks.map(x => x.recipe)
])].filter(Boolean);
const colors = ["var(--c1)", "var(--c2)", "var(--c3)", "var(--c4)"];
const recipeColor = Object.fromEntries(recipes.map((name, index) => [name, colors[index % colors.length]]));
const resourceOrder = ["前置准备", "人工", "灶眼", "灶具", "蒸箱", "烤箱", "冰箱", "等待/腌制", "设备预热", "其他"];

function number(value) { return Number.parseFloat(value || 0); }
function formatMinute(value) { return Number.isInteger(value) ? String(value) : value.toFixed(1); }
function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, character => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  })[character]);
}
function maxTimeFor(view) {
  return Math.max(number(view.overview.timeSpent), ...view.tasks.map(x => x.end), 1);
}
function tickStep(maxTime) { return maxTime <= 60 ? 5 : maxTime <= 120 ? 10 : 30; }
function percentage(value, maxTime) { return `${(value / maxTime) * 100}%`; }
function setGridSize(element, step, maxTime) { element.style.setProperty("--grid-size", `${(step / maxTime) * 100}%`); }
function resourceRank(name) {
  const normalized = name === "人工（1人）" ? "人工" : name;
  const index = resourceOrder.findIndex(prefix => normalized === prefix || normalized.includes(prefix));
  return index < 0 ? resourceOrder.length : index;
}

function renderMetrics(view) {
  const items = [
    ["菜品数量", `${view.overview.recipeCount} 道`],
    ["排程总耗时", `${view.overview.timeSpent} 分钟`],
    ["相对顺序制作节省", `${view.overview.timeSave} 分钟`]
  ];
  if (view.metrics && view.metrics.runtimeMs !== undefined) {
    const runtime = Number(view.metrics.runtimeMs);
    items.push(["算法运行时长", runtime < 1000 ? `${runtime.toFixed(1)} ms` : `${(runtime / 1000).toFixed(2)} 秒`]);
  }
  if (view.metrics && view.metrics.maxContinuousHumanMin !== undefined) {
    items.push(["最大连续人工", `${view.metrics.maxContinuousHumanMin} 分钟`]);
  }
  if (view.metrics && view.metrics.finishSpreadMin !== undefined) {
    items.push(["出锅时间差", `${view.metrics.finishSpreadMin} 分钟`]);
  }
  document.getElementById("metrics").innerHTML = items.map(([label, value]) =>
    `<div class="metric"><div class="metric-label">${label}</div><div class="metric-value">${value}</div></div>`
  ).join("");
}

function renderLegend(view) {
  const names = [...new Set(view.tasks.map(x => x.recipe))];
  document.getElementById("legend").innerHTML = names.map(name =>
    `<span class="legend-item"><span class="swatch" style="--color:${recipeColor[name]}"></span>${name}</span>`
  ).join("");
}

function renderAxis(maxTime) {
  const step = tickStep(maxTime);
  const ticks = [];
  for (let value = 0; value <= maxTime; value += step) {
    ticks.push(`<span class="tick" style="left:${percentage(value, maxTime)}"><span>${value} 分</span></span>`);
  }
  if (maxTime % step !== 0) {
    ticks.push(`<span class="tick" style="left:100%"><span>${formatMinute(maxTime)} 分</span></span>`);
  }
  return `<div class="axis"><div class="axis-label">实际资源</div><div class="axis-track">${ticks.join("")}</div></div>`;
}

function renderSchedule(viewName) {
  const view = DATA[viewName];
  const maxTime = maxTimeFor(view);
  const step = tickStep(maxTime);
  renderMetrics(view);
  renderLegend(view);

  const lanes = new Map();
  view.tasks.forEach(task => {
    const key = task.resource;
    if (!lanes.has(key)) lanes.set(key, []);
    lanes.get(key).push(task);
  });
  const sorted = [...lanes.entries()].sort((a, b) => {
    return resourceRank(a[0]) - resourceRank(b[0]) || a[0].localeCompare(b[0], "zh-CN");
  });

  const html = [renderAxis(maxTime)];
  sorted.forEach(([resource, tasks]) => {
    const bars = tasks.sort((a, b) => a.start - b.start).map(task => {
      const labelWidth = String(task.stepId).length * 7 + 14;
      const estimatedBarWidth = (task.duration / maxTime) * 760;
      const label = estimatedBarWidth >= labelWidth ? task.stepId : "";
      const left = percentage(task.start, maxTime);
      const width = percentage(task.duration, maxTime);
      // 收进左右各 2px，彻底分开首尾相接的短步骤；极短步骤允许缩到
      // 0.5px，避免“最小宽度”反过来覆盖相邻色块。
      const style = `left:calc(${left} + 2px);width:max(0.5px, calc(${width} - 4px));--color:${recipeColor[task.recipe]}`;
      const tooltip = `${task.recipe} ${task.stepId}｜${formatMinute(task.start)}–${formatMinute(task.end)} 分钟｜${task.resource}｜${task.action}`;
      const markerClass = task.marker ? " preparation-marker" : "";
      const waitingClass = (task.waiting || task.background) ? " waiting" : "";
      const markerStyle = task.marker ? `left:${left};--color:${recipeColor[task.recipe]}` : style;
      return `<button type="button" class="bar${markerClass}${waitingClass}" style="${markerStyle}" data-task="${encodeURIComponent(JSON.stringify(task))}" title="${escapeHtml(tooltip)}" aria-label="${escapeHtml(tooltip)}"><span class="bar-content">${escapeHtml(label)}</span></button>`;
    }).join("");
    html.push(`<div class="lane"><div class="lane-label"><div class="lane-resource">${resource}</div></div><div class="lane-track" style="--grid-size:${(step / maxTime) * 100}%">${bars}</div></div>`);
  });
  document.getElementById("gantt").innerHTML = html.join("");
  document.querySelectorAll(".bar").forEach(bar => bar.addEventListener("click", () => {
    const task = JSON.parse(decodeURIComponent(bar.dataset.task));
    document.getElementById("task-detail").innerHTML =
      `<strong>${escapeHtml(task.recipe)} ${escapeHtml(task.stepId)}</strong>｜` +
      `${formatMinute(task.start)}–${formatMinute(task.end)} 分钟｜${escapeHtml(task.resource)}<br>` +
      `${escapeHtml(task.action)}`;
  }));
}

document.querySelectorAll(".tab").forEach(tab => tab.addEventListener("click", () => {
  document.querySelectorAll(".tab").forEach(item => item.classList.toggle("active", item === tab));
  renderSchedule(tab.dataset.view);
}));

renderSchedule("initial");
</script>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="生成动态排程对比甘特图")
    parser.add_argument(
        "--initial",
        type=Path,
        default=SCHEDULER_DIR / "experiment_dynamic_initial.json",
    )
    parser.add_argument(
        "--dynamic",
        type=Path,
        default=SCHEDULER_DIR / "experiment_dynamic_result.json",
    )
    parser.add_argument(
        "--notifications",
        type=Path,
        default=SCHEDULER_DIR / "experiment_runtime" / "notifications.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=SCHEDULER_DIR / "dynamic_experiment_gantt.html",
    )
    args = parser.parse_args()

    payload = build_payload(
        read_json(args.initial),
        read_json(args.dynamic),
        read_json(args.notifications),
    )
    embedded = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    html = HTML_TEMPLATE.replace("__DATA__", embedded)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(html, encoding="utf-8")

    print(f"甘特图已生成：{args.output}")
    print(f"加菜前任务：{len(payload['initial']['tasks'])} 条")
    print(f"加菜后任务：{len(payload['dynamic']['tasks'])} 条")
    print(f"排程调整：{len(payload['changes'])} 条")
    print(f"操作通知：{len(payload['notifications'])} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
