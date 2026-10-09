"""一条命令演示：静态排程 -> 动态加菜 -> 自动导出全部结果。"""

from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib import request

from api_server import (
    DEFAULT_DEVICES,
    DEFAULT_NOTIFICATIONS,
    DEFAULT_RECIPES,
    DEFAULT_STATE,
    ScheduleRequestHandler,
)
from schedule_service import CookingScheduleService


SCHEDULER_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = SCHEDULER_DIR / "demo_output"


def read_request(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def post_json(url: str, items: list[dict]) -> dict:
    """像正式调用方一样，通过 POST /schedule 提交菜谱数组。"""

    body = json.dumps(items, ensure_ascii=False).encode("utf-8")
    http_request = request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with request.urlopen(http_request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    ScheduleRequestHandler.service = CookingScheduleService(
        recipe_dir=DEFAULT_RECIPES,
        device_path=DEFAULT_DEVICES,
        state_path=DEFAULT_STATE,
        notification_path=DEFAULT_NOTIFICATIONS,
    )
    ScheduleRequestHandler.output_dir = OUTPUT_DIR

    # 使用随机空闲端口，避免和用户已经启动的8000端口冲突。
    server = ThreadingHTTPServer(("127.0.0.1", 0), ScheduleRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/schedule"

    try:
        initial = read_request(SCHEDULER_DIR / "example_request.json")
        addition = read_request(SCHEDULER_DIR / "demo_add_recipe_request.json")
        static_result = post_json(url, initial)
        dynamic_result = post_json(url, addition)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    print("完整流程演示成功")
    print(f"静态菜品数：{static_result['overview']['recipeCount']}")
    print(f"动态菜品数：{dynamic_result['overview']['recipeCount']}")
    print(f"全部结果：{OUTPUT_DIR}")
    print(f"静态甘特图：{OUTPUT_DIR / '03_static_gantt.html'}")
    print(f"动态甘特图：{OUTPUT_DIR / '07_dynamic_gantt.html'}")
    print(f"前后对比图：{OUTPUT_DIR / '08_dynamic_comparison.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
