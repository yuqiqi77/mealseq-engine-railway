"""同一接口动态加菜的本地测试。"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib import request

from api_server import ScheduleRequestHandler
from artifact_exporter import export_result_artifacts
from plan_state import PlanStateStore
from schedule_service import CookingScheduleService
from scheduler import build_tasks, load_requested_recipes
from dynamic_scheduler import rebuild_old_schedule


PROJECT_ROOT = Path(__file__).resolve().parent.parent
RECIPE_DIR = PROJECT_ROOT / "ai" / "parsed_recipes_v5"
DEVICE_PATH = PROJECT_ROOT / "para" / "设备参数清单参考.json"

INITIAL_REQUEST = [
    {"id": "5d54bae2a9114174727c8b20", "name": "鸡翅包饭"},
    {"id": "5dad79866601a865649f9cb7", "name": "家常鲈鱼"},
]
ADDED_REQUEST = [
    {"id": "58e70b129f6429675ec80601", "name": "麻辣对虾"},
]
FIVE_RECIPE_REQUEST = [
    {"id": "58e70ae1a3fd4a750f4b75af", "name": "双椒鳙鱼头"},
    {"id": "58e70ae1a3fd4a750f4b75b0", "name": "韩式泡菜鸦片鱼头"},
    {"id": "58e70b0e9f6429675ec805d8", "name": "清蒸鲈鱼"},
    {"id": "58e70b109f6429675ec805ed", "name": "蒜蓉粉丝蒸扇贝"},
    {"id": "58e70b129f6429675ec80601", "name": "麻辣对虾"},
]

SIMULTANEOUS_FINISH_REQUEST = [
    {"id": "6492a7c6933a4b7277dee237", "name": "Q弹果冻鸡蛋羹"},
    {"id": "6577d480c458a177d8438637", "name": "三味蒸鳗鱼&香茅烤鳗鱼同烹"},
    {"id": "657951cac458a177d84387a1", "name": "五味乌鸡煲"},
    {"id": "6510fb7845256c3ad7c25cd8", "name": "京韵酱香烤鸡"},
    {"id": "66cec69b1142545e3856e07e", "name": "亲朋欢聚套餐"},
]
LOW_TEMPERATURE_STEAK_REQUEST = [
    {"id": "61e6c51fec6e1d65587067e1", "name": "低温牛排"},
]
CHEESECAKE_REQUEST = [
    {"id": "5d565e87a9114174727c8bad", "name": "半熟芝士蛋糕"},
]
DEMO_FOUR_RECIPE_REQUEST = [
    {"id": "5caf2ec7e78bfc455f2d5cb0", "name": "清蒸带鱼"},
    {"id": "5c871bef98d5bc7e184a90dd", "name": "香烤鲫鱼"},
    {"id": "59cc89b47d21be2a79172c98", "name": "豉汁蒸草鱼"},
    {"id": "58e70b0e9f6429675ec805d8", "name": "清蒸鲈鱼"},
]
DEMO_ADDED_REQUEST = [
    {"id": "58e70b129f6429675ec80601", "name": "麻辣对虾"},
]
INSERTION_BASE_REQUEST = SIMULTANEOUS_FINISH_REQUEST[:4]
INSERTION_ADDED_REQUEST = SIMULTANEOUS_FINISH_REQUEST[4:]


class DynamicSchedulerTests(unittest.TestCase):
    def test_rebuild_rejects_incomplete_saved_plan(self) -> None:
        tasks = build_tasks(load_requested_recipes(INITIAL_REQUEST, RECIPE_DIR))
        with self.assertRaisesRegex(ValueError, "缺少"):
            rebuild_old_schedule(tasks, {tasks[0].key: 0})

    def make_service(self, directory: Path) -> CookingScheduleService:
        return CookingScheduleService(
            recipe_dir=RECIPE_DIR,
            device_path=DEVICE_PATH,
            state_path=directory / "active_plan.json",
            notification_path=directory / "notifications.json",
        )

    @staticmethod
    def task_file(directory: Path, task_id: str, filename: str) -> Path:
        return directory / "tasks" / task_id / filename

    def test_same_endpoint_adds_only_new_recipe_and_preserves_started_work(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            service = self.make_service(directory)
            start = datetime(2026, 1, 1, 12, 0)
            task_id = "test_task_001"

            first = service.handle_request(INITIAL_REQUEST, task_id=task_id, now=start)
            self.assertEqual(first.mode, "initial")
            self.assertEqual(first.api_response["overview"]["recipeCount"], 2)
            self.assertGreaterEqual(first.metrics["runtimeMs"], 0)
            self.assertLess(first.metrics["runtimeMs"], 5000)
            self.assertIn("maxContinuousHumanMin", first.metrics)
            artifact_dir = directory / "artifacts"
            export_result_artifacts(
                service,
                task_id,
                INITIAL_REQUEST,
                first,
                artifact_dir,
            )
            static_folder = artifact_dir / task_id
            self.assertTrue((static_folder / "03_static_notifications.json").is_file())
            static_html = (static_folder / "03_static_gantt.html").read_text(
                encoding="utf-8"
            )
            self.assertIn('id="time-progress"', static_html)
            self.assertIn('id="reminder-popup"', static_html)
            self.assertIn("最大连续人工", static_html)
            self.assertIn("相对顺序制作节省", static_html)
            old_state = PlanStateStore(
                self.task_file(directory, task_id, "active_plan.json")
            ).load()
            old_starts = dict(old_state.task_starts)

            second = service.handle_request(
                ADDED_REQUEST,
                task_id=task_id,
                now=start + timedelta(minutes=20),
            )
            self.assertEqual(second.mode, "dynamic")
            self.assertLess(second.metrics["runtimeMs"], 3000)
            self.assertEqual(second.metrics["rescheduleAtMin"], 20)
            self.assertEqual(second.api_response["overview"]["recipeCount"], 3)
            export_result_artifacts(
                service,
                task_id,
                ADDED_REQUEST,
                second,
                artifact_dir,
            )
            comparison_html = (static_folder / "08_dynamic_comparison.html").read_text(
                encoding="utf-8"
            )
            dynamic_html = (static_folder / "07_dynamic_gantt.html").read_text(
                encoding="utf-8"
            )
            self.assertIn("第20分钟重调度", dynamic_html)
            self.assertIn("重调度时刻：第 20 分钟", dynamic_html)
            self.assertNotIn('data-view="events"', comparison_html)
            self.assertNotIn("调整与通知", comparison_html)
            self.assertIn("estimatedBarWidth", comparison_html)
            added_id = ADDED_REQUEST[0]["id"]
            self.assertGreaterEqual(
                min(
                    item.start
                    for item in second.scheduled_tasks
                    if item.task.recipe_id == added_id
                ),
                20,
            )
            self.assertEqual(
                set(second.api_response),
                {"overview", "cookingTimeline", "ingredientsSummary", "detailTimeline", "recipeDetail"},
            )

            new_state = PlanStateStore(
                self.task_file(directory, task_id, "active_plan.json")
            ).load()
            self.assertEqual(new_state.version, 2)
            self.assertEqual(len(new_state.recipes), 3)

            recipes = load_requested_recipes(INITIAL_REQUEST, RECIPE_DIR)
            tasks = {task.key: task for task in build_tasks(recipes)}
            for key, old_start in old_starts.items():
                old_end = old_start + tasks[key].duration
                if old_start <= 22 or old_end <= 20:
                    self.assertEqual(new_state.task_starts[key], old_start)

            notification_data = json.loads(
                self.task_file(directory, task_id, "notifications.json").read_text(encoding="utf-8")
            )
            self.assertEqual(notification_data["taskId"], task_id)
            self.assertTrue(notification_data["notifications"])
            notification_types = {
                item["type"] for item in notification_data["notifications"]
            }
            self.assertIn("ACTION", notification_types)
            self.assertIn("REMINDER", notification_types)
            self.assertNotIn("DEVICE_FINISH", notification_types)
            self.assertNotIn("DEVICE_REMINDER", notification_types)
            self.assertTrue(all(
                item.get("operationType")
                for item in notification_data["notifications"]
            ))
            self.assertTrue(any(
                "3分钟后" in item["message"]
                for item in notification_data["notifications"]
            ))

    def test_duplicate_recipe_is_rejected_during_active_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            service = self.make_service(directory)
            start = datetime(2026, 1, 1, 12, 0)
            task_id = "duplicate_test"
            service.handle_request(INITIAL_REQUEST, task_id=task_id, now=start)
            with self.assertRaisesRegex(ValueError, "已经存在"):
                service.handle_request(
                    [INITIAL_REQUEST[0]],
                    task_id=task_id,
                    now=start + timedelta(minutes=5),
                )

    def test_active_plan_accepts_exactly_one_added_recipe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            service = self.make_service(directory)
            start = datetime(2026, 1, 1, 12, 0)
            task_id = "one_recipe_only"
            service.handle_request(INITIAL_REQUEST, task_id=task_id, now=start)
            with self.assertRaisesRegex(ValueError, "只能包含1道菜"):
                service.handle_request(
                    [ADDED_REQUEST[0], {"id": "61e6c51fec6e1d65587067e1", "name": "低温牛排"}],
                    task_id=task_id,
                    now=start + timedelta(minutes=5),
                )

    def test_different_task_ids_have_independent_plans(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            service = self.make_service(directory)
            start = datetime(2026, 1, 1, 12, 0)

            first = service.handle_request(INITIAL_REQUEST, task_id="table_a", now=start)
            second = service.handle_request(ADDED_REQUEST, task_id="table_b", now=start)

            self.assertEqual(first.mode, "initial")
            self.assertEqual(second.mode, "initial")
            self.assertEqual(first.api_response["overview"]["recipeCount"], 2)
            self.assertEqual(second.api_response["overview"]["recipeCount"], 1)
            self.assertTrue(self.task_file(directory, "table_a", "active_plan.json").exists())
            self.assertTrue(self.task_file(directory, "table_b", "active_plan.json").exists())

    def test_finished_plan_automatically_starts_a_new_initial_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            service = self.make_service(directory)
            start = datetime(2026, 1, 1, 12, 0)
            task_id = "finished_test"
            service.handle_request(INITIAL_REQUEST, task_id=task_id, now=start)
            result = service.handle_request(
                FIVE_RECIPE_REQUEST[:3],
                task_id=task_id,
                now=start + timedelta(days=2),
            )
            self.assertEqual(result.mode, "initial")
            self.assertEqual(result.api_response["overview"]["recipeCount"], 3)

    def test_refresh_keeps_active_plan_and_explicit_reset_starts_over(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            service = self.make_service(directory)
            start = datetime(2026, 1, 1, 12, 0)
            task_id = "refresh_test"
            service.handle_request(INITIAL_REQUEST, task_id=task_id, now=start)

            summary = service.task_summary(task_id, now=start + timedelta(minutes=1))
            self.assertTrue(summary["hasPlan"])
            self.assertEqual(summary["version"], 1)
            self.assertEqual(len(summary["recipes"]), 2)

            service.reset_task(task_id)
            self.assertFalse(service.task_summary(task_id, now=start)["hasPlan"])
            restarted = service.handle_request(
                FIVE_RECIPE_REQUEST[:3],
                task_id=task_id,
                now=start + timedelta(minutes=2),
            )
            self.assertEqual(restarted.mode, "initial")
            self.assertEqual(restarted.api_response["overview"]["recipeCount"], 3)

    def test_http_endpoint_uses_the_same_path_for_initial_and_dynamic_requests(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            ScheduleRequestHandler.service = self.make_service(directory)
            server = ThreadingHTTPServer(("127.0.0.1", 0), ScheduleRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            # 单会话调用不再要求 task_id；请求体仍是接口规定的菜谱数组。
            url = f"http://127.0.0.1:{server.server_port}/schedule"

            def post(items: list[dict]) -> dict:
                body = json.dumps(items, ensure_ascii=False).encode("utf-8")
                req = request.Request(
                    url,
                    data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with request.urlopen(req, timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    self.assertIsNotNone(response.headers.get("X-Schedule-Runtime-Ms"))
                    self.assertIsNotNone(
                        response.headers.get("X-Max-Continuous-Human-Min")
                    )
                    return json.load(response)

            try:
                first = post(INITIAL_REQUEST)
                second = post(ADDED_REQUEST)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            self.assertEqual(first["overview"]["recipeCount"], 2)
            self.assertEqual(second["overview"]["recipeCount"], 3)

    def test_four_plus_one_dynamic_request_finishes_within_three_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            service = self.make_service(directory)
            start = datetime(2026, 1, 1, 12, 0)
            service.handle_request(
                FIVE_RECIPE_REQUEST[:4],
                task_id="runtime_dynamic",
                now=start,
            )
            result = service.handle_request(
                FIVE_RECIPE_REQUEST[4:],
                task_id="runtime_dynamic",
                now=start + timedelta(minutes=10),
            )
            self.assertEqual(result.mode, "dynamic")
            self.assertLess(result.metrics["runtimeMs"], 3000)
            # 第10分钟已有步骤完成或正在执行，不能为追求完全同出锅而改动；
            # 对仍可移动的步骤重插后，这组含真实等待的菜谱应压到40分钟内。
            self.assertLessEqual(result.metrics["finishSpreadMin"], 40)

    def test_optional_insertion_minute_overrides_request_arrival_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            service = self.make_service(directory)
            start = datetime(2026, 1, 1, 12, 0)
            service.handle_request(
                DEMO_FOUR_RECIPE_REQUEST,
                task_id="manual_insertion_minute",
                now=start,
            )
            result = service.handle_request(
                DEMO_ADDED_REQUEST,
                task_id="manual_insertion_minute",
                now=start + timedelta(minutes=1),
                reschedule_at_min=10,
            )

            self.assertEqual(result.mode, "dynamic")
            self.assertEqual(result.metrics["rescheduleAtMin"], 10)
            self.assertGreaterEqual(
                min(
                    item.start
                    for item in result.scheduled_tasks
                    if item.task.recipe_id == DEMO_ADDED_REQUEST[0]["id"]
                ),
                10,
            )

    def test_insertion_minute_requires_an_active_unfinished_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = self.make_service(Path(temporary))
            start = datetime(2026, 1, 1, 12, 0)
            with self.assertRaisesRegex(ValueError, "活动排程"):
                service.handle_request(
                    DEMO_FOUR_RECIPE_REQUEST,
                    task_id="invalid_manual_insertion",
                    now=start,
                    reschedule_at_min=10,
                )

            initial = service.handle_request(
                DEMO_FOUR_RECIPE_REQUEST,
                task_id="invalid_manual_insertion",
                now=start,
            )
            with self.assertRaisesRegex(ValueError, "早于当前排程"):
                service.handle_request(
                    DEMO_ADDED_REQUEST,
                    task_id="invalid_manual_insertion",
                    now=start,
                    reschedule_at_min=initial.metrics["makespanMin"],
                )

    def test_dynamic_keeps_long_machine_phase_early_and_aligns_final_step(self) -> None:
        """新增低温菜时不应把整段机器运行推到原排程末尾之后。"""

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            service = self.make_service(directory)
            start = datetime(2026, 1, 1, 12, 0)
            service.handle_request(
                SIMULTANEOUS_FINISH_REQUEST,
                task_id="simultaneous_finish",
                now=start,
            )
            result = service.handle_request(
                LOW_TEMPERATURE_STEAK_REQUEST,
                task_id="simultaneous_finish",
                now=start + timedelta(minutes=23),
            )

            self.assertLess(result.metrics["runtimeMs"], 3000)
            self.assertLessEqual(result.metrics["makespanMin"], 200)
            # “鸡蛋羹完成”现在是零时长标记；真实鲜嫩蒸、普通蒸、多层蒸
            # 不能并行，修正后的出锅差不能再依靠虚假收尾步骤压低。
            self.assertGreaterEqual(result.metrics["finishSpreadMin"], 45)
            self.assertLessEqual(result.metrics["finishSpreadMin"], 100)

    def test_dynamic_inserts_new_recipe_into_unfinished_plan(self) -> None:
        """新增菜应参与剩余步骤的全局重排，而不是固定追加到旧计划末尾。"""

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            service = self.make_service(directory)
            start = datetime(2026, 1, 1, 12, 0)
            initial = service.handle_request(
                INSERTION_BASE_REQUEST,
                task_id="insert_recipe",
                now=start,
            )
            dynamic = service.handle_request(
                INSERTION_ADDED_REQUEST,
                task_id="insert_recipe",
                now=start + timedelta(minutes=1),
            )
            added_id = INSERTION_ADDED_REQUEST[0]["id"]
            added_start = min(
                item.start
                for item in dynamic.scheduled_tasks
                if item.task.recipe_id == added_id
            )

            self.assertLess(added_start, initial.metrics["makespanMin"])
            self.assertGreaterEqual(dynamic.metrics["finishSpreadMin"], 45)
            self.assertLessEqual(dynamic.metrics["finishSpreadMin"], 65)
            self.assertLess(dynamic.metrics["runtimeMs"], 3000)

    def test_dynamic_cheesecake_preheat_immediately_precedes_loading(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            service = self.make_service(directory)
            start = datetime(2026, 1, 1, 12, 0)
            service.handle_request(
                INITIAL_REQUEST,
                task_id="preheat_dynamic",
                now=start,
            )
            result = service.handle_request(
                CHEESECAKE_REQUEST,
                task_id="preheat_dynamic",
                now=start + timedelta(minutes=1),
            )
            cheesecake = {
                item.task.step_id: item
                for item in result.scheduled_tasks
                if item.task.recipe_id == CHEESECAKE_REQUEST[0]["id"]
            }
            self.assertEqual(
                cheesecake["s9_preheat"].end,
                cheesecake["s9"].start,
            )
            self.assertLess(result.metrics["runtimeMs"], 3000)

    def test_recommended_four_plus_one_demo_is_fast_and_well_aligned(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = self.make_service(Path(temporary))
            start = datetime(2026, 1, 1, 12, 0)
            static = service.handle_request(
                DEMO_FOUR_RECIPE_REQUEST,
                task_id="recommended_demo",
                now=start,
            )
            dynamic = service.handle_request(
                DEMO_ADDED_REQUEST,
                task_id="recommended_demo",
                now=start + timedelta(minutes=10),
            )
            self.assertEqual(static.mode, "initial")
            self.assertEqual(dynamic.mode, "dynamic")
            self.assertLess(static.metrics["runtimeMs"], 5000)
            self.assertLess(dynamic.metrics["runtimeMs"], 3000)
            self.assertLessEqual(static.metrics["finishSpreadMin"], 8)
            self.assertLessEqual(dynamic.metrics["finishSpreadMin"], 10)
            self.assertLessEqual(static.metrics["maxContinuousHumanMin"], 30)
            self.assertLessEqual(dynamic.metrics["maxContinuousHumanMin"], 30)
            self.assertEqual(dynamic.metrics["rescheduleAtMin"], 10)


if __name__ == "__main__":
    unittest.main()
