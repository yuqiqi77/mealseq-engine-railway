"""无需第三方依赖的排程 HTTP 服务。

POST /schedule 的请求体始终是菜谱数组：
- 同一 task_id 当前没有活动排程：创建首次排程；
- 同一 task_id 当前存在活动排程：数组中的一道菜作为临时新增菜谱动态加入。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from artifact_exporter import export_result_artifacts
from schedule_service import CookingScheduleService
from web_ui import INDEX_HTML, load_recipe_catalog


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RECIPES = PROJECT_ROOT / "ai" / "parsed_recipes_v5"
DEFAULT_DEVICES = PROJECT_ROOT / "para" / "设备参数清单参考.json"
DEFAULT_STATE = Path(__file__).resolve().parent / "runtime" / "active_plan.json"
DEFAULT_NOTIFICATIONS = Path(__file__).resolve().parent / "runtime" / "notifications.json"


class ScheduleRequestHandler(BaseHTTPRequestHandler):
    service: CookingScheduleService
    output_dir: Path | None = None

    def _send_json(
        self,
        status: int,
        body: object,
        headers: dict[str, str] | None = None,
    ) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def _send_bytes(self, status: int, data: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 要求此命名
        """提供本地评委页面、菜谱列表和生成结果，不改变正式POST接口。"""

        parsed_url = urlsplit(self.path)
        if parsed_url.path == "/":
            self._send_bytes(
                200,
                INDEX_HTML.encode("utf-8"),
                "text/html; charset=utf-8",
            )
            return
        if parsed_url.path == "/recipes":
            self._send_json(200, load_recipe_catalog(self.service.recipe_dir))
            return
        if parsed_url.path == "/session":
            query = parse_qs(parsed_url.query, keep_blank_values=True)
            if set(query) - {"task_id"}:
                self._send_json(400, {"error": "只支持可选的 task_id 查询参数"})
                return
            task_id = query.get("task_id", ["default"])[0]
            try:
                self._send_json(200, self.service.task_summary(task_id))
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
            return
        if parsed_url.path.startswith("/artifacts/"):
            self._serve_artifact(parsed_url.path.removeprefix("/artifacts/"))
            return
        self._send_json(404, {"error": "页面或接口不存在"})

    def _serve_artifact(self, filename: str) -> None:
        """只允许访问评委需要的固定输出文件，避免任意文件路径读取。"""

        allowed = {
            "01_static_request.json",
            "02_static_result.json",
            "03_static_gantt.html",
            "03_static_notifications.json",
            "04_add_recipe_request.json",
            "05_dynamic_result.json",
            "06_notifications.json",
            "07_dynamic_gantt.html",
            "08_dynamic_comparison.html",
        }
        if self.output_dir is None or filename not in allowed:
            self._send_json(404, {"error": "结果文件不存在"})
            return
        path = self.output_dir / filename
        if not path.is_file():
            self._send_json(404, {"error": "请先在网页中运行排程"})
            return
        content_type = (
            "text/html; charset=utf-8"
            if path.suffix == ".html"
            else "application/json; charset=utf-8"
        )
        self._send_bytes(200, path.read_bytes(), content_type)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 要求此命名
        parsed_url = urlsplit(self.path)
        if parsed_url.path not in {"/schedule", "/new-plan"}:
            self._send_json(404, {"error": "接口不存在"})
            return
        try:
            query = parse_qs(parsed_url.query, keep_blank_values=True)
            if set(query) - {"task_id", "reschedule_at_min"}:
                raise ValueError("接口只支持可选的 task_id 和 reschedule_at_min 查询参数")
            if "task_id" in query and len(query["task_id"]) != 1:
                raise ValueError("task_id 只能提供一次")
            if "reschedule_at_min" in query and len(query["reschedule_at_min"]) != 1:
                raise ValueError("reschedule_at_min 只能提供一次")
            # task_id 现在是可选项。单评委、单会话演示直接 POST /schedule；
            # 多用户或并发任务仍可用 task_id 隔离各自的动态排程状态。
            task_id = query.get("task_id", ["default"])[0]
            raw_reschedule_minute = query.get("reschedule_at_min", [""])[0].strip()
            reschedule_at_min = None
            if raw_reschedule_minute:
                try:
                    reschedule_at_min = float(raw_reschedule_minute)
                except ValueError as exc:
                    raise ValueError("reschedule_at_min 必须是分钟数") from exc
                if not math.isfinite(reschedule_at_min) or reschedule_at_min < 0:
                    raise ValueError("reschedule_at_min 必须是大于或等于0的有限分钟数")
            if parsed_url.path == "/new-plan":
                if reschedule_at_min is not None:
                    raise ValueError("/new-plan 不支持 reschedule_at_min")
                self.service.reset_task(task_id)
                self._send_json(200, {"ok": True})
                return
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 1_000_000:
                raise ValueError("请求体为空或超过 1MB")
            request_items = json.loads(self.rfile.read(length).decode("utf-8"))
            result = self.service.handle_request(
                request_items,
                task_id=task_id,
                reschedule_at_min=reschedule_at_min,
            )
            if self.output_dir is not None:
                export_result_artifacts(
                    service=self.service,
                    task_id=task_id,
                    request_items=request_items,
                    result=result,
                    output_root=self.output_dir,
                )
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except Exception as exc:  # 避免服务直接退出，同时保留错误类型便于调试
            self._send_json(500, {"error": f"排程失败：{type(exc).__name__}: {exc}"})
            return

        # 正式响应仍严格使用原接口文档结构；通知按 task_id 分目录保存。
        self._send_json(
            200,
            result.api_response,
            headers={
                "X-Schedule-Runtime-Ms": str(result.metrics.get("runtimeMs", "")),
                "X-Max-Continuous-Human-Min": str(
                    result.metrics.get("maxContinuousHumanMin", "")
                ),
                "X-Finish-Spread-Min": str(result.metrics.get("finishSpreadMin", "")),
                "X-Reschedule-At-Min": str(result.metrics.get("rescheduleAtMin", "")),
            },
        )
        print(
            f"任务 {task_id}，排程版本 {result.plan_version}，模式={result.mode}，"
            f"调整 {len(result.changes)} 项，通知 {len(result.notifications)} 项"
        )

    def log_message(self, format: str, *args: object) -> None:
        print(f"[{self.log_date_time_string()}] {format % args}")


def main() -> int:
    parser = argparse.ArgumentParser(description="食序引擎 MealSeq Engine 动态排程服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--recipes", type=Path, default=DEFAULT_RECIPES)
    parser.add_argument("--devices", type=Path, default=DEFAULT_DEVICES)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--notifications", type=Path, default=DEFAULT_NOTIFICATIONS)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "demo_output",
        help="自动保存请求、响应和甘特图的目录",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="启动后不自动打开本地评委页面",
    )
    args = parser.parse_args()

    ScheduleRequestHandler.service = CookingScheduleService(
        recipe_dir=args.recipes,
        device_path=args.devices,
        state_path=args.state,
        notification_path=args.notifications,
    )
    ScheduleRequestHandler.output_dir = args.output_dir
    server = ThreadingHTTPServer((args.host, args.port), ScheduleRequestHandler)
    print(f"食序引擎 MealSeq Engine 已启动：http://{args.host}:{args.port}/schedule")
    print("单会话可省略 task_id；并发任务可使用 ?task_id=任务编号")
    print(f"演示结果目录：{args.output_dir}")
    print(f"当前求解器：{os.environ.get('COOKING_SCHEDULER_SOLVER', 'genetic')}")
    print(f"各任务状态目录：{args.state.parent / 'tasks'}")
    page_url = f"http://127.0.0.1:{args.port}/"
    print(f"评委操作页面：{page_url}")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(page_url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("排程服务已停止")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
