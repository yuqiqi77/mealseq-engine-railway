"""排程算法的本地测试，不调用任何外部 API。"""

from __future__ import annotations

import json
import random
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from generate_gantt import build_bars, render_html
from scheduler import (
    ScheduledTask,
    _overlaps,
    _place_tasks,
    build_api_response,
    build_tasks,
    calculate_metrics,
    create_schedule,
    load_device_rules,
    load_requested_recipes,
    prepare_recipes_for_schedule,
    repair_schedule_integrity,
    schedule_from_request,
    validate_schedule,
)
from genetic_solver import Chromosome, _order_crossover
from static_solver_common import assign_resource_instances, build_resource_uses


PROJECT_ROOT = Path(__file__).resolve().parent.parent
RECIPE_DIR = PROJECT_ROOT / "ai" / "parsed_recipes_v5"
DEVICE_PATH = PROJECT_ROOT / "para" / "设备参数清单参考.json"
EXAMPLE_REQUEST = Path(__file__).resolve().parent / "example_request.json"
FIVE_RECIPE_REQUEST = [
    {"id": "58e70ae1a3fd4a750f4b75af", "name": "双椒鳙鱼头"},
    {"id": "58e70ae1a3fd4a750f4b75b0", "name": "韩式泡菜鸦片鱼头"},
    {"id": "58e70b0e9f6429675ec805d8", "name": "清蒸鲈鱼"},
    {"id": "58e70b109f6429675ec805ed", "name": "蒜蓉粉丝蒸扇贝"},
    {"id": "58e70b129f6429675ec80601", "name": "麻辣对虾"},
]
SIX_RECIPE_REQUEST = [
    *FIVE_RECIPE_REQUEST,
    {"id": "61e6c51fec6e1d65587067e1", "name": "低温牛排"},
]


