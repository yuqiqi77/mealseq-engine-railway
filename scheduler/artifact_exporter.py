"""把接口每次运行的结果集中导出为评委可直接查看的文件。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from generate_dynamic_gantt import HTML_TEMPLATE, build_payload
from generate_gantt import build_bars, render_html
from scheduler import load_device_rules


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _safe_folder_name(task_id: str) -> str:
    """default 直接写到输出根目录；显式任务编号各用一个子目录。"""

    return "" if task_id == "default" else task_id


def _current_gantt_data(service, result) -> tuple[list[str], list[dict], dict]:
    """返回本次真实资源行和条形数据，供两种甘特图共用。"""

    plan = SimpleNamespace(scheduled_tasks=result.scheduled_tasks)
    return build_bars(plan, load_device_rules(service.device_path))


def _write_current_gantt(path: Path, rows, bars, colors, metrics, notifications) -> None:
    """根据本次真实排程绘图，保留具体灶眼和箱体层位。"""

    path.write_text(
        render_html(rows, bars, colors, metrics, notifications),
        encoding="utf-8",
    )


def _comparison_tasks(bars: list[dict]) -> list[dict]:
    """把静态图条形转换成动态对比页需要的统一结构。"""

    return [
        {
            "start": item["start"],
            "end": item["end"],
            "duration": item["end"] - item["start"],
            "recipe": item["recipe"],
            "stepId": item.get("stepId", "?"),
            "resource": item["row"],
            "title": item.get("title", ""),
            "action": item["action"],
            "marker": bool(item.get("marker")),
            "waiting": bool(item.get("waiting")),
            "background": bool(item.get("background")),
        }
        for item in bars
    ]


def export_result_artifacts(
    service,
    task_id: str,
    request_items: list[dict],
    result,
    output_root: Path,
) -> Path:
    """保存请求、标准响应、通知及静态/动态甘特图。"""

    folder_name = _safe_folder_name(task_id)
    output_dir = output_root / folder_name if folder_name else output_root
    output_dir.mkdir(parents=True, exist_ok=True)

    notice_data = {
        "taskId": task_id,
        "notifications": result.notifications,
        "changes": result.changes,
    }
    rows, bars, colors = _current_gantt_data(service, result)
    comparison_tasks = _comparison_tasks(bars)

    if result.mode == "initial":
        _write_json(output_dir / "01_static_request.json", request_items)
        _write_json(output_dir / "02_static_result.json", result.api_response)
        _write_json(output_dir / "03_static_notifications.json", notice_data)
        _write_current_gantt(
            output_dir / "03_static_gantt.html",
            rows,
            bars,
            colors,
            result.metrics,
            result.notifications,
        )
        _write_json(output_dir / "static_gantt_data.json", comparison_tasks)
        _write_json(output_dir / "static_metrics.json", result.metrics)
    else:
        _write_json(output_dir / "04_add_recipe_request.json", request_items)
        _write_json(output_dir / "05_dynamic_result.json", result.api_response)
        _write_json(output_dir / "06_notifications.json", notice_data)
        _write_current_gantt(
            output_dir / "07_dynamic_gantt.html",
            rows,
            bars,
            colors,
            result.metrics,
            result.notifications,
        )
        _write_json(output_dir / "dynamic_gantt_data.json", comparison_tasks)

        initial_path = output_dir / "02_static_result.json"
        initial_tasks_path = output_dir / "static_gantt_data.json"
        if initial_path.exists() and initial_tasks_path.exists():
            initial = json.loads(initial_path.read_text(encoding="utf-8-sig"))
            initial_tasks = json.loads(
                initial_tasks_path.read_text(encoding="utf-8-sig")
            )
            static_metrics_path = output_dir / "static_metrics.json"
            initial_metrics = (
                json.loads(static_metrics_path.read_text(encoding="utf-8-sig"))
                if static_metrics_path.exists()
                else {}
            )
            payload = build_payload(
                initial,
                result.api_response,
                notice_data,
                initial_tasks=initial_tasks,
                dynamic_tasks=comparison_tasks,
                initial_metrics=initial_metrics,
                dynamic_metrics=result.metrics,
            )
            embedded = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
            comparison = HTML_TEMPLATE.replace("__DATA__", embedded)
            (output_dir / "08_dynamic_comparison.html").write_text(
                comparison,
                encoding="utf-8",
            )

    return output_dir
