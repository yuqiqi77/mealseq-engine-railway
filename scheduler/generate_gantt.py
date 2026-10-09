"""生成按具体灶眼、设备层位和后台分区展示的静态甘特图。"""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

from notification_generator import advance_preparation_events
from scheduler import (
    EPSILON,
    _assign_calendar_instances,
    load_device_rules,
    schedule_from_request,
)
from static_solver_common import build_resource_uses


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RECIPES = PROJECT_ROOT / "ai" / "parsed_recipes_v5"
DEFAULT_DEVICES = PROJECT_ROOT / "para" / "设备参数清单参考.json"
COLORS = ["#3b82f6", "#f97316", "#22c55e", "#a855f7", "#ef4444", "#14b8a6", "#eab308"]


def clean_number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.1f}"


def build_bars(plan, device_rules: dict) -> tuple[list[str], list[dict], dict[str, str]]:
    """把排程转换为甘特图行；前台连续会话按实际步骤分段绘制。"""

    schedule = plan.scheduled_tasks
    by_key = {item.task.key: item for item in schedule}
    recipes = list(dict.fromkeys(item.task.recipe_name for item in schedule))
    colors = {name: COLORS[index % len(COLORS)] for index, name in enumerate(recipes)}
    bars: list[dict] = []
    for item in schedule:
        for segment_index, interval in enumerate(item.human_intervals, start=1):
            bars.append({
                "row": "人工（1人）", "start": interval[0], "end": interval[1],
                "recipe": item.task.recipe_name,
                "stepId": item.task.step_id,
                "title": "人工操作",
                "label": f"{item.task.recipe_name} {item.task.step_id} · 人工{segment_index}",
                "action": item.task.action,
            })
        # 等待行完全按资源占用判断，不依赖动作中的关键词：只有步骤没有
        # 主设备和附加设备、且人工只占其中一部分时，才把人工区间之外的
        # 补集画为等待。人工全程执行或任何机器正在占用时都不画等待条。
        human_duration = sum(
            end - start for start, end in item.human_intervals
        )
        if (
            human_duration > EPSILON
            and item.task.duration > human_duration + EPSILON
            and not item.task.original.get("advance_preparation")
            and item.task.device is None
            and not item.task.additional_device_usages
        ):
            # 只绘制人工区间之间的补集，避免等待色块与放入、取出等
            # 人工操作重复。人工时间仍只出现在“人工（1人）”行。
            wait_start = item.start
            waiting_intervals: list[tuple[float, float]] = []
            for human_start, human_end in sorted(item.human_intervals):
                if human_start > wait_start + EPSILON:
                    waiting_intervals.append((wait_start, human_start))
                wait_start = max(wait_start, human_end)
            if item.end > wait_start + EPSILON:
                waiting_intervals.append((wait_start, item.end))
            for wait_index, (start, end) in enumerate(waiting_intervals, start=1):
                bars.append({
                    "row": f"等待 · {item.task.recipe_name}",
                    "start": start,
                    "end": end,
                    "recipe": item.task.recipe_name,
                    "stepId": item.task.step_id,
                    "title": "等待（无人操作）",
                    "waiting": True,
                    "label": f"{item.task.recipe_name} {item.task.step_id} · 等待{wait_index}",
                    "action": item.task.action,
                })

    resource_uses = build_resource_uses(schedule)
    instances_by_use: dict[str, tuple[str, ...]] = {}
    for device in {item.device for item in resource_uses}:
        records = [item for item in resource_uses if item.device == device]
        rule = device_rules.get(device)
        if rule is None:
            continue
        assigned = _assign_calendar_instances(
            [((item.start, item.end), item.instance_hints) for item in records],
            rule,
        )
        if assigned:
            instances_by_use.update(
                {item.key: instances for item, instances in zip(records, assigned)}
            )

    for use in resource_uses:
        members = [by_key[key] for key in use.task_keys]
        first = members[0]
        rule = device_rules.get(use.device)
        instances = instances_by_use.get(
            use.key, first.resource_instances or (use.device,)
        )
        # 调度仍使用 use.start～use.end 的完整会话锁定资源并统一分配实例；
        # 展示时，灶具、蒸箱、烤箱等前台设备拆回各步骤的实际工作区间，
        # 不再把步骤之间的等待渲染成一整条很长的色块。
        if len(members) > 1 and not (rule and rule.background):
            for member in members:
                interval = member.device_interval
                if interval is None:
                    continue
                for instance in instances:
                    bars.append({
                        "row": instance,
                        "start": interval[0],
                        "end": interval[1],
                        "recipe": member.task.recipe_name,
                        "stepId": member.task.step_id,
                        "title": use.device,
                        "label": (
                            f"{member.task.recipe_name} {member.task.step_id}"
                            f" · {instance}"
                        ),
                        "action": member.task.action,
                    })
            continue

        action = " → ".join(item.task.action for item in members)
        step_ids = list(dict.fromkeys(item.task.step_id for item in members))
        step_id = "→".join(step_ids)
        for instance in instances:
            row = instance
            if rule and rule.background and use.device == "冰箱":
                row = f"{instance} · {first.task.recipe_name}"
            bars.append({
                "row": row, "start": use.start, "end": use.end,
                "recipe": first.task.recipe_name,
                "stepId": step_id,
                "title": use.device,
                "background": bool(rule and rule.background and use.device == "烟机"),
                "label": f"{first.task.recipe_name} · {instance}",
                "action": action,
            })

    for event in advance_preparation_events(schedule):
        bars.append({
            "row": "前置准备（不计时）",
            "start": event["notifyAtMin"],
            "end": event["notifyAtMin"],
            "recipe": event["recipeName"],
            "stepId": "→".join(event["stepIds"]),
            "title": "前置准备已完成",
            "label": "前置准备",
            "action": "需提前完成：" + "；".join(event["actions"]),
            "marker": True,
        })

    # 只展示本次排程真正用到的资源。设备清单可能包含很多未参与本次
    # 烹饪的设备，提前全部建行会让甘特图出现大片没有任务的空白区域。
    used_rows = {item["row"] for item in bars}
    rows: list[str] = []
    if "人工（1人）" in used_rows:
        rows.append("人工（1人）")
    if "前置准备（不计时）" in used_rows:
        rows.append("前置准备（不计时）")
    for rule in device_rules.values():
        for instance in rule.instances:
            if instance in used_rows and instance not in rows:
                rows.append(instance)
    # 冰箱后台分区会附带菜名，或将来可能出现配置外的展示行，按首次
    # 出现顺序补在后面，确保任何实际占用都不会被漏画。
    for item in bars:
        if item["row"] not in rows:
            rows.append(item["row"])
    return rows, bars, colors