class SchedulerTests(unittest.TestCase):
    @staticmethod
    def one_step_recipe(recipe_id: str, name: str, temperature: int) -> dict:
        return {
            "recipe_id": recipe_id,
            "name": name,
            "ingredients": [],
            "steps": [{
                "step_id": "s1",
                "action": f"将{name}放入烤箱烘烤",
                "duration_min": 11,
                "human_active_min": 1,
                "device": "烤箱",
                "device_active_start_min": 0,
                "device_active_min": 11,
                "mode": "上下火",
                "temperature_settings": [{"zone": "整体", "value_c": temperature}],
                "humidity": None,
                "heat_level_raw": None,
                "heat_level": None,
                "depends_on": [],
            }],
        }

    def test_final_validation_rejects_missing_duplicate_and_overlong_human_work(self) -> None:
        rules = load_device_rules(DEVICE_PATH)
        recipe = self.one_step_recipe("audit", "校验菜", 180)
        task = build_tasks([recipe])[0]
        item = ScheduledTask(task=task, start=0)
        with self.assertRaisesRegex(ValueError, "缺少"):
            validate_schedule([], rules, expected_keys={task.key})
        with self.assertRaisesRegex(ValueError, "重复步骤"):
            validate_schedule([item, item], rules)

        step = recipe["steps"][0]
        step.update({
            "duration_min": 31,
            "human_active_min": 31,
            "device": None,
            "device_active_min": 0,
            "mode": None,
            "temperature_settings": [],
        })
        overlong = build_tasks([recipe])[0]
        with self.assertRaisesRegex(ValueError, "必须拆分菜谱步骤"):
            _place_tasks([overlong], {"audit": 0}, rules, 30, 1)
        with self.assertRaisesRegex(ValueError, "连续人工操作超过"):
            validate_schedule(
                [ScheduledTask(task=overlong, start=0)],
                rules,
                max_continuous_human=30,
            )

    def test_incomplete_static_result_is_repaired_before_output(self) -> None:
        rules = load_device_rules(DEVICE_PATH)
        recipes = [
            self.one_step_recipe("repair-a", "修复菜A", 180),
            self.one_step_recipe("repair-b", "修复菜B", 200),
        ]
        tasks = build_tasks(recipes)
        malformed = [
            ScheduledTask(task=tasks[0], start=0),
            ScheduledTask(task=tasks[0], start=0),
        ]
        repaired = repair_schedule_integrity(malformed, tasks, rules, 30, 1)
        self.assertEqual({item.task.key for item in repaired}, {task.key for task in tasks})
        self.assertEqual(len(repaired), len(tasks))
        validate_schedule(
            repaired,
            rules,
            expected_keys={task.key for task in tasks},
            max_continuous_human=30,
        )

    def test_three_recipe_plan_is_valid_and_nearly_synchronized(self) -> None:
        request_items = json.loads(EXAMPLE_REQUEST.read_text(encoding="utf-8"))
        plan = schedule_from_request(
            request_items,
            RECIPE_DIR,
            DEVICE_PATH,
            plan_start=datetime(2026, 1, 1, 12, 0),
        )

        self.assertEqual(plan.response["overview"]["recipeCount"], 3)
        self.assertEqual(len(plan.response["cookingTimeline"]), 3)
        self.assertEqual(len(plan.response["recipeDetail"]), 3)
        # 这组真实菜谱受最后几段人工互斥限制，当前可行最优差为约6分钟。
        self.assertLessEqual(plan.metrics["finishSpreadMin"], 6)
        self.assertGreater(plan.metrics["compressionRate"], 0)
        types = [item["type"] for item in plan.response["detailTimeline"]]
        self.assertEqual(types, sorted(types))
        device_steps = [
            item for item in plan.response["detailTimeline"]
            if item["type"] in (3, 4, 5)
        ]
        self.assertTrue(device_steps)
        self.assertTrue(all(item.get("recipeNames") for item in device_steps))

    def test_waiting_rows_only_show_released_human_time_without_devices(self) -> None:
        """等待行不是关键词分类，只表示无人工、无机器占用的真实区间。"""

        partial = self.one_step_recipe("partial", "部分人工", 180)
        partial["steps"][0].update({
            "action": "人工处理2分钟后静置8分钟",
            "duration_min": 10,
            "human_active_min": 2,
            "human_intervals": [{"start_min": 0, "duration_min": 2}],
            "device": None,
            "device_active_start_min": 0,
            "device_active_min": 0,
            "mode": None,
            "temperature_settings": [],
        })
        full_human = self.one_step_recipe("full", "全程人工", 180)
        full_human["steps"][0].update({
            "action": "持续人工搅拌",
            "duration_min": 4,
            "human_active_min": 4,
            "human_intervals": [{"start_min": 0, "duration_min": 4}],
            "device": None,
            "device_active_start_min": 0,
            "device_active_min": 0,
            "mode": None,
            "temperature_settings": [],
        })
        machine = self.one_step_recipe("machine", "机器运行", 180)
        machine["steps"][0]["human_intervals"] = [
            {"start_min": 0, "duration_min": 1}
        ]

        tasks = build_tasks([partial, full_human, machine])
        starts = {"partial": 0, "full": 12, "machine": 20}
        schedule = [
            ScheduledTask(task=task, start=starts[task.recipe_id])
            for task in tasks
        ]
        _, bars, _ = build_bars(
            SimpleNamespace(scheduled_tasks=schedule),
            load_device_rules(DEVICE_PATH),
        )
        waits = [bar for bar in bars if bar["title"] == "等待（无人操作）"]

        self.assertEqual(len(waits), 1)
        self.assertEqual(waits[0]["recipe"], "部分人工")
        self.assertEqual((waits[0]["start"], waits[0]["end"]), (2, 10))
        self.assertTrue(waits[0]["waiting"])
        html = render_html(
            [waits[0]["row"]],
            waits,
            {"部分人工": "#2563eb"},
            {"makespanMin": 10, "finishSpreadMin": 0},
            [],
        )
        self.assertIn('stroke-dasharray="6 4"', html)

    def test_generic_preparation_is_a_real_dependency_not_a_final_step(self) -> None:
        """“准备食材”必须约束后续根步骤，不能被右移到整道菜末尾。"""

        recipe = load_requested_recipes(
            [{"id": "657951cac458a177d84387a1", "name": "五味乌鸡煲"}],
            RECIPE_DIR,
        )[0]
        by_id = {step["step_id"]: step for step in recipe["steps"]}

        self.assertIn("s1", by_id["s2"]["depends_on"])
        self.assertIn("s1", by_id["s3"]["depends_on"])

    def test_smoke_hood_stays_on_one_dashed_background_lane(self) -> None:
        """烟机允许并行重叠，但统一显示在一条虚线后台资源泳道。"""

        recipes = []
        for recipe_id, name in (("smoke-a", "烟机菜甲"), ("smoke-b", "烟机菜乙")):
            recipe = self.one_step_recipe(recipe_id, name, 180)
            recipe["steps"][0].update({
                "action": "开启烟机排烟",
                "duration_min": 10,
                "human_active_min": 1,
                "human_intervals": [{"start_min": 0, "duration_min": 1}],
                "device": "烟机",
                "device_active_start_min": 0,
                "device_active_min": 10,
                "mode": "低档",
                "temperature_settings": [],
            })
            recipes.append(recipe)
        schedule = [
            ScheduledTask(task=task, start=0)
            for task in build_tasks(recipes)
        ]
        rows, bars, colors = build_bars(
            SimpleNamespace(scheduled_tasks=schedule),
            load_device_rules(DEVICE_PATH),
        )
        smoke_bars = [bar for bar in bars if bar["title"] == "烟机"]

        self.assertEqual([bar["row"] for bar in smoke_bars], ["烟机", "烟机"])
        self.assertTrue(all(bar["background"] for bar in smoke_bars))
        html = render_html(
            rows,
            bars,
            colors,
            {"makespanMin": 10, "finishSpreadMin": 0},
            [],
        )
        self.assertIn('stroke-dasharray="6 4"', html)
        self.assertIn('style="min-width:', html)

    def test_cp_sat_and_genetic_keep_the_same_api_shape(self) -> None:
        """更换内部算法不能改变比赛接口的五个顶层字段。"""

        request_items = json.loads(EXAMPLE_REQUEST.read_text(encoding="utf-8"))[:2]
        expected_keys = {
            "overview",
            "cookingTimeline",
            "ingredientsSummary",
            "detailTimeline",
            "recipeDetail",
        }
        for solver_name in ("cp_sat", "genetic"):
            with self.subTest(solver=solver_name):
                plan = schedule_from_request(
                    request_items,
                    RECIPE_DIR,
                    DEVICE_PATH,
                    plan_start=datetime(2026, 1, 1, 12, 0),
                    solver=solver_name,
                )
                self.assertEqual(set(plan.response), expected_keys)
                self.assertEqual(plan.response["overview"]["recipeCount"], 2)
                self.assertLessEqual(plan.metrics["finishSpreadMin"], 5)

    def test_recipe_detail_follows_actual_start_order(self) -> None:
        """独立步骤换序后，recipeDetail 必须展示真实执行顺序。"""

        recipe = {
            "recipe_id": "actual-order",
            "name": "实际顺序测试菜",
            "ingredients": [],
            "steps": [],
        }
        for step_id, action in (("s1", "处理食材甲"), ("s2", "处理食材乙")):
            recipe["steps"].append({
                "step_id": step_id,
                "action": action,
                "duration_min": 1,
                "human_active_min": 1,
                "human_intervals": [{"start_min": 0, "duration_min": 1}],
                "device": None,
                "device_active_start_min": 0,
                "device_active_min": 0,
                "mode": None,
                "temperature_settings": [],
                "humidity": None,
                "heat_level_raw": None,
                "heat_level": None,
                "depends_on": [],
            })
        tasks = {task.step_id: task for task in build_tasks([recipe])}
        schedule = [
            ScheduledTask(tasks["s1"], start=5),
            ScheduledTask(tasks["s2"], start=0),
        ]

        response = build_api_response(
            [recipe], schedule, {"actual-order": 6}, datetime(2026, 1, 1, 12, 0)
        )
        descriptions = [
            item["describe"] for item in response["recipeDetail"][0]["cookingSteps"]
        ]
        self.assertEqual(descriptions, ["处理食材乙", "处理食材甲"])

    def test_every_recipe_is_individually_schedulable(self) -> None:
        """完整菜谱库中的每一道菜都必须能生成合法单菜排程。"""

        paths = sorted(RECIPE_DIR.glob("*.json"))
        paths = [path for path in paths if path.name != "failed_recipes.json"]
        self.assertEqual(len(paths), 100)
        for path in paths:
            recipe = json.loads(path.read_text(encoding="utf-8"))
            request_items = [{"id": str(recipe["recipe_id"]), "name": recipe["name"]}]
            with self.subTest(recipe=recipe["name"]):
                plan = schedule_from_request(
                    request_items,
                    RECIPE_DIR,
                    DEVICE_PATH,
                    plan_start=datetime(2026, 1, 1, 12, 0),
                    solver="heuristic",
                )
                self.assertEqual(plan.response["overview"]["recipeCount"], 1)

    def test_real_three_to_five_recipe_combinations_are_legal(self) -> None:
        """真实菜谱的3、4、5菜组合必须满足依赖、人工和设备硬约束。"""

        rules = load_device_rules(DEVICE_PATH)
        for count in (3, 4, 5):
            with self.subTest(recipe_count=count):
                plan = schedule_from_request(
                    FIVE_RECIPE_REQUEST[:count],
                    RECIPE_DIR,
                    DEVICE_PATH,
                    plan_start=datetime(2026, 1, 1, 12, 0),
                    solver="heuristic",
                )
                validate_schedule(plan.scheduled_tasks, rules)
                self.assertEqual(plan.response["overview"]["recipeCount"], count)
                self.assertLessEqual(plan.metrics["maxContinuousHumanMin"], 30)

    def test_both_optimizers_return_five_recipes_within_time_budget(self) -> None:
        """两个静态求解器都要在5秒预算内返回合法的五菜方案。"""

        rules = load_device_rules(DEVICE_PATH)
        for solver_name in ("cp_sat", "genetic"):
            with self.subTest(solver=solver_name):
                started = time.perf_counter()
                plan = schedule_from_request(
                    FIVE_RECIPE_REQUEST,
                    RECIPE_DIR,
                    DEVICE_PATH,
                    plan_start=datetime(2026, 1, 1, 12, 0),
                    solver=solver_name,
                )
                elapsed = time.perf_counter() - started
                validate_schedule(plan.scheduled_tasks, rules)
                self.assertLessEqual(elapsed, 5.0)
                self.assertLessEqual(plan.metrics["maxContinuousHumanMin"], 30)

    def test_static_schedule_has_no_five_recipe_upper_limit(self) -> None:
        """六道及更多菜也能进入静态排程，接口不设置五道上限。"""

        plan = schedule_from_request(
            SIX_RECIPE_REQUEST,
            RECIPE_DIR,
            DEVICE_PATH,
            plan_start=datetime(2026, 1, 1, 12, 0),
            solver="heuristic",
        )
        validate_schedule(plan.scheduled_tasks, load_device_rules(DEVICE_PATH))
        self.assertEqual(plan.response["overview"]["recipeCount"], 6)

    def test_three_to_five_compatible_recipes_finish_within_five_minutes(self) -> None:
        """资源允许时，3～5道菜应通过优化实现5分钟内同出锅。"""

        rules = load_device_rules(DEVICE_PATH)
        recipes = [
            self.one_step_recipe(str(index), f"同步菜{index}", 180)
            for index in range(1, 6)
        ]
        # 第五道菜只等待人工收尾，不占烤箱，避免人为制造超过设备容量的无解目标。
        fifth = recipes[4]["steps"][0]
        fifth.update({
            "action": "准备同步菜5并等待装盘",
            "device": None,
            "device_active_min": 0,
            "mode": None,
            "temperature_settings": [],
        })

        for solver_name in ("cp_sat", "genetic"):
            for count in (3, 4, 5):
                with self.subTest(solver=solver_name, recipe_count=count):
                    schedule, standalone = create_schedule(
                        recipes[:count], rules, solver=solver_name
                    )
                    validate_schedule(schedule, rules)
                    metrics = calculate_metrics(
                        recipes[:count], schedule, standalone, rules, human_break=1.0
                    )
                    self.assertLessEqual(metrics["finishSpreadMin"], 5)

    def test_genetic_algorithm_uses_one_layer_operation_encoding(self) -> None:
        """染色体只能包含工序顺序，交叉后步骤不能丢失或重复。"""

        self.assertEqual(
            set(Chromosome.__dataclass_fields__),
            {"operation_order"},
        )
        first = Chromosome(["a:s1", "a:s2", "b:s1", "b:s2"])
        second = Chromosome(["b:s2", "b:s1", "a:s2", "a:s1"])
        child = _order_crossover(first, second, random.Random(42))
        self.assertEqual(len(child.operation_order), 4)
        self.assertEqual(set(child.operation_order), set(first.operation_order))

    def test_twenty_four_hour_freezing_is_kept_as_advance_preparation(self) -> None:
        request_items = [{
            "id": "5c8646fe98d5bc7e184a90bc",
            "name": "鸡仔饼",
        }]
        plan = schedule_from_request(request_items, RECIPE_DIR, DEVICE_PATH)
        self.assertLess(plan.metrics["makespanMin"], 1440)
        first_step = next(
            item for item in plan.scheduled_tasks if item.task.step_id == "s1"
        )
        self.assertEqual(first_step.task.duration, 0)
        # 原步骤由 7 分钟人工处理和 1440 分钟冷冻组成，前置化后仍须
        # 完整保留 1447 分钟原始时长，不能只留下设备等待时长。
        self.assertEqual(
            first_step.task.original["advance_preparation"]["duration_min"],
            1447,
        )
        self.assertTrue(any(
            step["describe"].startswith("需提前完成：")
            for step in plan.response["recipeDetail"][0]["cookingSteps"]
        ))

    def test_only_pre_cooking_waits_become_advance_preparation(self) -> None:
        recipe = {
            "recipe_id": "prep-prefix",
            "name": "前置准备测试",
            "steps": [
                {"step_id": "s1", "action": "清洗并切好食材", "device": None,
                 "duration_min": 5, "device_active_min": 0},
                {"step_id": "s2", "action": "加入调料腌制30分钟", "device": None,
                 "duration_min": 30, "device_active_min": 0},
                {"step_id": "s3", "action": "清水浸泡120分钟", "device": None,
                 "duration_min": 120, "device_active_min": 0},
                {"step_id": "s4", "action": "放入锅中煎熟", "device": "灶具",
                 "duration_min": 8, "device_active_min": 8},
                {"step_id": "s5", "action": "煮好后浸泡120分钟", "device": None,
                 "duration_min": 120, "device_active_min": 0},
            ],
        }
        prepared = prepare_recipes_for_schedule([recipe])[0]["steps"]
        self.assertNotIn("advance_preparation", prepared[0])
        self.assertEqual(prepared[1]["duration_min"], 30)
        self.assertNotIn("advance_preparation", prepared[1])
        self.assertEqual(prepared[2]["duration_min"], 0)
        self.assertIn("advance_preparation", prepared[2])
        self.assertNotIn("advance_preparation", prepared[4])

    def test_enjoyment_sentence_does_not_delay_completion(self) -> None:
        request_items = [{
            "id": "6510fb3545256c3ad7c25c9c",
            "name": "苹果芒果派",
        }]
        recipes = load_requested_recipes(request_items, RECIPE_DIR)
        tasks = build_tasks(recipes)
        enjoy = next(task for task in tasks if task.step_id == "s14")
        self.assertEqual(enjoy.duration, 0)
        self.assertEqual(enjoy.human_duration, 0)

    def test_outcome_sentence_does_not_fake_egg_custard_finish(self) -> None:
        request_items = [{
            "id": "6492a7c6933a4b7277dee237",
            "name": "Q弹果冻鸡蛋羹",
        }]
        recipes = load_requested_recipes(request_items, RECIPE_DIR)
        outcome = next(
            task for task in build_tasks(recipes) if task.step_id == "s7"
        )
        self.assertEqual(outcome.duration, 0)
        self.assertEqual(outcome.human_duration, 0)
        plan = schedule_from_request(
            request_items, RECIPE_DIR, DEVICE_PATH, solver="heuristic"
        )
        cooked_end = next(
            item.end for item in plan.scheduled_tasks if item.task.step_id == "s6"
        )
        self.assertEqual(plan.metrics["makespanMin"], cooked_end)

    def test_other_resource_free_recipe_endings_are_not_fake_work(self) -> None:
        endings = (
            ("5c860d6298d5bc7e184a907c", "s8"),
            ("64706f256f734220a4e24b47", "s6"),
            ("64706f406f734220a4e24b57", "s10"),
            ("657951b7c458a177d84386bc", "s10"),
        )
        for recipe_id, step_id in endings:
            with self.subTest(recipe_id=recipe_id):
                recipe = json.loads(
                    (RECIPE_DIR / f"{recipe_id}.json").read_text(encoding="utf-8")
                )
                task = next(
                    item for item in build_tasks([recipe]) if item.step_id == step_id
                )
                self.assertEqual(task.duration, 0)

    def test_incompatible_final_steam_modes_are_aligned_as_far_as_possible(self) -> None:
        request_items = [
            {"id": "66715324bfbee338853895c7", "name": "轻松一锅蒸"},
            {"id": "5cc7e0404a21a4301960aef1", "name": "糯米烧麦"},
            {"id": "5a38d6b3f254230b76e2b50f", "name": "蒜香烤茄子"},
            {"id": "5f73fee8a6daaa3b53ba043c", "name": "美式薯条"},
        ]
        plan = schedule_from_request(request_items, RECIPE_DIR, DEVICE_PATH)
        validate_schedule(plan.scheduled_tasks, load_device_rules(DEVICE_PATH))
        # 两段不同模式的终末蒸制不能重叠，20分钟是此组合的资源下界。
        self.assertGreaterEqual(plan.metrics["finishSpreadMin"], 20)
        self.assertLessEqual(plan.metrics["finishSpreadMin"], 25)

    def test_recipe_name_must_match_database(self) -> None:
        request_items = [{
            "id": "5d54bae2a9114174727c8b20",
            "name": "错误名称",
        }]
        with self.assertRaisesRegex(ValueError, "菜谱名称不匹配"):
            load_requested_recipes(request_items, RECIPE_DIR)

    def test_oven_parallelism_requires_same_mode_and_temperature(self) -> None:
        rules = load_device_rules(DEVICE_PATH)
        same_tasks = build_tasks([
            self.one_step_recipe("a", "菜A", 180),
            self.one_step_recipe("b", "菜B", 180),
        ])
        same_schedule = _place_tasks(
            same_tasks, {"a": 0, "b": 0}, rules, 30, 1
        )
        self.assertTrue(_overlaps(
            same_schedule[0].device_interval,
            same_schedule[1].device_interval,
        ))

        different_tasks = build_tasks([
            self.one_step_recipe("a", "菜A", 180),
            self.one_step_recipe("b", "菜B", 200),
        ])
        different_schedule = _place_tasks(
            different_tasks, {"a": 0, "b": 0}, rules, 30, 1
        )
        self.assertFalse(_overlaps(
            different_schedule[0].device_interval,
            different_schedule[1].device_interval,
        ))

    def test_device_catalog_has_concrete_instances_and_background_resources(self) -> None:
        rules = load_device_rules(DEVICE_PATH)
        self.assertEqual(rules["灶具"].instances, ("左灶眼", "右灶眼"))
        self.assertEqual(len(rules["蒸箱"].instances), 4)
        self.assertEqual(len(rules["烤箱"].instances), 4)
        self.assertTrue(rules["冰箱"].background)
        self.assertTrue(rules["烟机"].background)
        self.assertEqual(rules["洗碗机"].capacity, 1)
        self.assertEqual(rules["净咖一体机"].capacity, 1)

    def test_explicit_oven_layer_cannot_be_used_by_two_overlapping_tasks(self) -> None:
        rules = load_device_rules(DEVICE_PATH)
        first = self.one_step_recipe("a", "菜A", 180)
        second = self.one_step_recipe("b", "菜B", 180)
        first["steps"][0]["action"] += "，放入第3层"
        second["steps"][0]["action"] += "，放入第3层"
        tasks = build_tasks([first, second])
        schedule = _place_tasks(tasks, {"a": 0, "b": 0}, rules, 30, 1)
        self.assertFalse(_overlaps(
            schedule[0].device_interval,
            schedule[1].device_interval,
        ))

    def test_unspecified_oven_steps_are_assigned_to_different_free_layers(self) -> None:
        rules = load_device_rules(DEVICE_PATH)
        tasks = build_tasks([
            self.one_step_recipe("a", "菜A", 180),
            self.one_step_recipe("b", "菜B", 180),
        ])
        schedule = _place_tasks(tasks, {"a": 0, "b": 0}, rules, 30, 1)
        assigned = assign_resource_instances(schedule, rules)
        self.assertTrue(_overlaps(assigned[0].device_interval, assigned[1].device_interval))
        self.assertNotEqual(assigned[0].resource_instances, assigned[1].resource_instances)

    def test_fridge_is_tracked_but_does_not_limit_parallel_recipes(self) -> None:
        rules = load_device_rules(DEVICE_PATH)
        recipes = []
        for recipe_id, name in (("a", "菜A"), ("b", "菜B")):
            recipe = self.one_step_recipe(recipe_id, name, 180)
            step = recipe["steps"][0]
            step.update({
                "action": f"将{name}放入冰箱冷藏",
                "device": "冰箱",
                "mode": "冷藏",
                "temperature_settings": [{"zone": "冷藏室", "value_c": 4}],
                "duration_min": 60,
                "device_active_min": 60,
            })
            recipes.append(recipe)
        schedule = _place_tasks(build_tasks(recipes), {"a": 0, "b": 0}, rules, 30, 1)
        assigned = assign_resource_instances(schedule, rules)
        self.assertTrue(_overlaps(assigned[0].device_interval, assigned[1].device_interval))
        self.assertEqual(assigned[0].resource_instances, ("冰箱冷藏区",))
        self.assertEqual(assigned[1].resource_instances, ("冰箱冷藏区",))

    def test_one_step_can_track_primary_oven_and_additional_fridge(self) -> None:
        recipe = self.one_step_recipe("multi", "多设备菜", 180)
        step = recipe["steps"][0]
        step.update({
            "action": "从烤箱取出，放凉后放入冰箱冷藏",
            "duration_min": 131,
            "device_active_min": 1,
            "additional_device_usages": [{
                "device": "冰箱",
                "mode": "冷藏",
                "temperature_settings": [{"zone": "冷藏室", "value_c": 4}],
                "start_min": 11,
                "duration_min": 120,
                "action": "放入冰箱冷藏",
            }],
        })
        tasks = build_tasks([recipe])
        rules = load_device_rules(DEVICE_PATH)
        schedule = _place_tasks(tasks, {"multi": 0}, rules, 30, 1)
        uses = build_resource_uses(schedule)
        self.assertEqual({use.device for use in uses}, {"烤箱", "冰箱"})
        fridge = next(use for use in uses if use.device == "冰箱")
        self.assertEqual((fridge.start, fridge.end), (11, 131))

    def test_implicit_preheated_oven_gets_real_preheat_task(self) -> None:
        recipe = self.one_step_recipe("preheat", "测试蛋糕", 180)
        recipe["steps"][0].update({
            "action": "将测试蛋糕放入预热好的烤箱烘烤",
            "source_text": "将测试蛋糕放入预热好的烤箱中。",
        })

        tasks = build_tasks([recipe])
        self.assertEqual(len(tasks), 2)
        preheat, cooking = tasks
        self.assertTrue(preheat.original["virtual_preheat"])
        self.assertEqual(preheat.duration, 10)
        self.assertEqual(preheat.device_duration, 10)
        self.assertEqual(preheat.temperatures, (("整体", 180.0),))
        self.assertIn(preheat.key, cooking.dependencies)

        rules = load_device_rules(DEVICE_PATH)
        schedule = _place_tasks(tasks, {"preheat": 0}, rules, 30, 1)
        by_key = {item.task.key: item for item in schedule}
        self.assertGreaterEqual(by_key[cooking.key].start, by_key[preheat.key].end)

        final_schedule, _ = create_schedule([recipe], rules)
        final_by_key = {item.task.key: item for item in final_schedule}
        self.assertEqual(
            final_by_key[preheat.key].end,
            final_by_key[cooking.key].start,
        )

        assigned = assign_resource_instances(schedule, rules)
        assigned_by_key = {item.task.key: item for item in assigned}
        self.assertEqual(
            len(assigned_by_key[preheat.key].resource_instances),
            len(rules["烤箱"].instances),
        )
        self.assertEqual(len(assigned_by_key[cooking.key].resource_instances), 1)

    def test_real_cheesecake_preheat_immediately_precedes_oven_loading(self) -> None:
        request_items = [{
            "id": "5d565e87a9114174727c8bad",
            "name": "半熟芝士蛋糕",
        }]
        plan = schedule_from_request(request_items, RECIPE_DIR, DEVICE_PATH)
        by_step = {item.task.step_id: item for item in plan.scheduled_tasks}
        self.assertEqual(by_step["s9_preheat"].end, by_step["s9"].start)

    def test_separated_human_intervals_release_person_during_wait(self) -> None:
        waiting = self.one_step_recipe("waiting", "等待菜", 180)
        waiting_step = waiting["steps"][0]
        waiting_step.update({
            "action": "启动设备，等待完成后取出",
            "device": None,
            "device_active_start_min": 0,
            "device_active_min": 0,
            "mode": None,
            "temperature_settings": [],
            "duration_min": 11,
            "human_active_min": 2,
            "human_intervals": [
                {"start_min": 0, "duration_min": 1},
                {"start_min": 10, "duration_min": 1},
            ],
        })
        manual = self.one_step_recipe("manual", "人工菜", 180)
        manual_step = manual["steps"][0]
        manual_step.update({
            "action": "人工处理食材",
            "device": None,
            "device_active_start_min": 0,
            "device_active_min": 0,
            "mode": None,
            "temperature_settings": [],
            "duration_min": 3,
            "human_active_min": 3,
            "human_intervals": [
                {"start_min": 0, "duration_min": 3},
            ],
        })
        rules = load_device_rules(DEVICE_PATH)
        tasks = build_tasks([waiting, manual])
        schedule = _place_tasks(tasks, {"waiting": 0, "manual": 0}, rules, 30, 1)
        by_recipe = {item.task.recipe_id: item for item in schedule}
        self.assertEqual(by_recipe["waiting"].human_intervals, ((0, 1), (10, 11)))
        self.assertEqual(by_recipe["manual"].start, 1)

    def test_preheat_is_exposed_with_real_interval_and_parameters(self) -> None:
        recipe = self.one_step_recipe("preheat", "测试蛋糕", 180)
        recipe["steps"][0].update({
            "action": "将测试蛋糕放入预热好的烤箱烘烤",
            "source_text": "将测试蛋糕放入预热好的烤箱中。",
        })
        rules = load_device_rules(DEVICE_PATH)
        tasks = build_tasks([recipe])
        schedule = _place_tasks(tasks, {"preheat": 0}, rules, 30, 1)
        from scheduler import build_api_response

        response = build_api_response(
            [recipe], schedule, {"preheat": 21}, datetime(2026, 1, 1, 12, 0)
        )
        preheat_detail = next(
            item for item in response["detailTimeline"] if item["title"] == "预热"
        )
        start, end = (float(value) for value in preheat_detail["timeInterval"].split("-"))
        self.assertEqual(end - start, 10)
        self.assertEqual(preheat_detail["parameters"][0]["temperature"], "整体180℃")
        self.assertEqual(preheat_detail["parameters"][0]["time"], 10)


if __name__ == "__main__":
    unittest.main()
