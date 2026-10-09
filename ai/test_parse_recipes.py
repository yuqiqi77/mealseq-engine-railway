"""Local checks for the offline recipe record format; no API calls."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from parse_recipes import (
    DEFAULT_DEVICES,
    call_deepseek_chunked,
    _repair_deterministic_result,
    _write_json_atomic,
    read_devices,
    validate_and_wrap,
)


class RecipeValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.devices = read_devices(DEFAULT_DEVICES)

    def setUp(self) -> None:
        self.recipe = {
            "菜谱id": "test-1",
            "名称": "测试鸡翅",
            "食材清单": "主料：鸡翅3个",
        }
        self.source_steps = [{"no": 1, "text": "将鸡翅放入烤箱，选择上下火180℃烤15分钟。"}]
        self.result = {
            "ingredients": [{
                "name": "鸡翅", "amount_text": "3个", "quantity": 3, "unit": "个",
                "role": "主料", "category": "荤菜", "source_text": "鸡翅3个",
            }],
            "steps": [{
                "step_id": "s1", "source_step_no": 1,
                "action": "将鸡翅放入烤箱，选择上下火180℃烤15分钟",
                "device_raw": "烤箱", "device": "烤箱", "device_source": "explicit",
                "mode_raw": "上下火", "mode": "上下火", "mode_source": "explicit",
                "temperature_c": 180, "temperature_source": "explicit",
                "temperature_settings": [
                    {"zone": "整体", "value_c": 180, "source": "explicit"},
                ],
                "heat_level_raw": None, "heat_level": None, "humidity": None,
                "duration_min": 16, "duration_source": "estimated",
                "human_active_min": 1, "human_active_source": "estimated",
                "device_active_start_min": 0, "device_active_min": 16,
                "device_active_source": "estimated",
                "depends_on": [], "requires_human": True, "can_run_unattended": True,
            }],
        }

    def validate(self, result: dict, source_steps: list[dict] | None = None) -> dict:
        return validate_and_wrap(
            self.recipe,
            source_steps or self.source_steps,
            result,
            self.devices,
        )

    def test_oven_step_keeps_original_count_and_releases_human(self) -> None:
        output = self.validate(copy.deepcopy(self.result))
        self.assertEqual(output["schema_version"], 5)
        self.assertEqual(len(output["steps"]), len(self.source_steps))
        self.assertEqual(output["steps"][0]["human_active_min"], 1)
        self.assertEqual(output["steps"][0]["source_text"], self.source_steps[0]["text"])

    def test_split_step_is_rejected(self) -> None:
        result = copy.deepcopy(self.result)
        result["steps"].append(copy.deepcopy(result["steps"][0]))
        with self.assertRaisesRegex(ValueError, "解析步骤数"):
            self.validate(result)

    def test_device_limits_and_human_release_are_enforced(self) -> None:
        invalid_updates = (
            {"mode": "常规烘焙"},
            {"temperature_c": 250,
             "temperature_settings": [{"zone": "整体", "value_c": 250, "source": "explicit"}]},
            {"duration_min": 302, "device_active_min": 301},
        )
        for updates in invalid_updates:
            with self.subTest(updates=updates):
                result = copy.deepcopy(self.result)
                result["steps"][0].update(updates)
                with self.assertRaises(ValueError):
                    self.validate(result)

    def test_human_flags_are_derived_from_durations(self) -> None:
        result = copy.deepcopy(self.result)
        result["steps"][0]["requires_human"] = False
        result["steps"][0]["can_run_unattended"] = False
        step = self.validate(result)["steps"][0]
        self.assertTrue(step["requires_human"])
        self.assertTrue(step["can_run_unattended"])

    def test_omitted_derived_fields_are_filled_locally(self) -> None:
        result = copy.deepcopy(self.result)
        step = result["steps"][0]
        step["human_intervals"] = [
            {"start_min": 0, "duration_min": 1, "source": "estimated"},
        ]
        for key in (
            "temperature_c", "temperature_source", "human_active_min",
            "human_active_source", "device_active_source", "requires_human",
            "can_run_unattended",
        ):
            step.pop(key, None)
        checked = self.validate(result)["steps"][0]
        self.assertEqual(checked["temperature_c"], 180)
        self.assertEqual(checked["human_active_min"], 1)
        self.assertEqual(checked["device_active_source"], "estimated")
        self.assertTrue(checked["requires_human"])
        self.assertTrue(checked["can_run_unattended"])

    def test_deterministic_repairs_avoid_model_retry(self) -> None:
        result = copy.deepcopy(self.result)
        step = result["steps"][0]
        step.update({
            "action": "用锅烧热食用油并浇在鸡翅上",
            "device_raw": "锅", "device": "灶具", "device_source": "normalized",
            "mode_raw": None, "mode": None, "mode_source": "unknown",
            "temperature_c": None, "temperature_source": "unknown",
            "temperature_settings": [], "heat_level_raw": None, "heat_level": 5,
            "duration_min": 3, "human_active_min": 3,
            "device_active_start_min": 0, "device_active_min": 1,
        })
        repairs = _repair_deterministic_result(
            result,
            [{"no": 1, "text": "用锅烧热食用油并浇在鸡翅上。"}],
        )
        self.assertTrue(repairs)
        self.assertEqual(step["device_active_min"], 3)

    def test_generic_preparation_becomes_ancestor_of_all_root_branches(self) -> None:
        result = copy.deepcopy(self.result)
        preparation = result["steps"][0]
        preparation.update({
            "action": "准备所有食材",
            "device_raw": None,
            "device": None,
            "device_source": "unknown",
            "mode_raw": None,
            "mode": None,
            "mode_source": "unknown",
            "temperature_c": None,
            "temperature_source": "unknown",
            "temperature_settings": [],
            "heat_level_raw": None,
            "heat_level": None,
            "humidity": None,
            "duration_min": 1,
            "human_active_min": 1,
            "device_active_start_min": 0,
            "device_active_min": 0,
            "device_active_source": "not_applicable",
        })
        second = copy.deepcopy(preparation)
        second.update({
            "step_id": "s2",
            "source_step_no": 2,
            "action": "清洗鸡翅",
            "depends_on": [],
        })
        third = copy.deepcopy(preparation)
        third.update({
            "step_id": "s3",
            "source_step_no": 3,
            "action": "切配蔬菜",
            "depends_on": [],
        })
        result["steps"] = [preparation, second, third]
        source_steps = [
            {"no": 1, "text": "准备所有食材"},
            {"no": 2, "text": "清洗鸡翅"},
            {"no": 3, "text": "切配蔬菜"},
        ]
        repairs = _repair_deterministic_result(result, source_steps)
        self.assertTrue(any("通用准备步骤" in item for item in repairs))
        self.assertEqual(second["depends_on"], ["s1"])
        self.assertEqual(third["depends_on"], ["s1"])
        checked = self.validate(result, source_steps)
        self.assertEqual(checked["steps"][1]["depends_on"], ["s1"])
        self.assertEqual(checked["steps"][2]["depends_on"], ["s1"])

    def test_long_soaking_releases_human_without_splitting_source_step(self) -> None:
        """切配、泡发、收尾仍是一条步骤，但等待期间必须释放人工。"""

        result = copy.deepcopy(self.result)
        step = result["steps"][0]
        step.update({
            "action": "将腐竹切段，用开水泡发2小时，挤干水分后摆盘",
            "device_raw": None, "device": None, "device_source": "unknown",
            "mode_raw": None, "mode": None, "mode_source": "unknown",
            "temperature_c": None, "temperature_source": "unknown",
            "temperature_settings": [], "heat_level_raw": None,
            "heat_level": None, "humidity": None,
            "duration_min": 124, "human_active_min": 124,
            "device_active_start_min": 0, "device_active_min": 0,
            "device_active_source": "not_applicable",
            "human_intervals": [{
                "start_min": 0, "duration_min": 124, "source": "estimated",
            }],
        })
        source_steps = [{
            "no": 1,
            "text": "将腐竹切成段，用开水泡发2小时，挤干水分后整齐摆盘。",
        }]

        with self.assertRaisesRegex(ValueError, "长时间等待"):
            self.validate(copy.deepcopy(result), source_steps)

        repairs = _repair_deterministic_result(result, source_steps)
        checked = self.validate(result, source_steps)["steps"][0]

        self.assertTrue(any("长等待" in item for item in repairs))
        self.assertEqual(len(checked["human_intervals"]), 2)
        self.assertLess(checked["human_active_min"], 30)
        self.assertTrue(checked["can_run_unattended"])

    def test_hot_water_temperature_without_device_is_preserved(self) -> None:
        result = copy.deepcopy(self.result)
        step = result["steps"][0]
        step.update({
            "action": "用90℃左右的热水烫鸡翅15秒后洗净",
            "device_raw": None, "device": None, "device_source": "unknown",
            "mode_raw": "热水处理", "mode": "热水处理", "mode_source": "estimated",
            "temperature_c": 90, "temperature_source": "explicit",
            "temperature_settings": [
                {"zone": "整体", "value_c": 90, "source": "explicit"},
            ],
            "heat_level_raw": None, "heat_level": None, "humidity": None,
            "duration_min": 2, "human_active_min": 2,
            "device_active_start_min": 0, "device_active_min": 0,
            "device_active_source": "not_applicable", "depends_on": [],
        })
        source_steps = [{"no": 1, "text": "用90℃左右的热水烫鸡翅15秒后洗净。"}]

        repairs = _repair_deterministic_result(result, source_steps)
        checked = self.validate(result, source_steps)["steps"][0]

        self.assertTrue(any("处理用水" in item for item in repairs))
        self.assertIsNone(checked["device"])
        self.assertIsNone(checked["mode"])
        self.assertEqual(checked["temperature_c"], 90)
        self.assertEqual(checked["temperature_settings"][0]["zone"], "处理用水")

    def test_cross_batch_material_dependency_is_repaired_locally(self) -> None:
        result = copy.deepcopy(self.result)
        first = copy.deepcopy(result["steps"][0])
        first.update({
            "action": "将鸡翅洗净", "device_raw": None, "device": None,
            "device_source": "unknown", "mode_raw": None, "mode": None,
            "mode_source": "unknown", "temperature_c": None,
            "temperature_source": "unknown", "temperature_settings": [],
            "duration_min": 2, "human_active_min": 2,
            "device_active_start_min": 0, "device_active_min": 0,
            "device_active_source": "not_applicable", "depends_on": [],
        })
        second = copy.deepcopy(first)
        second.update({
            "step_id": "s2", "source_step_no": 2,
            "action": "继续将鸡翅切块", "depends_on": [],
        })
        result["steps"] = [first, second]
        source_steps = [
            {"no": 1, "text": "将鸡翅洗净。"},
            {"no": 2, "text": "继续将鸡翅切块。"},
        ]

        repairs = _repair_deterministic_result(result, source_steps)
        checked = self.validate(result, source_steps)

        self.assertTrue(any("前置依赖s1" in item for item in repairs))
        self.assertEqual(checked["steps"][1]["depends_on"], ["s1"])

    def test_split_ingredient_branches_do_not_depend_on_each_other(self) -> None:
        result = copy.deepcopy(self.result)
        self.recipe["食材清单"] = "主料：鳗鱼1条"
        result["ingredients"] = [{
            "name": "鳗鱼", "amount_text": "1条", "quantity": 1, "unit": "条",
            "role": "主料", "category": "荤菜", "source_text": "鳗鱼1条",
        }]
        template = copy.deepcopy(result["steps"][0])
        template.update({
            "device_raw": None, "device": None, "device_source": "unknown",
            "mode_raw": None, "mode": None, "mode_source": "unknown",
            "temperature_c": None, "temperature_source": "unknown",
            "temperature_settings": [], "heat_level_raw": None, "heat_level": None,
            "duration_min": 2, "human_active_min": 2,
            "device_active_start_min": 0, "device_active_min": 0,
            "device_active_source": "not_applicable",
        })
        actions_and_dependencies = [
            ("将鳗鱼洗净", []),
            ("将鳗鱼分成两份", ["s1"]),
            ("把1份鳗鱼放入蒸盘", ["s2"]),
            ("将另1份鳗鱼放入照烧汁", ["s2"]),
        ]
        result["steps"] = []
        source_steps = []
        for index, (action, dependencies) in enumerate(actions_and_dependencies, start=1):
            step = copy.deepcopy(template)
            step.update({
                "step_id": f"s{index}", "source_step_no": index,
                "action": action, "depends_on": dependencies,
            })
            result["steps"].append(step)
            source_steps.append({"no": index, "text": action + "。"})

        repairs = _repair_deterministic_result(result, source_steps)
        checked = self.validate(result, source_steps)

        self.assertFalse(any("s4 补充同一食材" in item for item in repairs))
        self.assertEqual(checked["steps"][3]["depends_on"], ["s2"])

    def test_stove_is_released_while_waiting_for_cooling(self) -> None:
        result = copy.deepcopy(self.result)
        step = result["steps"][0]
        step.update({
            "action": "关火待凉后加入面粉和蛋黄搅拌均匀",
            "device_raw": "锅", "device": "灶具", "device_source": "normalized",
            "mode_raw": None, "mode": None, "mode_source": "unknown",
            "temperature_c": None, "temperature_source": "unknown",
            "temperature_settings": [], "heat_level_raw": None, "heat_level": 1,
            "duration_min": 20, "human_active_min": 2,
            "device_active_start_min": 0, "device_active_min": 20,
            "device_active_source": "estimated", "depends_on": [],
        })
        source_steps = [{"no": 1, "text": "关火待凉后加入面粉和蛋黄搅拌均匀。"}]

        repairs = _repair_deterministic_result(result, source_steps)
        checked = self.validate(result, source_steps)["steps"][0]

        self.assertTrue(any("释放灶具" in item for item in repairs))
        self.assertEqual(checked["device_active_min"], 1)
        self.assertEqual(len(checked["human_intervals"]), 2)
        self.assertEqual(checked["human_intervals"][1]["start_min"], 18)

    def test_one_source_step_can_use_oven_then_fridge(self) -> None:
        result = copy.deepcopy(self.result)
        first = result["steps"][0]
        first.update({
            "action": "将鸡翅放在烤箱中烘烤8分钟",
            "duration_min": 9,
            "device_active_min": 9,
        })
        second = copy.deepcopy(first)
        second.update({
            "step_id": "s2",
            "source_step_no": 2,
            "action": "取出鸡翅，放凉后放入冰箱冷藏120分钟",
            "device_raw": "冰箱",
            "device": "冰箱",
            "device_source": "explicit",
            "mode_raw": "冷藏",
            "mode": "冷藏",
            "mode_source": "explicit",
            "temperature_c": None,
            "temperature_source": "unknown",
            "temperature_settings": [],
            "duration_min": 126,
            "human_active_min": 2,
            "human_intervals": [
                {"start_min": 0, "duration_min": 1, "source": "estimated"},
                {"start_min": 5, "duration_min": 1, "source": "estimated"},
            ],
            "device_active_start_min": 6,
            "device_active_min": 120,
            "depends_on": [],
        })
        result["steps"] = [first, second]
        source_steps = [
            {"no": 1, "text": "将鸡翅放在烤箱中烘烤8分钟。"},
            {"no": 2, "text": "取出鸡翅，放凉后放入冰箱冷藏120分钟。"},
        ]
        repairs = _repair_deterministic_result(result, source_steps)
        self.assertTrue(repairs)
        repaired = result["steps"][1]
        self.assertEqual(repaired["device"], "烤箱")
        self.assertEqual(repaired["device_active_min"], 1)
        self.assertEqual(repaired["additional_device_usages"][0]["device"], "冰箱")
        self.assertEqual(
            repaired["additional_device_usages"][0]["temperature_settings"][0]["value_c"],
            4,
        )
        checked = self.validate(result, source_steps)["steps"][1]
        self.assertEqual(checked["depends_on"], ["s1"])

    def test_long_recipe_is_merged_from_step_batches(self) -> None:
        first_result = copy.deepcopy(self.result)
        second_step = copy.deepcopy(self.result["steps"][0])
        second_step.update({"step_id": "s1", "source_step_no": 2})
        second_result = {"ingredients": [], "steps": [second_step]}
        source_steps = [
            {"no": 1, "text": "第一步。"},
            {"no": 2, "text": "第二步。"},
        ]
        with patch("parse_recipes.call_deepseek", side_effect=[first_result, second_result]) as mocked:
            merged = call_deepseek_chunked(
                self.recipe,
                source_steps,
                self.devices,
                "test-key",
                "https://example.invalid",
                "test-model",
                batch_size=1,
            )
        self.assertEqual(mocked.call_count, 2)
        self.assertEqual([step["step_id"] for step in merged["steps"]], ["s1", "s2"])
        self.assertEqual(merged["ingredients"], first_result["ingredients"])

    def test_stove_process_temperature_is_preserved(self) -> None:
        result = copy.deepcopy(self.result)
        step = result["steps"][0]
        step.update({"action": "用平底锅中火将油加热至180℃",
                     "device_raw": "平底锅", "device": "灶具", "device_source": "normalized",
                     "mode_raw": None, "mode": None, "mode_source": "unknown",
                     "temperature_c": 180,
                     "temperature_settings": [{"zone": "锅内油温", "value_c": 180, "source": "explicit"}],
                     "heat_level_raw": "中火", "heat_level": 4,
                     "duration_min": 2, "human_active_min": 2,
                     "device_active_start_min": 0, "device_active_min": 2,
                     "can_run_unattended": False})
        source_steps = [{"no": 1, "text": "用平底锅中火将油加热至180℃。"}]
        checked = self.validate(result, source_steps)["steps"][0]
        self.assertEqual(checked["temperature_c"], 180)
        self.assertEqual(checked["heat_level"], 4)

    def test_stove_step_requires_normalized_heat_level(self) -> None:
        result = copy.deepcopy(self.result)
        step = result["steps"][0]
        step.update({"action": "用锅将食材翻炒3分钟",
                     "device_raw": "锅", "device": "灶具", "device_source": "normalized",
                     "mode_raw": None, "mode": None, "mode_source": "unknown",
                     "temperature_c": None, "temperature_source": "unknown", "temperature_settings": [],
                     "heat_level_raw": None, "heat_level": None,
                     "duration_min": 3, "human_active_min": 3,
                     "device_active_start_min": 0, "device_active_min": 3})
        source_steps = [{"no": 1, "text": "用锅将食材翻炒3分钟。"}]
        with self.assertRaisesRegex(ValueError, "1～7 档"):
            self.validate(result, source_steps)

    def test_negative_fridge_temperature_is_valid(self) -> None:
        result = copy.deepcopy(self.result)
        step = result["steps"][0]
        step.update({"action": "将食材放入冰箱冷冻60分钟",
                     "device_raw": "冰箱", "device": "冰箱", "mode_raw": "冷冻",
                     "mode": "冷冻", "temperature_c": -18,
                     "temperature_settings": [{"zone": "冷冻室", "value_c": -18, "source": "explicit"}],
                     "duration_min": 61, "device_active_start_min": 1, "device_active_min": 60})
        source_steps = [{"no": 1, "text": "将食材放入冰箱冷冻60分钟。"}]
        self.assertEqual(self.validate(result, source_steps)["steps"][0]["temperature_c"], -18)

    def test_cold_storage_requires_temperature(self) -> None:
        result = copy.deepcopy(self.result)
        step = result["steps"][0]
        step.update({
            "action": "将食材放入冰箱冷冻60分钟",
            "device_raw": "冰箱", "device": "冰箱", "mode_raw": "冷冻", "mode": "冷冻",
            "temperature_c": None, "temperature_source": "unknown", "temperature_settings": [],
        })
        source_steps = [{"no": 1, "text": "将食材放入冰箱冷冻60分钟。"}]
        with self.assertRaisesRegex(ValueError, "必须提供温度"):
            self.validate(result, source_steps)

    def test_split_upper_and_lower_temperatures_are_preserved(self) -> None:
        result = copy.deepcopy(self.result)
        result["steps"][0].update({
            "temperature_settings": [
                {"zone": "上火", "value_c": 180, "source": "explicit"},
                {"zone": "下火", "value_c": 165, "source": "explicit"},
            ],
        })
        settings = self.validate(result)["steps"][0]["temperature_settings"]
        self.assertEqual([item["value_c"] for item in settings], [180, 165])

    def test_device_can_be_released_before_step_finishes(self) -> None:
        result = copy.deepcopy(self.result)
        step = result["steps"][0]
        step.update({
            "action": "将水煮沸后离火放凉15分钟",
            "device_raw": "锅", "device": "灶具", "device_source": "normalized",
            "mode_raw": None, "mode": None, "mode_source": "unknown",
            "temperature_c": None, "temperature_source": "unknown", "temperature_settings": [],
            "heat_level_raw": "大火", "heat_level": 7,
            "duration_min": 18, "human_active_min": 3,
            "device_active_start_min": 0, "device_active_min": 3,
            "device_active_source": "estimated",
        })
        source_steps = [{"no": 1, "text": "将水煮沸后离火放凉15分钟。"}]
        checked = self.validate(result, source_steps)["steps"][0]
        self.assertEqual(checked["device_active_min"], 3)
        self.assertEqual(checked["duration_min"], 18)

    def test_continuous_stove_must_stay_occupied_until_step_end(self) -> None:
        result = copy.deepcopy(self.result)
        step = result["steps"][0]
        step.update({
            "action": "将炒锅中倒入食用油烧热，加热至沸腾后浇在鲈鱼背上",
            "device_raw": "炒锅", "device": "灶具", "device_source": "normalized",
            "mode_raw": None, "mode": None, "mode_source": "unknown",
            "temperature_c": None, "temperature_source": "unknown", "temperature_settings": [],
            "heat_level_raw": None, "heat_level": 5,
            "duration_min": 3, "human_active_min": 3,
            "device_active_start_min": 0, "device_active_min": 2,
            "device_active_source": "estimated",
        })
        source_steps = [{"no": 1, "text": step["action"] + "。"}]
        with self.assertRaisesRegex(ValueError, "必须持续到步骤结束"):
            self.validate(result, source_steps)

        step["device_active_min"] = 3
        checked = self.validate(result, source_steps)["steps"][0]
        self.assertEqual(checked["device_active_min"], 3)

    def test_loading_an_explicit_device_cannot_omit_device(self) -> None:
        result = copy.deepcopy(self.result)
        step = result["steps"][0]
        step.update({
            "action": "将鱼盘放入蒸箱",
            "device_raw": None, "device": None, "device_source": "unknown",
            "mode_raw": None, "mode": None, "mode_source": "unknown",
            "temperature_c": None, "temperature_source": "unknown", "temperature_settings": [],
            "device_active_start_min": 0, "device_active_min": 0,
            "device_active_source": "not_applicable",
        })
        source_steps = [{"no": 1, "text": "将鱼盘放入蒸箱。"}]
        with self.assertRaisesRegex(ValueError, "device 必须标记"):
            self.validate(result, source_steps)

    def test_takeout_step_keeps_same_device_and_direct_dependency(self) -> None:
        result = copy.deepcopy(self.result)
        source_steps = [
            {"no": 1, "text": "将鱼盘放入蒸箱。"},
            {"no": 2, "text": "选择普通蒸模式，以100℃蒸8分钟。"},
            {"no": 3, "text": "烹饪结束后取出鱼盘。"},
        ]
        load_step = copy.deepcopy(result["steps"][0])
        load_step.update({
            "action": "将鱼盘放入蒸箱",
            "device_raw": "蒸箱", "device": "蒸箱", "device_source": "explicit",
            "mode_raw": None, "mode": None, "mode_source": "unknown",
            "temperature_c": None, "temperature_source": "unknown", "temperature_settings": [],
            "duration_min": 1, "human_active_min": 1,
            "device_active_start_min": 0, "device_active_min": 1,
            "depends_on": [],
        })
        run_step = copy.deepcopy(load_step)
        run_step.update({
            "step_id": "s2", "source_step_no": 2,
            "action": "选择普通蒸模式，以100℃蒸8分钟",
            "mode_raw": "普通蒸", "mode": "普通蒸", "mode_source": "explicit",
            "temperature_c": 100, "temperature_source": "explicit",
            "temperature_settings": [{"zone": "整体", "value_c": 100, "source": "explicit"}],
            "duration_min": 9, "human_active_min": 1,
            "device_active_start_min": 0, "device_active_min": 9,
            "depends_on": ["s1"],
        })
        takeout_step = copy.deepcopy(load_step)
        takeout_step.update({
            "step_id": "s3", "source_step_no": 3,
            "action": "烹饪结束后取出鱼盘",
            "duration_min": 2, "human_active_min": 2,
            "device_active_start_min": 0, "device_active_min": 1,
            "depends_on": ["s2"],
        })
        result["steps"] = [load_step, run_step, takeout_step]

        checked = self.validate(copy.deepcopy(result), source_steps)
        self.assertEqual([step["device"] for step in checked["steps"]], ["蒸箱"] * 3)

        missing_device = copy.deepcopy(result)
        missing_device["steps"][2].update({
            "device_raw": None, "device": None, "device_source": "unknown",
            "device_active_start_min": 0, "device_active_min": 0,
            "device_active_source": "not_applicable",
        })
        with self.assertRaisesRegex(ValueError, "取出步骤"):
            self.validate(missing_device, source_steps)

        missing_dependency = copy.deepcopy(result)
        missing_dependency["steps"][2]["depends_on"] = []
        with self.assertRaisesRegex(ValueError, "必须直接依赖"):
            self.validate(missing_dependency, source_steps)

    def test_device_occupancy_cannot_exceed_step(self) -> None:
        result = copy.deepcopy(self.result)
        result["steps"][0].update({"device_active_start_min": 2, "device_active_min": 15})
        with self.assertRaisesRegex(ValueError, "设备占用区间"):
            self.validate(result)

    def test_ingredient_must_come_from_source_list(self) -> None:
        result = copy.deepcopy(self.result)
        result["ingredients"][0]["source_text"] = "牛肉500克"
        with self.assertRaisesRegex(ValueError, "食材原文"):
            self.validate(result)

    def test_same_main_ingredient_requires_dependency(self) -> None:
        result = copy.deepcopy(self.result)
        first = copy.deepcopy(result["steps"][0])
        first.update({
            "action": "将鸡翅洗净", "device_raw": None, "device": None,
            "device_source": "unknown", "mode_raw": None, "mode": None,
            "mode_source": "unknown", "temperature_c": None,
            "temperature_source": "unknown", "temperature_settings": [],
            "duration_min": 2, "human_active_min": 2,
            "device_active_start_min": 0, "device_active_min": 0,
            "device_active_source": "not_applicable", "depends_on": [],
        })
        second = copy.deepcopy(first)
        second.update({
            "step_id": "s2", "source_step_no": 2,
            "action": "将洗净的鸡翅切块", "depends_on": [],
        })
        result["steps"] = [first, second]
        source_steps = [
            {"no": 1, "text": "将鸡翅洗净。"},
            {"no": 2, "text": "将洗净的鸡翅切块。"},
        ]
        with self.assertRaisesRegex(ValueError, "同一食材.*必须依赖"):
            self.validate(copy.deepcopy(result), source_steps)

        result["steps"][1]["depends_on"] = ["s1"]
        checked = self.validate(result, source_steps)
        self.assertEqual(checked["steps"][1]["depends_on"], ["s1"])

    def test_independent_ingredients_do_not_force_dependency(self) -> None:
        result = copy.deepcopy(self.result)
        self.recipe["食材清单"] = "主料：鸡翅3个；胡萝卜1根"
        result["ingredients"].append({
            "name": "胡萝卜", "amount_text": "1根", "quantity": 1, "unit": "根",
            "role": "主料", "category": "素菜", "source_text": "胡萝卜1根",
        })
        first = copy.deepcopy(result["steps"][0])
        first.update({
            "action": "将鸡翅洗净", "device_raw": None, "device": None,
            "device_source": "unknown", "mode_raw": None, "mode": None,
            "mode_source": "unknown", "temperature_c": None,
            "temperature_source": "unknown", "temperature_settings": [],
            "duration_min": 2, "human_active_min": 2,
            "device_active_start_min": 0, "device_active_min": 0,
            "device_active_source": "not_applicable", "depends_on": [],
        })
        second = copy.deepcopy(first)
        second.update({
            "step_id": "s2", "source_step_no": 2,
            "action": "将胡萝卜切片", "depends_on": [],
        })
        result["steps"] = [first, second]
        source_steps = [
            {"no": 1, "text": "将鸡翅洗净。"},
            {"no": 2, "text": "将胡萝卜切片。"},
        ]
        checked = self.validate(result, source_steps)
        self.assertEqual(checked["steps"][1]["depends_on"], [])

    def test_ready_cut_ingredient_without_source_step_does_not_invent_dependency(self) -> None:
        result = copy.deepcopy(self.result)
        self.recipe["食材清单"] = "主料：鸡翅3个；五花肉20克"
        result["ingredients"].append({
            "name": "五花肉", "amount_text": "20克", "quantity": 20, "unit": "克",
            "role": "主料", "category": "荤菜", "source_text": "五花肉20克",
        })
        first = copy.deepcopy(result["steps"][0])
        first.update({
            "action": "将鸡翅洗净", "device_raw": None, "device": None,
            "device_source": "unknown", "mode_raw": None, "mode": None,
            "mode_source": "unknown", "temperature_c": None,
            "temperature_source": "unknown", "temperature_settings": [],
            "duration_min": 2, "human_active_min": 2,
            "device_active_start_min": 0, "device_active_min": 0,
            "device_active_source": "not_applicable", "depends_on": [],
        })
        second = copy.deepcopy(first)
        second.update({
            "step_id": "s2", "source_step_no": 2,
            "action": "将切好的五花肉放入盘中", "depends_on": [],
        })
        result["steps"] = [first, second]
        source_steps = [
            {"no": 1, "text": "将鸡翅洗净。"},
            {"no": 2, "text": "将切好的五花肉放入盘中。"},
        ]

        checked = self.validate(result, source_steps)
        self.assertEqual(checked["steps"][1]["depends_on"], [])

    def test_screen_operation_requires_human_and_device(self) -> None:
        result = copy.deepcopy(self.result)
        step = result["steps"][0]
        step.update({
            "action": "将鱼盘放入蒸箱，按屏幕提示操作",
            "device_raw": "蒸箱", "device": "蒸箱", "device_source": "explicit",
            "mode_raw": "普通蒸", "mode": "普通蒸", "mode_source": "explicit",
            "temperature_c": 100, "temperature_source": "estimated",
            "temperature_settings": [{"zone": "整体", "value_c": 100, "source": "estimated"}],
            "duration_min": 10, "human_active_min": 0,
            "device_active_start_min": 0, "device_active_min": 10,
        })
        source_steps = [{"no": 1, "text": "将鱼盘放入蒸箱，按屏幕提示操作。"}]
        with self.assertRaisesRegex(ValueError, "human_active_min 必须大于0"):
            self.validate(result, source_steps)

        step["human_active_min"] = 1
        # 第一次失败校验已为旧格式测试数据补过兼容字段；这里同步修改人工区间，
        # 让第二次校验只验证“按屏幕操作必须标记设备”这一条规则。
        step["human_intervals"] = [
            {"start_min": 0, "duration_min": 1, "source": "estimated"},
        ]
        step.update({
            "device_raw": None, "device": None, "device_source": "unknown",
            "mode_raw": None, "mode": None, "mode_source": "unknown",
            "temperature_c": None, "temperature_source": "unknown", "temperature_settings": [],
            "device_active_start_min": 0, "device_active_min": 0,
            "device_active_source": "not_applicable",
        })
        with self.assertRaisesRegex(ValueError, "必须标记设备"):
            self.validate(result, source_steps)

    def test_preheat_requires_temperature_and_real_device_time(self) -> None:
        result = copy.deepcopy(self.result)
        step = result["steps"][0]
        step.update({
            "action": "将烤箱预热10分钟", "temperature_c": None,
            "temperature_source": "unknown", "temperature_settings": [],
            "duration_min": 10, "human_active_min": 1,
            "device_active_start_min": 0, "device_active_min": 10,
        })
        source_steps = [{"no": 1, "text": "将烤箱预热10分钟。"}]
        with self.assertRaisesRegex(ValueError, "必须提供工作温度"):
            self.validate(result, source_steps)

    def test_preheat_and_late_loading_use_two_human_intervals(self) -> None:
        result = copy.deepcopy(self.result)
        step = result["steps"][0]
        step.update({
            "action": "启动烤箱预热10分钟，完成后将鸡翅放入烤箱",
            "duration_min": 11,
            "human_active_min": 2,
            "device_active_start_min": 0,
            "device_active_min": 11,
            "human_intervals": [
                {"start_min": 0, "duration_min": 1, "source": "estimated"},
                {"start_min": 10, "duration_min": 1, "source": "estimated"},
            ],
            "preheat": {
                "required": True,
                "duration_min": 10,
                "source": "explicit",
                "whole_device_exclusive": True,
                "placement": "within_step",
            },
        })
        source_steps = [{
            "no": 1,
            "text": "启动烤箱预热10分钟，预热完成后将鸡翅放入烤箱。",
        }]
        checked = self.validate(copy.deepcopy(result), source_steps)["steps"][0]
        self.assertEqual(len(checked["human_intervals"]), 2)

        collapsed = copy.deepcopy(result)
        collapsed["steps"][0]["human_intervals"] = [
            {"start_min": 0, "duration_min": 2, "source": "estimated"},
        ]
        with self.assertRaisesRegex(ValueError, "至少两段"):
            self.validate(collapsed, source_steps)

        not_exclusive = copy.deepcopy(result)
        not_exclusive["steps"][0]["preheat"]["whole_device_exclusive"] = False
        with self.assertRaisesRegex(ValueError, "整机独占"):
            self.validate(not_exclusive, source_steps)

    def test_atomic_overwrite_replaces_old_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "recipe.json"
            path.write_text('{"version":"old"}', encoding="utf-8")
            _write_json_atomic(path, {"version": "new"})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["version"], "new")
            self.assertFalse(path.with_suffix(".json.tmp").exists())


if __name__ == "__main__":
    unittest.main()