def render_html(
    rows: list[str],
    bars: list[dict],
    colors: dict[str, str],
    metrics: dict,
    notifications: list[dict] | None = None,
) -> str:
    notifications = notifications or []
    makespan = max((item["end"] for item in bars), default=1)
    row_height = 42
    # 左侧泳道名称可能很长。按实际文字估算宽度，并让 SVG 本身随之变宽；
    # 外层 .scroll 会出现横向滚动条，不再把名称裁掉。
    label_width = min(
        430,
        max(190, max((sum(14 if ord(char) > 127 else 7 for char in row) for row in rows), default=0) + 28),
    )
    chart_width = 1280
    width = label_width + chart_width + 30
    height = 150 + len(rows) * row_height

    def x(value: float) -> float:
        return label_width + value / max(makespan, 1) * chart_width

    svg: list[str] = [
        f'<svg viewBox="0 0 {width} {height}" role="img" '
        f'style="min-width:{width}px">'
    ]
    bar_index = 0
    for tick in range(0, 11):
        minute = makespan * tick / 10
        xpos = x(minute)
        svg.append(f'<line x1="{xpos:.1f}" y1="34" x2="{xpos:.1f}" y2="{height - 15}" class="grid"/>')
        svg.append(f'<text x="{xpos:.1f}" y="24" text-anchor="middle" class="axis">{html.escape(clean_number(minute))}分</text>')
    reschedule_at = metrics.get("rescheduleAtMin")
    if isinstance(reschedule_at, (int, float)):
        marker_minute = min(max(float(reschedule_at), 0.0), makespan)
        marker_x = x(marker_minute)
        marker_label = html.escape(clean_number(marker_minute))
        svg.append(
            f'<line x1="{marker_x:.1f}" y1="32" x2="{marker_x:.1f}" '
            f'y2="{height - 15}" class="reschedule-line"/>'
        )
        svg.append(
            f'<text x="{marker_x + 6:.1f}" y="44" class="reschedule-label">'
            f'第{marker_label}分钟重调度</text>'
        )
    for index, row in enumerate(rows):
        ypos = 48 + index * row_height
        svg.append(f'<text x="{label_width - 10}" y="{ypos + 21}" text-anchor="end" class="label">{html.escape(row)}</text>')
        svg.append(f'<line x1="{label_width}" y1="{ypos + 32}" x2="{width - 20}" y2="{ypos + 32}" class="rowline"/>')
        for bar in (item for item in bars if item["row"] == row):
            bar_index += 1
            raw_left = x(bar["start"])
            raw_right = x(bar["end"])
            # 首尾相接的短步骤在数据上并不冲突，但圆角和阴影容易让它们
            # 看起来互相覆盖。每端最多收进 1.5px，留下清晰的视觉间隔。
            raw_width = max(0.0, raw_right - raw_left)
            inset = min(1.5, max(0.0, raw_width * 0.25))
            left = raw_left + inset
            bar_width = max(0.5, raw_width - 2 * inset)
            tooltip = html.escape(
                f"{bar['recipe']} | {clean_number(bar['start'])}-{clean_number(bar['end'])}分钟 | {bar['action']}"
            )
            clip_id = f"bar-clip-{bar_index}"
            task_data = html.escape(
                json.dumps(bar, ensure_ascii=False),
                quote=True,
            )
            if bar.get("marker"):
                center = raw_left
                marker_y = ypos + 17
                svg.append(
                    f'<g class="task-bar preparation-marker" role="button" '
                    f'tabindex="0" data-task="{task_data}"><title>{tooltip}</title>'
                    f'<polygon points="{center:.1f},{marker_y - 7} '
                    f'{center + 7:.1f},{marker_y} {center:.1f},{marker_y + 7} '
                    f'{center - 7:.1f},{marker_y}" fill="{colors[bar["recipe"]]}" '
                    f'stroke="#ffffff" stroke-width="1"/></g>'
                )
                continue
            is_waiting = bool(bar.get("waiting"))
            is_background = bool(bar.get("background"))
            is_outline = is_waiting or is_background
            # 很短的块不硬塞文字，避免文字越过矩形盖住相邻步骤；完整信息
            # 仍保留在 title 中，鼠标悬停即可查看。
            bar_text = ""
            required_text_width = len(str(bar["stepId"])) * 7 + 12
            if bar_width >= required_text_width:
                text_fill = (
                    f'fill="{colors[bar["recipe"]]}"' if is_outline else ""
                )
                bar_text = (
                    f'<text x="{left + bar_width / 2:.1f}" y="{ypos + 22}" '
                    f'text-anchor="middle" '
                    f'class="bartext{' waitingtext' if is_outline else ''}" '
                    f'clip-path="url(#{clip_id})" {text_fill}>'
                    f'{html.escape(bar["stepId"])}</text>'
                )
            rect_style = (
                f'fill="white" fill-opacity="0.35" '
                f'stroke="{colors[bar["recipe"]]}" stroke-width="1.6" '
                f'stroke-dasharray="6 4"'
                if is_outline
                else (
                    f'fill="{colors[bar["recipe"]]}" opacity="0.86" '
                    f'stroke="#ffffff" stroke-width="0.5"'
                )
            )
            svg.append(
                f'<clipPath id="{clip_id}"><rect x="{left:.1f}" y="{ypos + 5}" '
                f'width="{bar_width:.1f}" height="24" rx="5"/></clipPath>'
                f'<g class="task-bar" role="button" tabindex="0" data-task="{task_data}">'
                f'<title>{tooltip}</title><rect x="{left:.1f}" y="{ypos + 5}" '
                f'width="{bar_width:.1f}" height="24" rx="5" '
                f'{rect_style}/>{bar_text}</g>'
            )
    svg.append("</svg>")
    legend = "".join(
        f'<span><i style="background:{color}"></i>{html.escape(name)}</span>'
        for name, color in colors.items()
    )
    notification_json = json.dumps(
        notifications,
        ensure_ascii=False,
    ).replace("</", "<\\/")
    type_labels = {
        "PREPARATION": "前置准备",
        "REMINDER": "提前提醒",
        "ACTION": "立即操作",
    }
    notice_rows: list[str] = []
    for notice_type in ("PREPARATION", "REMINDER", "ACTION"):
        nodes = []
        for notice_index, notice in enumerate(notifications):
            if notice.get("type") != notice_type:
                continue
            notify_at = float(notice.get("notifyAtMin", 0))
            left = min(100.0, max(0.0, notify_at / max(makespan, 1) * 100))
            tooltip = html.escape(
                f"{notice.get('notifyAtMin', 0)}分钟｜"
                f"{notice.get('recipeName', '')} {notice.get('stepId', '')}｜"
                f"{notice.get('operationType', '关键操作')}｜"
                f"{notice.get('message', '')}",
                quote=True,
            )
            color = colors.get(notice.get("recipeName", ""), "#64748b")
            nodes.append(
                f'<button type="button" class="notice-node" '
                f'style="left:{left:.4f}%;--notice-color:{color}" '
                f'data-notice-index="{notice_index}" title="{tooltip}" '
                f'aria-label="{tooltip}"></button>'
            )
        notice_rows.append(
            f'<div class="notice-row"><div class="notice-label">'
            f'{type_labels[notice_type]}</div><div class="notice-track">'
            f'{"".join(nodes)}</div></div>'
        )
    operation_types = "、".join(dict.fromkeys(
        str(item.get("operationType") or "关键操作")
        for item in notifications
    )) or "暂无"
    runtime_ms = metrics.get("runtimeMs")
    runtime_text = (
        f"{runtime_ms:.1f} ms"
        if isinstance(runtime_ms, (int, float)) and runtime_ms < 1000
        else f"{runtime_ms / 1000:.2f} 秒"
        if isinstance(runtime_ms, (int, float))
        else "-"
    )
    is_dynamic = isinstance(metrics.get("rescheduleAtMin"), (int, float))
    view_name = "动态重排甘特图" if is_dynamic else "静态排程甘特图"
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>食序引擎 · {view_name}</title>
<style>
:root{{--bg:#f3f7f8;--surface:#fff;--surface-2:#edf6f5;--text:#102a35;--muted:#687b83;--line:#dbe5e7;--navy:#0b2b3a;--teal:#21b6a8;--teal-dark:#087f8c;--amber:#f0a14a}}
*{{box-sizing:border-box}}body{{font-family:Inter,"Microsoft YaHei",Arial,sans-serif;margin:0;color:var(--text);background:radial-gradient(circle at 8% 0,#dff5f2 0,transparent 31%),var(--bg)}}
.shell{{max-width:1580px;margin:auto;padding:22px}}.brand-header{{display:flex;align-items:center;justify-content:space-between;gap:20px;margin-bottom:18px;padding:20px 24px;border-radius:20px;background:linear-gradient(125deg,#092838,#0d4551 60%,#087f8c);color:white;box-shadow:0 16px 40px #11354229}}.brand-lockup{{display:flex;align-items:center;gap:14px}}.brand-mark{{display:grid;place-items:center;width:52px;height:52px;border:1px solid #ffffff45;border-radius:15px;background:#ffffff16;font-weight:800;letter-spacing:.08em}}.eyebrow{{color:#8ce3d9;font-size:10px;font-weight:800;letter-spacing:.17em}}h1{{margin:2px 0 1px;font-size:24px}}.subtitle{{margin:0;color:#d8ecee;font-size:13px}}.view-chip{{padding:8px 12px;border:1px solid #ffffff30;border-radius:999px;background:#ffffff14;font-weight:700;white-space:nowrap}}
.card{{background:linear-gradient(180deg,#fff,#fcfefe);border:1px solid #dfe9ea;border-radius:16px;padding:20px;box-shadow:0 8px 28px #1739450e;margin-bottom:16px}}
.metrics{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px}}.metric{{background:var(--surface-2);border:1px solid #e4efee;padding:12px 16px;border-radius:11px}}
.legend{{display:flex;gap:16px;flex-wrap:wrap;margin:10px 0}}.legend span{{display:flex;align-items:center;gap:6px}}
.legend i{{width:12px;height:12px;border-radius:3px}}.scroll{{overflow-x:auto}}
svg{{min-width:1100px}}.grid{{stroke:#dbe5e7;stroke-width:1}}.rowline{{stroke:#e7eeee}}
.axis{{font-size:11px;fill:#687b83}}.label{{font-size:12px;fill:#294750}}.bartext{{font-size:10px;fill:white;pointer-events:none}}.waitingtext{{font-weight:700}}
.reschedule-line{{stroke:#dc2626;stroke-width:2;stroke-dasharray:7 5;pointer-events:none}}.reschedule-label{{font-size:12px;font-weight:700;fill:#dc2626;stroke:white;stroke-width:4;paint-order:stroke;pointer-events:none}}
.task-bar{{cursor:pointer}}.task-bar:hover rect,.task-bar:focus-visible rect,.task-bar:hover polygon,.task-bar:focus-visible polygon{{stroke:#102a35;stroke-width:1.5}}
.detail{{margin-top:12px;min-height:52px;padding:12px 14px;border-radius:10px;background:var(--surface-2);color:var(--muted)}}
.detail strong{{color:var(--text)}}
.notice-panel h2{{margin:0 0 12px;font-size:18px}}.progress-head{{display:flex;justify-content:space-between;gap:12px;color:var(--muted)}}
.progress-row{{display:grid;grid-template-columns:110px minmax(850px,1fr);align-items:center;margin:8px 0 10px}}.progress-label{{padding-right:12px;text-align:right;color:#46616a}}
.time-slider{{width:100%;margin:0;accent-color:var(--teal-dark);cursor:pointer}}
.notice-row{{display:grid;grid-template-columns:110px minmax(850px,1fr);min-height:42px;align-items:center}}
.notice-label{{padding-right:12px;text-align:right;color:#46616a}}.notice-track{{position:relative;height:30px;border-bottom:1px solid var(--line);background-image:linear-gradient(to right,var(--line) 1px,transparent 1px);background-size:10% 100%}}
.notice-node{{position:absolute;top:8px;width:13px;height:13px;padding:0;transform:translateX(-50%) rotate(45deg);border:1px solid white;border-radius:2px;background:var(--notice-color);cursor:pointer}}
.notice-node:hover,.notice-node:focus-visible{{z-index:2;outline:2px solid var(--text);outline-offset:2px}}
.notice-kinds{{margin-top:10px;color:var(--muted);font-size:13px}}
.popup-backdrop{{position:fixed;inset:0;z-index:20;display:grid;place-items:center;padding:20px;background:#0f172a66}}
.popup-backdrop.hidden{{display:none}}.popup{{width:min(560px,100%);padding:20px;border-radius:14px;background:white;box-shadow:0 18px 60px #0003}}
.popup-head{{display:flex;justify-content:space-between;gap:15px;align-items:center;margin-bottom:10px}}.popup-head strong{{font-size:18px}}.popup-close{{border:0;border-radius:8px;padding:7px 11px;background:var(--surface-2);cursor:pointer}}
@media(max-width:700px){{.shell{{padding:10px}}.brand-header{{align-items:flex-start;flex-direction:column}}.metrics{{grid-template-columns:1fr}}}}
</style></head><body><main class="shell">
<header class="brand-header"><div class="brand-lockup"><div class="brand-mark">MS</div><div><div class="eyebrow">MEALSEQ ENGINE · 食序引擎</div><h1>{view_name}</h1><p class="subtitle">多菜并行 · 资源协同 · 准时出锅</p></div></div><div class="view-chip">{('动态重调度' if is_dynamic else '静态调度')}</div></header>
<div class="card metrics"><div class="metric">总耗时：{metrics['makespanMin']} 分钟</div>
<div class="metric">出锅时间差：{metrics['finishSpreadMin']} 分钟</div>
<div class="metric">相对顺序制作节省：{metrics.get('timeSavedMin', '-')} 分钟</div>
{f'<div class="metric" style="background:#fff1f2;color:#b91c1c">重调度时刻：第 {clean_number(metrics["rescheduleAtMin"])} 分钟</div>' if isinstance(metrics.get('rescheduleAtMin'), (int, float)) else ''}
<div class="metric">算法运行时长：{runtime_text}</div>
<div class="metric">资源冲突：0 个</div>
<div class="metric">最大连续人工：{metrics.get('maxContinuousHumanMin', '-')} 分钟</div></div>
<div class="card"><div class="legend">{legend}</div><div class="scroll">{''.join(svg)}</div>
<div class="detail" id="task-detail">点击任意色块查看菜品、步骤、时间、资源和操作详情。</div></div>
<div class="card notice-panel"><h2>紧急设备操作通知时间线</h2>
<div class="progress-head"><span>拖动进度模拟烹饪过程，到达紧急操作节点时会弹窗</span><strong id="progress-time">0 分钟</strong></div>
<div class="scroll"><div style="min-width:1100px"><div class="progress-row"><div class="progress-label">时间进度</div><input class="time-slider" id="time-progress" type="range" min="0" max="{makespan}" step="0.5" value="0" aria-label="烹饪时间进度"></div>{''.join(notice_rows)}</div></div>
<div class="notice-kinds">通知操作类型：{html.escape(operation_types)}</div>
<div class="detail" id="notification-detail">点击通知节点查看提醒详情。</div></div>
<div class="popup-backdrop hidden" id="reminder-popup" role="dialog" aria-modal="true" aria-labelledby="reminder-title">
  <div class="popup"><div class="popup-head"><strong id="reminder-title">操作提醒</strong><button type="button" class="popup-close" id="popup-close">关闭</button></div><div id="popup-content"></div></div>
</div>
<script>
const NOTIFICATIONS = {notification_json};
function escapeHtml(value) {{
  return String(value ?? "").replace(/[&<>"']/g, character => ({{
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }})[character]);
}}
function showTaskDetail(element) {{
  const task = JSON.parse(element.dataset.task);
  document.getElementById("task-detail").innerHTML =
    `<strong>${{escapeHtml(task.recipe)}} ${{escapeHtml(task.stepId)}}</strong>｜` +
    `${{escapeHtml(task.start)}}–${{escapeHtml(task.end)}} 分钟｜${{escapeHtml(task.row)}}<br>` +
    `${{escapeHtml(task.action)}}`;
}}
document.querySelectorAll(".task-bar").forEach(element => {{
  element.addEventListener("click", () => showTaskDetail(element));
  element.addEventListener("keydown", event => {{
    if (event.key === "Enter" || event.key === " ") {{
      event.preventDefault();
      showTaskDetail(element);
    }}
  }});
}});
function notificationMarkup(items) {{
  return items.map(item =>
    `<div style="margin:8px 0"><strong>${{escapeHtml(item.notifyAtMin)}} 分｜${{escapeHtml(item.recipeName)}} ${{escapeHtml(item.stepId)}}｜${{escapeHtml(item.operationType || "关键操作")}}</strong><br>${{escapeHtml(item.message)}}</div>`
  ).join("");
}}
function showNotifications(items, openPopup=false) {{
  if (!items.length) return;
  const markup = notificationMarkup(items);
  document.getElementById("notification-detail").innerHTML = markup;
  if (openPopup) {{
    document.getElementById("popup-content").innerHTML = markup;
    document.getElementById("reminder-popup").classList.remove("hidden");
  }}
}}
document.querySelectorAll("[data-notice-index]").forEach(node => {{
  node.addEventListener("click", () => {{
    showNotifications([NOTIFICATIONS[Number(node.dataset.noticeIndex)]], false);
  }});
}});
const progress = document.getElementById("time-progress");
const fired = new Set();
let previousProgress = 0;
progress.addEventListener("input", () => {{
  const current = Number(progress.value);
  document.getElementById("progress-time").textContent = `${{current}} 分钟`;
  if (current < previousProgress) {{
    NOTIFICATIONS.forEach((item, index) => {{
      if (Number(item.notifyAtMin) > current) fired.delete(index);
    }});
  }} else {{
    const due = [];
    NOTIFICATIONS.forEach((item, index) => {{
      const time = Number(item.notifyAtMin);
      if (!fired.has(index) && time >= previousProgress && time <= current) {{
        fired.add(index); due.push(item);
      }}
    }});
    showNotifications(due, true);
  }}
  previousProgress = current;
}});
document.getElementById("popup-close").addEventListener("click", () => {{
  document.getElementById("reminder-popup").classList.add("hidden");
}});
document.getElementById("reminder-popup").addEventListener("click", event => {{
  if (event.target.id === "reminder-popup") event.currentTarget.classList.add("hidden");
}});
</script>
</main></body></html>"""


def main() -> int:
    parser = argparse.ArgumentParser(description="生成按设备实例分行的排程甘特图")
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent / "cooking_schedule_gantt.html")
    parser.add_argument("--recipes", type=Path, default=DEFAULT_RECIPES)
    parser.add_argument("--devices", type=Path, default=DEFAULT_DEVICES)
    parser.add_argument("--solver", choices=("cp_sat", "genetic", "heuristic"), default="genetic")
    args = parser.parse_args()

    request_items = json.loads(args.request.read_text(encoding="utf-8-sig"))
    plan = schedule_from_request(request_items, args.recipes, args.devices, solver=args.solver)
    rows, bars, colors = build_bars(plan, load_device_rules(args.devices))
    from notification_generator import build_notifications
    notifications = build_notifications(plan.scheduled_tasks, elapsed_min=0.0)
    args.output.write_text(
        render_html(rows, bars, colors, plan.metrics, notifications),
        encoding="utf-8",
    )
    print(f"甘特图已生成：{args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
