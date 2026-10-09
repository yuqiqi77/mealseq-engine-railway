"""容易阅读的多菜并行排程算法。

算法分为四步：
1. 把菜谱 JSON 中的每个步骤转换成任务；
2. 计算每道菜单独制作的时长；
3. 用反向起步时间让短菜尽量晚开始，从而接近同时出锅；
4. 按依赖逐个放置任务，并检查人工和设备资源冲突。

这里使用启发式算法而不是难以理解的数学求解器。它不能保证每次得到严格
全局最优解，但结果可复现、可解释，也便于后续替换为遗传算法或整数规划。
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable


EPSILON = 1e-9
TIME_STEP_MIN = 0.5
NO_OPERATION_RE = re.compile(r"^\s*(尽情)?(?:享用|品尝)(?:一下)?[^，。；;]*[。！!]?$" )
OUTCOME_ONLY_RE = re.compile(
    r"^(?:[\u4e00-\u9fffA-Za-z0-9（）()·\s]{1,24}完成(?:了|啦)?|"
    r"完成(?:[\u4e00-\u9fff]{1,24}|[，,\s]*可(?:以上桌|上桌|食用|享用))?|"
    r"描述.{1,40}|包好后即可食用(?:，冷藏后口感更佳)?)[。！!]?$"
)
IMPLICIT_PREHEATED_LOAD_RE = re.compile(
    r"(?:放入|放进|送入|推入|置入).{0,12}预热(?:好)?的(?:烤箱|蒸箱|设备)"
)
DEFAULT_PREHEAT_MIN = 10.0
ADVANCE_PREPARATION_RE = re.compile(
    r"腌|浸泡|泡发|泡软|冷藏|冷冻|静置|松弛|醒发|发酵"
)
ADVANCE_PREPARATION_MIN = 60.0
FORMAL_COOKING_RE = re.compile(r"炒|煎|炸|蒸|烤|煮|炖|焯|烹饪")
FORMAL_COOKING_DEVICES = {"灶具", "蒸箱", "烤箱", "洗碗机", "净咖机"}


@dataclass(frozen=True)
class DeviceRule:
    """一种设备的并行规则。"""

    capacity: int = 1
    same_setting_required: bool = False
    instances: tuple[str, ...] = ()
    background: bool = False


@dataclass(frozen=True)
class AdditionalDeviceUsage:
    """同一原始步骤中，主设备之外的一段设备占用。"""

    device: str
    start: float
    duration: float
    mode: str | None
    temperatures: tuple[tuple[str, float], ...]
    action: str
    instance_hints: tuple[str, ...]

    @property
    def signature(self) -> tuple:
        return self.mode, self.temperatures, None


@dataclass(frozen=True)
class Task:
    """菜谱中的一个不可拆分步骤。"""

    key: str
    recipe_id: str
    recipe_name: str
    step_id: str
    step_no: int
    action: str
    duration: float
    human_duration: float
    human_segments: tuple[tuple[float, float], ...]
    device: str | None
    device_start: float
    device_duration: float
    mode: str | None
    temperatures: tuple[tuple[str, float], ...]
    humidity: str | None
    heat_level_raw: str | None
    heat_level: int | None
    instance_hints: tuple[str, ...]
    additional_device_usages: tuple[AdditionalDeviceUsage, ...]
    dependencies: tuple[str, ...]
    whole_device_exclusive: bool
    original: dict

    @property
    def device_signature(self) -> tuple:
        """蒸箱/烤箱合并任务时用于比较模式、温度和湿度。"""

        return self.mode, self.temperatures, self.humidity


@dataclass(frozen=True)
class ScheduledTask:
    task: Task
    start: float
    resource_instances: tuple[str, ...] = ()

    @property
    def end(self) -> float:
        return self.start + self.task.duration

    @property
    def human_intervals(self) -> tuple[tuple[float, float], ...]:
        """返回步骤内所有人工片段对应的绝对排程区间。"""

        return tuple(
            (self.start + offset, self.start + offset + duration)
            for offset, duration in self.task.human_segments
            if duration > EPSILON
        )

    @property
    def device_interval(self) -> tuple[float, float] | None:
        if self.task.device is None or self.task.device_duration <= EPSILON:
            return None
        start = self.start + self.task.device_start
        return start, start + self.task.device_duration

    @property
    def additional_device_intervals(
        self,
    ) -> tuple[tuple[AdditionalDeviceUsage, tuple[float, float]], ...]:
        return tuple(
            (
                usage,
                (
                    self.start + usage.start,
                    self.start + usage.start + usage.duration,
                ),
            )
            for usage in self.task.additional_device_usages
            if usage.duration > EPSILON
        )


@dataclass
class SchedulePlan:
    """排程结果。response 是符合接口规范的 JSON 对象。"""

    response: dict
    metrics: dict
    scheduled_tasks: list[ScheduledTask]


def _number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} 必须是数字")
    return float(value)


def _is_no_operation(action: str) -> bool:
    """“尽情享用”一类结束语保留在菜谱中，但不占排程时间。"""

    return bool(NO_OPERATION_RE.fullmatch(action.strip()))


def _is_outcome_only_step(step: dict) -> bool:
    """完成语句没有人工和设备动作，不能凭估算时长推迟出锅。"""

    return bool(
        OUTCOME_ONLY_RE.fullmatch(str(step.get("action") or "").strip())
        and not step.get("device")
        and not step.get("additional_device_usages")
        and float(step.get("duration_min") or 0) <= 2
        and float(step.get("human_active_min") or 0) <= EPSILON
        and float(step.get("device_active_min") or 0) <= EPSILON
    )


def prepare_recipes_for_schedule(recipes: list[dict]) -> list[dict]:
    """把可提前完成的准备等待转换成不占正式排程时间的前置条件。

    只有首次正式烹饪之前、且总时长达到60分钟的腌制、浸泡、冷藏等长
    等待可提前完成；短等待继续参与正式排程。烹饪开始后再出现的等待也
    保留真实时长。原字段完整保存在 ``advance_preparation`` 中，供通知、
    菜谱详情和甘特图零时长标记使用。
    """

    prepared = copy.deepcopy(recipes)
    for recipe in prepared:
        steps = recipe.get("steps", [])
        formal_indexes = [
            index
            for index, step in enumerate(steps)
            if (
                step.get("device") in FORMAL_COOKING_DEVICES
                and float(step.get("device_active_min") or 0) > EPSILON
            )
            or FORMAL_COOKING_RE.search(str(step.get("action") or ""))
        ]
        if not formal_indexes:
            continue
        first_formal_index = min(formal_indexes)
        for index, step in enumerate(steps):
            action = str(step.get("action") or "")
            if index >= first_formal_index or not ADVANCE_PREPARATION_RE.search(action):
                continue
            duration = float(step.get("duration_min") or 0)
            if duration < ADVANCE_PREPARATION_MIN:
                continue
            if step.get("device") in FORMAL_COOKING_DEVICES:
                continue
            step["advance_preparation"] = {
                "duration_min": step.get("duration_min", 0),
                "human_intervals": copy.deepcopy(step.get("human_intervals", [])),
                "device": step.get("device"),
                "device_active_start_min": step.get("device_active_start_min", 0),
                "device_active_min": step.get("device_active_min", 0),
                "additional_device_usages": copy.deepcopy(
                    step.get("additional_device_usages", [])
                ),
            }
            step["duration_min"] = 0
            step["human_active_min"] = 0
            step["human_intervals"] = []
            step["device"] = None
            step["device_active_start_min"] = 0
            step["device_active_min"] = 0
            step["additional_device_usages"] = []
            step["mode"] = None
            step["temperature_settings"] = []
            step["humidity"] = None
            step["heat_level_raw"] = None
            step["heat_level"] = None
            step["whole_device_exclusive"] = False
    return prepared


def load_requested_recipes(request_items: list[dict], recipe_dir: Path) -> list[dict]:
    """按照请求中的 id 和 name 从本地菜谱库读取数据。"""

    if not isinstance(request_items, list) or not request_items:
        raise ValueError("请求体必须是非空菜谱数组")

    seen_ids: set[str] = set()
    recipes: list[dict] = []
    for item in request_items:
        if not isinstance(item, dict) or set(item) != {"id", "name"}:
            raise ValueError("每个请求菜谱必须且只能包含 id 和 name")
        recipe_id = str(item["id"])
        name = item["name"]
        if not recipe_id or not isinstance(name, str) or not name.strip():
            raise ValueError("菜谱 id 和 name 不能为空")
        if recipe_id in seen_ids:
            raise ValueError(f"请求中菜谱 id 重复：{recipe_id}")
        seen_ids.add(recipe_id)

        path = recipe_dir / f"{recipe_id}.json"
        if not path.exists():
            raise ValueError(f"菜谱库中不存在 id={recipe_id} 的菜谱")
        recipe = json.loads(path.read_text(encoding="utf-8"))
        if recipe.get("name") != name:
            raise ValueError(
                f"菜谱名称不匹配：id={recipe_id}，请求为“{name}”，库中为“{recipe.get('name')}”"
            )
        recipes.append(recipe)
    return recipes


def load_device_rules(device_path: Path) -> dict[str, DeviceRule]:
    """把设备清单转换为排程器真正需要的容量规则。"""

    catalog = json.loads(device_path.read_text(encoding="utf-8-sig"))
    rules: dict[str, DeviceRule] = {}
    for device in catalog["设备清单"]:
        constraint = device.get("调度约束", {})
        raw_instances = tuple(str(item) for item in constraint.get("资源实例", []))
        instance_count = len(raw_instances) or 1
        per_instance = int(constraint.get("单实例最大同时菜数", 1))
        same_setting = bool(constraint.get("并行条件"))
        background = (
            device.get("资源类别") == "后台资源"
            or constraint.get("是否阻塞排程") is False
        )
        if not raw_instances:
            instances = tuple(
                f"{device['设备名称']}{index}"
                for index in range(1, instance_count * per_instance + 1)
            )
        elif per_instance == 1:
            instances = raw_instances
        else:
            # 兼容旧配置中“一个设备实例、单实例可放多道菜”的写法。
            instances = tuple(
                f"{name}-位置{slot}"
                for name in raw_instances
                for slot in range(1, per_instance + 1)
            )
        rules[device["设备名称"]] = DeviceRule(
            capacity=instance_count * per_instance,
            same_setting_required=same_setting,
            instances=instances,
            background=background,
        )
    return rules


CHINESE_LAYER_NUMBERS = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9,
}


def _extract_instance_hints(device: str | None, step: dict, action: str) -> tuple[str, ...]:
    """从原始步骤中提取明确的灶眼或设备层位，不要求菜谱增加新字段。"""

    if device is None:
        return ()
    text = f"{step.get('source_text', '')} {action}"
    if device == "灶具":
        return tuple(name for name in ("左灶眼", "右灶眼") if name in text)
    if device == "冰箱":
        mode = step.get("mode")
        return ({"冷藏": ("冷藏区",), "冷冻": ("冷冻区",), "解冻": ("解冻区",)}.get(mode, ()))
    if device not in ("蒸箱", "烤箱"):
        return ()

    found: list[int] = []
    for raw in re.findall(r"第\s*([一二三四五六七八九1-9])\s*层", text):
        number = int(raw) if raw.isdigit() else CHINESE_LAYER_NUMBERS[raw]
        if number not in found:
            found.append(number)
    if found:
        return tuple(f"第{number}层" for number in found)
    for word in ("上层", "中层", "中间层", "下层"):
        if word in text:
            return (word,)
    return ()


def _preheat_parameters(steps: list[dict], start_index: int, device: str) -> tuple:
    """从当前或后续正式烹饪步骤取得预热所需模式和温度。"""

    for step in steps[start_index:]:
        if step.get("device") != device:
            continue
        temperatures = tuple(sorted(
            (item["zone"], float(item["value_c"]))
            for item in step.get("temperature_settings", [])
        ))
        if temperatures:
            return step.get("mode"), temperatures, step.get("humidity")
    raise ValueError(f"菜谱要求使用预热的{device}，但后续步骤没有提供预热温度")


def _preheat_action(
    device: str,
    mode: str | None,
    temperatures: tuple,
    duration: float,
) -> str:
    """生成接口可直接展示的简洁预热说明。"""

    temperature_text = "、".join(
        f"{zone}{value:g}℃" if zone != "整体" else f"{value:g}℃"
        for zone, value in temperatures
    )
    mode_text = f"，选择{mode}模式" if mode else ""
    return f"将{device}预热{mode_text}至{temperature_text}，预热{duration:g}分钟"


def _task_human_segments(step: dict, no_operation: bool) -> tuple[tuple[float, float], ...]:
    """读取多段人工区间；旧JSON自动退化为从步骤开头开始的一段。"""

    if no_operation:
        return ()
    raw = step.get("human_intervals")
    if isinstance(raw, list):
        return tuple(
            (
                _number(item["start_min"], "human_intervals.start_min"),
                _number(item["duration_min"], "human_intervals.duration_min"),
            )
            for item in raw
        )
    duration = _number(step["human_active_min"], "human_active_min")
    return ((0.0, duration),) if duration > EPSILON else ()


def _task_preheat(step: dict, combined_text: str) -> dict:
    """读取预热元数据；旧JSON按原文关键词兼容推断。"""

    value = step.get("preheat")
    if isinstance(value, dict):
        return value
    if "无需预热" in combined_text or "预热" not in combined_text:
        return {
            "required": False,
            "duration_min": 0,
            "whole_device_exclusive": False,
            "placement": "none",
        }
    return {
        "required": True,
        "duration_min": DEFAULT_PREHEAT_MIN,
        "whole_device_exclusive": True,
        "placement": (
            "before_step" if IMPLICIT_PREHEATED_LOAD_RE.search(combined_text)
            else "within_step"
        ),
    }


def _task_additional_device_usages(
    step: dict,
    no_operation: bool,
) -> tuple[AdditionalDeviceUsage, ...]:
    """读取一个原始步骤中主设备之外的设备占用；旧JSON默认没有。"""

    if no_operation:
        return ()
    usages: list[AdditionalDeviceUsage] = []
    for item in step.get("additional_device_usages", []):
        device = item["device"]
        temperatures = tuple(sorted(
            (setting["zone"], float(setting["value_c"]))
            for setting in item.get("temperature_settings", [])
        ))
        usages.append(AdditionalDeviceUsage(
            device=device,
            start=_number(item["start_min"], "additional_device_usages.start_min"),
            duration=_number(item["duration_min"], "additional_device_usages.duration_min"),
            mode=item.get("mode"),
            temperatures=temperatures,
            action=item["action"],
            instance_hints=_extract_instance_hints(
                device,
                {"source_text": step.get("source_text", ""), "mode": item.get("mode")},
                item["action"],
            ),
        ))
    return tuple(usages)


def build_tasks(recipes: list[dict]) -> list[Task]:
    """将菜谱步骤变成排程任务，并补齐菜谱明确要求但缺失的预热任务。"""

    tasks: list[Task] = []
    for recipe in recipes:
        recipe_id = str(recipe["recipe_id"])
        recipe_steps = recipe["steps"]
        schedule_step_no = 0
        for source_index, step in enumerate(recipe_steps):
            action = step["action"].strip()
            no_operation = _is_no_operation(action) or _is_outcome_only_step(step)
            duration = 0.0 if no_operation else _number(step["duration_min"], "duration_min")
            human_duration = 0.0 if no_operation else _number(step["human_active_min"], "human_active_min")
            human_segments = _task_human_segments(step, no_operation)
            device = None if no_operation else step["device"]
            device_start = 0.0 if no_operation else _number(step["device_active_start_min"], "device_active_start_min")
            device_duration = 0.0 if no_operation else _number(step["device_active_min"], "device_active_min")
            if duration < 0 or human_duration < 0 or human_duration > duration + EPSILON:
                raise ValueError(f"{recipe['name']} {step['step_id']} 的人工时长无效")
            if abs(sum(length for _, length in human_segments) - human_duration) > EPSILON:
                raise ValueError(f"{recipe['name']} {step['step_id']} 的多段人工总时长不一致")
            previous_human_end = 0.0
            for offset, length in human_segments:
                if offset < previous_human_end - EPSILON or offset + length > duration + EPSILON:
                    raise ValueError(f"{recipe['name']} {step['step_id']} 的人工区间无效")
                previous_human_end = offset + length
            if device_start < 0 or device_duration < 0 or device_start + device_duration > duration + EPSILON:
                raise ValueError(f"{recipe['name']} {step['step_id']} 的设备占用区间无效")

            temperatures = tuple(sorted(
                (item["zone"], float(item["value_c"]))
                for item in step.get("temperature_settings", [])
            ))
            dependencies = tuple(
                f"{recipe_id}:{dependency}" for dependency in step["depends_on"]
            )
            combined_text = f"{step.get('source_text', '')} {action}"
            preheat = _task_preheat(step, combined_text)
            additional_device_usages = _task_additional_device_usages(step, no_operation)
            if any(
                usage.start < 0
                or usage.duration <= 0
                or usage.start + usage.duration > duration + EPSILON
                for usage in additional_device_usages
            ):
                raise ValueError(f"{recipe['name']} {step['step_id']} 的附加设备区间无效")

            # “放入预热好的烤箱”说明预热已经发生，但原菜谱没有把它单列成步骤。
            # 解析JSON仍保持原步骤数；这里只按解析出的预热时长补真实设备任务。
            if (
                device in ("烤箱", "蒸箱")
                and preheat["required"]
                and preheat["placement"] == "before_step"
            ):
                preheat_mode, preheat_temperatures, preheat_humidity = _preheat_parameters(
                    recipe_steps, source_index, device
                )
                schedule_step_no += 1
                preheat_key = f"{recipe_id}:{step['step_id']}_preheat"
                preheat_duration = _number(preheat["duration_min"], "preheat.duration_min")
                preheat_human = min(1.0, preheat_duration)
                tasks.append(Task(
                    key=preheat_key,
                    recipe_id=recipe_id,
                    recipe_name=recipe["name"],
                    step_id=f"{step['step_id']}_preheat",
                    step_no=schedule_step_no,
                    action=_preheat_action(
                        device, preheat_mode, preheat_temperatures, preheat_duration
                    ),
                    duration=preheat_duration,
                    human_duration=preheat_human,
                    human_segments=((0.0, preheat_human),),
                    device=device,
                    device_start=0.0,
                    device_duration=preheat_duration,
                    mode=preheat_mode,
                    temperatures=preheat_temperatures,
                    humidity=preheat_humidity,
                    heat_level_raw=None,
                    heat_level=None,
                    instance_hints=("__whole_device__",),
                    additional_device_usages=(),
                    # 先完成本步骤原有前置工序，再预热并立即放入设备。
                    # 这样不会在食材尚未准备好时提前几十分钟空烧烤箱。
                    dependencies=dependencies,
                    whole_device_exclusive=True,
                    original={"virtual_preheat": True, "source_step_id": step["step_id"]},
                ))
                # 放入动作必须等预热完成；预热期间会独占整台设备。
                dependencies = tuple(dict.fromkeys((*dependencies, preheat_key)))

            schedule_step_no += 1
            tasks.append(Task(
                key=f"{recipe_id}:{step['step_id']}",
                recipe_id=recipe_id,
                recipe_name=recipe["name"],
                step_id=step["step_id"],
                step_no=schedule_step_no,
                action=action,
                duration=duration,
                human_duration=human_duration,
                human_segments=human_segments,
                device=device,
                device_start=device_start,
                device_duration=device_duration,
                mode=step["mode"],
                temperatures=temperatures,
                humidity=step["humidity"],
                heat_level_raw=step["heat_level_raw"],
                heat_level=step["heat_level"],
                instance_hints=(
                    ("__whole_device__",)
                    if preheat["required"]
                    and preheat["placement"] == "within_step"
                    and preheat.get("whole_device_exclusive", False)
                    else _extract_instance_hints(device, step, action)
                ),
                additional_device_usages=additional_device_usages,
                dependencies=dependencies,
                whole_device_exclusive=bool(
                    preheat["required"]
                    and preheat["placement"] == "within_step"
                    and preheat.get("whole_device_exclusive", False)
                ),
                original=step,
            ))
    keys = [task.key for task in tasks]
    if len(keys) != len(set(keys)):
        raise ValueError("菜谱中存在重复的步骤 ID")
    return tasks


def _overlaps(first: tuple[float, float], second: tuple[float, float]) -> bool:
    return first[0] < second[1] - EPSILON and second[0] < first[1] - EPSILON


def _has_capacity(
    new_interval: tuple[float, float],
    existing: Iterable[tuple[float, float]],
    capacity: int,
) -> bool:
    """检查一个时间段加入后，任意时刻的任务数是否超过容量。"""

    relevant = [interval for interval in existing if _overlaps(new_interval, interval)]
    points = sorted({new_interval[0], new_interval[1], *(x for item in relevant for x in item)})
    for left, right in zip(points, points[1:]):
        if right - left <= EPSILON:
            continue
        middle = (left + right) / 2
        count = 1 + sum(start <= middle < end for start, end in relevant)
        if count > capacity:
            return False
    return True


def _instance_options(rule: DeviceRule, hints: tuple[str, ...]) -> list[tuple[str, ...]]:
    """把“第3层/中层/左灶眼”等提示转换为可选的具体实例组合。"""

    instances = rule.instances or tuple(f"位置{index}" for index in range(1, rule.capacity + 1))
    if "__whole_device__" in hints:
        return [instances]
    if not hints:
        return [(item,) for item in instances]

    exact: list[str] = []
    for hint in hints:
        matches = [item for item in instances if item == hint or item.endswith(hint)]
        if matches:
            exact.extend(item for item in matches if item not in exact)
    if exact:
        # 同一步明确提到多个层位时，表示它们必须同时被占用。
        return [tuple(exact)]
    if any(re.fullmatch(r"第\d+层|左灶眼|右灶眼|冷藏区|冷冻区|解冻区", hint) for hint in hints):
        return []

    if any(hint in ("中层", "中间层") for hint in hints):
        count = len(instances)
        middle = instances[(count - 1) // 2:(count // 2) + 1]
        return [(item,) for item in middle]
    if "上层" in hints:
        return [(instances[0],)]
    if "下层" in hints:
        return [(instances[-1],)]
    return [(item,) for item in instances]


def _assign_calendar_instances(
    entries: list[tuple[tuple[float, float], tuple[str, ...]]],
    rule: DeviceRule,
) -> list[tuple[str, ...]] | None:
    """给一组设备区间分配实例；返回 None 表示无合法层位/灶眼。"""

    if rule.background:
        return [options[0] for _, hints in entries if (options := _instance_options(rule, hints))]

    choices = [_instance_options(rule, hints) for _, hints in entries]
    if any(not item for item in choices):
        return None
    order = sorted(range(len(entries)), key=lambda index: (len(choices[index]), entries[index][0][0]))
    assigned: list[tuple[str, ...] | None] = [None] * len(entries)

    def place(position: int) -> bool:
        if position == len(order):
            return True
        index = order[position]
        interval = entries[index][0]
        for option in choices[index]:
            collision = False
            for other_index, other_option in enumerate(assigned):
                if other_option is None or not set(option) & set(other_option):
                    continue
                if _overlaps(interval, entries[other_index][0]):
                    collision = True
                    break
            if collision:
                continue
            assigned[index] = option
            if place(position + 1):
                return True
            assigned[index] = None
        return False

    if not place(0):
        return None
    return [item or () for item in assigned]


def _human_break_is_ok(
    new_intervals: Iterable[tuple[float, float]],
    existing: list[tuple[float, float]],
    max_continuous: float,
    minimum_break: float,
) -> bool:
    """避免用户长时间连续手工操作；短于 minimum_break 的间隔不算休息。"""

    additions = list(new_intervals)
    if any(end - start > max_continuous + EPSILON for start, end in additions):
        return False  # 原始片段不可拆时应拒绝，而不是悄悄违反硬上限。
    intervals = sorted([*existing, *additions])
    if not intervals:
        return True
    block_start, block_end = intervals[0]
    for start, end in intervals[1:]:
        if start - block_end < minimum_break - EPSILON:
            block_end = max(block_end, end)
        else:
            if block_end - block_start > max_continuous + EPSILON:
                return False
            block_start, block_end = start, end
    return block_end - block_start <= max_continuous + EPSILON


def _slot_is_available(
    task: Task,
    start: float,
    human_calendar: list[tuple[float, float]],
    device_calendar: dict[str, list[tuple[tuple[float, float], tuple, tuple[str, ...]]]],
    device_rules: dict[str, DeviceRule],
    max_continuous_human: float,
    human_break: float,
) -> bool:
    human_intervals = [
        (start + offset, start + offset + duration)
        for offset, duration in task.human_segments
        if duration > EPSILON
    ]
    if human_intervals:
        if any(
            not _has_capacity(interval, human_calendar, capacity=1)
            for interval in human_intervals
        ):
            return False
        if not _human_break_is_ok(
            human_intervals, human_calendar, max_continuous_human, human_break
        ):
            return False

    current_uses: list[tuple[str, tuple[float, float], tuple, tuple[str, ...]]] = []
    if task.device is not None and task.device_duration > EPSILON:
        current_uses.append((
            task.device,
            (
                start + task.device_start,
                start + task.device_start + task.device_duration,
            ),
            task.device_signature,
            task.instance_hints,
        ))
    for usage in task.additional_device_usages:
        current_uses.append((
            usage.device,
            (start + usage.start, start + usage.start + usage.duration),
            usage.signature,
            usage.instance_hints,
        ))

    pending: dict[str, list[tuple[tuple[float, float], tuple, tuple[str, ...]]]] = {}
    for device, interval, signature, hints in current_uses:
        rule = device_rules.get(device, DeviceRule())
        if rule.background:
            continue
        records = [*device_calendar.get(device, []), *pending.get(device, [])]
        if _assign_calendar_instances(
            [(item[0], item[2]) for item in records] + [(interval, hints)],
            rule,
        ) is None:
            return False
        if rule.same_setting_required:
            for old_interval, old_signature, _ in records:
                if _overlaps(interval, old_interval) and old_signature != signature:
                    return False
        pending.setdefault(device, []).append((interval, signature, hints))
    return True


def _append_device_calendar_item(
    item: ScheduledTask,
    device_calendar: dict[str, list[tuple[tuple[float, float], tuple, tuple[str, ...]]]],
) -> None:
    """把主设备及附加设备的实际占用统一加入资源日历。"""

    if item.device_interval and item.task.device:
        device_calendar.setdefault(item.task.device, []).append((
            item.device_interval,
            item.task.device_signature,
            item.task.instance_hints,
        ))
    for usage, interval in item.additional_device_intervals:
        device_calendar.setdefault(usage.device, []).append((
            interval,
            usage.signature,
            usage.instance_hints,
        ))


def _round_up_to_step(value: float) -> float:
    return math.ceil((value - EPSILON) / TIME_STEP_MIN) * TIME_STEP_MIN


def _find_earliest_start(
    task: Task,
    earliest: float,
    human_calendar: list[tuple[float, float]],
    device_calendar: dict[str, list[tuple[tuple[float, float], tuple, tuple[str, ...]]]],
    device_rules: dict[str, DeviceRule],
    max_continuous_human: float,
    human_break: float,
) -> float:
    """从 earliest 起以半分钟为步长寻找第一个合法位置。"""

    if any(length > max_continuous_human + EPSILON for _, length in task.human_segments):
        raise ValueError(
            f"{task.recipe_name} {task.step_id} 的单段人工操作超过"
            f"{max_continuous_human:g} 分钟上限，必须拆分菜谱步骤"
        )

    candidate = _round_up_to_step(earliest)
    for _ in range(200_000):
        if _slot_is_available(
            task,
            candidate,
            human_calendar,
            device_calendar,
            device_rules,
            max_continuous_human,
            human_break,
        ):
            return candidate
        candidate += TIME_STEP_MIN
    raise RuntimeError(f"无法为步骤寻找可用时间：{task.recipe_name} {task.step_id}")


def _remaining_path_lengths(tasks: list[Task]) -> dict[str, float]:
    """计算每个步骤到菜谱结束还剩多长，用于决定排程优先级。"""

    by_key = {task.key: task for task in tasks}
    successors: dict[str, list[str]] = {task.key: [] for task in tasks}
    for task in tasks:
        for dependency in task.dependencies:
            if dependency not in by_key:
                raise ValueError(f"不存在的前置步骤：{dependency}")
            successors[dependency].append(task.key)

    cache: dict[str, float] = {}

    def visit(key: str, visiting: set[str]) -> float:
        if key in cache:
            return cache[key]
        if key in visiting:
            raise ValueError("步骤依赖存在环，无法排程")
        visiting.add(key)
        tail = max((visit(item, visiting) for item in successors[key]), default=0.0)
        visiting.remove(key)
        cache[key] = by_key[key].duration + tail
        return cache[key]

    for key in by_key:
        visit(key, set())
    return cache


def _place_tasks(
    tasks: list[Task],
    recipe_release_times: dict[str, float],
    device_rules: dict[str, DeviceRule],
    max_continuous_human: float,
    human_break: float,
    recipe_rank: dict[str, int] | None = None,
    task_priority: dict[str, float] | None = None,
    task_release_times: dict[str, float] | None = None,
) -> list[ScheduledTask]:
    """按依赖关系逐个放置步骤。

    task_priority 是给遗传算法使用的“随机键”：数值越小越优先。
    普通启发式调用不传它，因此原来的行为保持不变。
    """

    remaining = _remaining_path_lengths(tasks)
    unscheduled = {task.key: task for task in tasks}
    scheduled: dict[str, ScheduledTask] = {}
    human_calendar: list[tuple[float, float]] = []
    device_calendar: dict[str, list[tuple[tuple[float, float], tuple, tuple[str, ...]]]] = {}

    while unscheduled:
        ready = [
            task for task in unscheduled.values()
            if all(dependency in scheduled for dependency in task.dependencies)
        ]
        if not ready:
            raise ValueError("步骤依赖存在环，无法排程")

        # 目标完成时间更晚、剩余关键路径更长的步骤优先。
        ready.sort(key=lambda task: (
            recipe_rank.get(task.recipe_id, 0) if recipe_rank else 0,
            task_priority.get(task.key, 0.0) if task_priority else 0.0,
            -(recipe_release_times[task.recipe_id] + remaining[task.key]),
            -remaining[task.key],
            task.recipe_name,
            task.step_no,
        ))
        task = ready[0]
        dependency_end = max(
            (scheduled[key].end for key in task.dependencies),
            default=recipe_release_times[task.recipe_id],
        )
        earliest = max(
            recipe_release_times[task.recipe_id],
            dependency_end,
            task_release_times.get(task.key, 0.0) if task_release_times else 0.0,
        )
        start = _find_earliest_start(
            task,
            earliest,
            human_calendar,
            device_calendar,
            device_rules,
            max_continuous_human,
            human_break,
        )
        placed = ScheduledTask(task=task, start=start)
        scheduled[task.key] = placed
        del unscheduled[task.key]

        human_calendar.extend(placed.human_intervals)
        _append_device_calendar_item(placed, device_calendar)

    return sorted(scheduled.values(), key=lambda item: (item.start, item.task.recipe_name, item.task.step_no))


def _recipe_bounds(schedule: list[ScheduledTask]) -> dict[str, tuple[float, float]]:
    grouped: dict[str, list[ScheduledTask]] = {}
    for item in schedule:
        grouped.setdefault(item.task.recipe_id, []).append(item)
    bounds = {}
    for recipe_id, items in grouped.items():
        relevant = [item for item in items if item.task.duration > EPSILON]
        if not relevant:
            bounds[recipe_id] = (0.0, 0.0)
        else:
            bounds[recipe_id] = (
                min(item.start for item in relevant),
                max(item.end for item in relevant),
            )
    return bounds


def _maximum_continuous_human(schedule: list[ScheduledTask], minimum_break: float) -> float:
    intervals = sorted(
        interval for item in schedule for interval in item.human_intervals
    )
    if not intervals:
        return 0.0
    block_start, block_end = intervals[0]
    longest = 0.0
    for start, end in intervals[1:]:
        if start - block_end < minimum_break - EPSILON:
            block_end = max(block_end, end)
        else:
            longest = max(longest, block_end - block_start)
            block_start, block_end = start, end
    return max(longest, block_end - block_start)


def _score_schedule(schedule: list[ScheduledTask], human_break: float) -> tuple[float, float, float]:
    """综合评分：兼顾总耗时、同出锅和连续人工时长。"""

    bounds = _recipe_bounds(schedule)
    finishes = [end for _, end in bounds.values()]
    makespan = max(finishes, default=0.0)
    spread = max(finishes, default=0.0) - min(finishes, default=0.0)
    continuous = _maximum_continuous_human(schedule, human_break)
    # 同出锅是独立评分项：先强力压缩超过5分钟的部分，再在5分钟内继续靠拢。
    # 总工期仍保留权重，避免为了完全同刻出锅而无意义地拖长整场烹饪。
    combined = (
        makespan
        + 10 * spread
        + 120 * max(0.0, spread - 5)
        + 0.1 * continuous
        + 20 * max(0.0, continuous - 30)
    )
    return combined, makespan, spread


def _is_generic_preparation_task(task: Task) -> bool:
    """防止“准备食材”因缺少后继关系被误当成最终出锅步骤。"""

    normalized = re.sub(r"[，。；：、\s]+", "", task.action)
    return bool(re.fullmatch(r"(?:准备(?:好|齐|所有|全部)?食材|备齐(?:所有|全部)?食材)", normalized))


def _right_justify(
    schedule: list[ScheduledTask],
    device_rules: dict[str, DeviceRule],
    max_continuous_human: float,
    human_break: float,
) -> list[ScheduledTask]:
    """在不延长总工期的前提下，把过早完成的菜向后移动。

    先移动每道菜的最后步骤，再依次移动它的前置步骤。这个过程不会改变
    步骤时长，也不会打乱依赖，只是利用原计划中的空隙缩小出锅时间差。
    """

    original = list(schedule)
    current = {item.task.key: item for item in schedule}
    target_finish = max((item.end for item in schedule), default=0.0)
    successors: dict[str, list[str]] = {key: [] for key in current}
    for item in schedule:
        for dependency in item.task.dependencies:
            successors[dependency].append(item.task.key)

    recipe_finishes = _recipe_bounds(schedule)
    recipe_order = sorted(recipe_finishes, key=lambda key: recipe_finishes[key][1])
    for recipe_id in recipe_order:
        recipe_tasks = sorted(
            (item.task for item in current.values() if item.task.recipe_id == recipe_id),
            key=lambda task: task.step_no,
            reverse=True,
        )
        for task in recipe_tasks:
            if task.duration <= EPSILON or _is_generic_preparation_task(task):
                continue
            item = current[task.key]
            latest_end = min(
                (current[key].start for key in successors[task.key]),
                default=target_finish,
            )
            latest_start = math.floor((latest_end - task.duration + EPSILON) / TIME_STEP_MIN) * TIME_STEP_MIN
            dependency_end = max(
                (current[key].end for key in task.dependencies),
                default=0.0,
            )

            other_items = [value for key, value in current.items() if key != task.key]
            human_calendar = [
                interval for value in other_items for interval in value.human_intervals
            ]
            device_calendar: dict[str, list[tuple[tuple[float, float], tuple, tuple[str, ...]]]] = {}
            for value in other_items:
                _append_device_calendar_item(value, device_calendar)

            candidate = latest_start
            while candidate > item.start + EPSILON and candidate >= dependency_end - EPSILON:
                if _slot_is_available(
                    task,
                    candidate,
                    human_calendar,
                    device_calendar,
                    device_rules,
                    max_continuous_human,
                    human_break,
                ):
                    current[task.key] = ScheduledTask(task=task, start=candidate)
                    break
                candidate -= TIME_STEP_MIN

    candidate_schedule = sorted(
        current.values(),
        key=lambda item: (item.start, item.task.recipe_name, item.task.step_no),
    )
    # 右移只是优化，绝不能牺牲依赖、人工、设备容量/参数或连续设备会话。
    # 任一硬约束不满足，或综合目标没有改善，直接返回原排程。
    try:
        validate_schedule(candidate_schedule, device_rules)
        try:
            from .static_solver_common import (
                foreground_sessions_are_contiguous,
                infer_device_sessions,
            )
        except ImportError:
            from static_solver_common import (
                foreground_sessions_are_contiguous,
                infer_device_sessions,
            )
        sessions = infer_device_sessions([item.task for item in candidate_schedule])
        legal = (
            foreground_sessions_are_contiguous(candidate_schedule, sessions)
            and _maximum_continuous_human(candidate_schedule, human_break)
            <= max_continuous_human + EPSILON
        )
    except ValueError:
        legal = False
    if not legal or _score_schedule(candidate_schedule, human_break) >= _score_schedule(original, human_break):
        return original
    return candidate_schedule


def create_schedule_heuristic(
    recipes: list[dict],
    device_rules: dict[str, DeviceRule],
    max_continuous_human: float = 30.0,
    human_break: float = 1.0,
) -> tuple[list[ScheduledTask], dict[str, float]]:
    """原有的可解释启发式求解器，兼作新求解器的初始解和兜底。"""

    tasks = build_tasks(recipes)
    recipe_ids = [str(recipe["recipe_id"]) for recipe in recipes]

    standalone: dict[str, float] = {}
    for recipe_id in recipe_ids:
        own_tasks = [task for task in tasks if task.recipe_id == recipe_id]
        own_schedule = _place_tasks(
            own_tasks,
            {recipe_id: 0.0},
            device_rules,
            max_continuous_human,
            human_break,
        )
        standalone[recipe_id] = max((item.end for item in own_schedule), default=0.0)

    candidates: list[list[ScheduledTask]] = []
    asap_release = {recipe_id: 0.0 for recipe_id in recipe_ids}
    target = max(standalone.values(), default=0.0)
    aligned_release = {
        recipe_id: max(0.0, target - standalone[recipe_id])
        for recipe_id in recipe_ids
    }

    # 不同菜谱优先顺序可能产生不同空隙。依次尝试轮换和反向轮换，
    # 再用同一评分函数选出最好的结果；固定顺序保证每次运行可复现。
    orders: list[list[str] | None] = [None]
    for base_order in (recipe_ids, list(reversed(recipe_ids))):
        for offset in range(len(base_order)):
            order = base_order[offset:] + base_order[:offset]
            if order not in orders:
                orders.append(order)

    for release in (asap_release, aligned_release):
        for order in orders:
            rank = None if order is None else {recipe_id: index for index, recipe_id in enumerate(order)}
            candidate = _place_tasks(
                tasks,
                release,
                device_rules,
                max_continuous_human,
                human_break,
                recipe_rank=rank,
            )
            candidates.extend([
                candidate,
                _right_justify(candidate, device_rules, max_continuous_human, human_break),
            ])

    # 共享人工和设备会使实际总工期比单菜估计更长。用第一轮得到的工期再校准
    # 短菜启动时间，但始终让最长的基准菜从 0 开始，避免整个计划无意义后移。
    provisional = min(candidates, key=lambda item: _score_schedule(item, human_break))
    adjusted_target = max((item.end for item in provisional), default=target)
    longest_standalone = max(standalone.values(), default=0.0)
    adjusted_release = {
        recipe_id: (
            0.0
            if abs(standalone[recipe_id] - longest_standalone) <= EPSILON
            else max(0.0, adjusted_target - standalone[recipe_id])
        )
        for recipe_id in recipe_ids
    }
    for order in orders:
        rank = None if order is None else {recipe_id: index for index, recipe_id in enumerate(order)}
        candidate = _place_tasks(
            tasks,
            adjusted_release,
            device_rules,
            max_continuous_human,
            human_break,
            recipe_rank=rank,
        )
        candidates.extend([
            candidate,
            _right_justify(candidate, device_rules, max_continuous_human, human_break),
        ])

    # 单步放置时只看当前区间；“放入→运行→取出”组成的连续设备会话要在
    # 整张计划形成后才能完整检查。过滤掉无法分配具体层位/灶眼的候选，
    # 防止评分很好但实际不可执行的计划被选中。
    valid_candidates: list[list[ScheduledTask]] = []
    for candidate in candidates:
        try:
            validate_schedule(candidate, device_rules)
        except ValueError:
            continue
        valid_candidates.append(candidate)

    if not valid_candidates:
        # 极端情况下让各道菜按单菜工期错开，得到保守但一定可解释的兜底解。
        serial_release: dict[str, float] = {}
        cursor = 0.0
        for recipe_id in recipe_ids:
            serial_release[recipe_id] = cursor
            cursor += standalone[recipe_id] + TIME_STEP_MIN
        serial = _place_tasks(
            tasks,
            serial_release,
            device_rules,
            max_continuous_human,
            human_break,
        )
        validate_schedule(serial, device_rules)
        valid_candidates.append(serial)

    best = min(valid_candidates, key=lambda item: _score_schedule(item, human_break))
    return best, standalone


def repair_schedule_integrity(
    schedule: list[ScheduledTask],
    tasks: list[Task],
    device_rules: dict[str, DeviceRule],
    max_continuous_human: float,
    human_break: float,
) -> list[ScheduledTask]:
    """发现步骤缺失、重复或多余时，按原候选顺序重新生成完整安全排程。"""

    expected_keys = {task.key for task in tasks}
    actual_keys = [item.task.key for item in schedule]
    if len(actual_keys) == len(set(actual_keys)) and set(actual_keys) == expected_keys:
        return schedule

    # 保留原候选已经搜索出的相对优先级；缺失步骤排在其后，再由统一放置器
    # 重新满足依赖、人工及设备约束。这样不会把不完整结果直接交给接口。
    priority: dict[str, float] = {}
    for index, item in enumerate(sorted(
        schedule,
        key=lambda value: (value.start, value.task.recipe_name, value.task.step_no),
    )):
        if item.task.key in expected_keys and item.task.key not in priority:
            priority[item.task.key] = float(index)
    next_priority = float(len(priority))
    for task in tasks:
        if task.key not in priority:
            priority[task.key] = next_priority
            next_priority += 1.0

    recipe_ids = list(dict.fromkeys(task.recipe_id for task in tasks))
    serial_release: dict[str, float] = {}
    cursor = 0.0
    for recipe_id in recipe_ids:
        own_tasks = [task for task in tasks if task.recipe_id == recipe_id]
        own_schedule = _place_tasks(
            own_tasks,
            {recipe_id: 0.0},
            device_rules,
            max_continuous_human,
            human_break,
        )
        serial_release[recipe_id] = cursor
        cursor += max((item.end for item in own_schedule), default=0.0) + TIME_STEP_MIN
    repaired = _place_tasks(
        tasks,
        serial_release,
        device_rules,
        max_continuous_human,
        human_break,
        task_priority=priority,
    )
    validate_schedule(
        repaired,
        device_rules,
        expected_keys=expected_keys,
        max_continuous_human=max_continuous_human,
        human_break=human_break,
    )
    return repaired


def align_virtual_preheats(
    schedule: list[ScheduledTask],
    device_rules: dict[str, DeviceRule],
    max_continuous_human: float,
    human_break: float,
    locked_keys: set[str] | None = None,
) -> list[ScheduledTask]:
    """把虚拟预热紧贴其“放入设备”步骤，必要时顺延该步骤及其后继。"""

    locked_keys = locked_keys or set()
    current = list(schedule)
    tasks = [item.task for item in current]
    by_task_key = {task.key: task for task in tasks}
    successors: dict[str, set[str]] = {task.key: set() for task in tasks}
    for task in tasks:
        for dependency in task.dependencies:
            if dependency in successors:
                successors[dependency].add(task.key)

    def descendants(root: str) -> set[str]:
        result = {root}
        stack = [root]
        while stack:
            for key in successors.get(stack.pop(), ()):
                if key not in result:
                    result.add(key)
                    stack.append(key)
        return result

    preheat_keys = [
        task.key for task in tasks if task.original.get("virtual_preheat")
    ]
    for preheat_key in preheat_keys:
        by_key = {item.task.key: item for item in current}
        preheat_item = by_key[preheat_key]
        source_step_id = preheat_item.task.original.get("source_step_id")
        target_key = f"{preheat_item.task.recipe_id}:{source_step_id}"
        if target_key not in by_key:
            continue
        target_item = by_key[target_key]
        if abs(preheat_item.end - target_item.start) <= EPSILON:
            continue

        moving_keys = descendants(target_key)
        if preheat_key in locked_keys or moving_keys & locked_keys:
            continue

        # 优先只把预热右移到原放入时刻；若整机预热与别的菜冲突，则把
        # 放入及其全部后继按半分钟顺延，直到得到合法的连续组合。
        for step_index in range(4_000):
            delay = step_index * TIME_STEP_MIN
            target_start = target_item.start + delay
            preheat_start = target_start - preheat_item.task.duration
            if preheat_start < -EPSILON:
                continue
            candidate = [
                ScheduledTask(item.task, preheat_start)
                if item.task.key == preheat_key
                else ScheduledTask(item.task, item.start + delay)
                if item.task.key in moving_keys
                else item
                for item in current
            ]
            candidate.sort(
                key=lambda item: (item.start, item.task.recipe_name, item.task.step_no)
            )
            try:
                validate_schedule(candidate, device_rules)
            except ValueError:
                continue
            if (
                _maximum_continuous_human(candidate, human_break)
                <= max_continuous_human + EPSILON
            ):
                current = candidate
                break
    return current


def validate_preheat_contiguity(schedule: list[ScheduledTask]) -> None:
    """确保预热结束后立即放入设备，不输出已经失温的排程。"""

    by_key = {item.task.key: item for item in schedule}
    for item in schedule:
        if not item.task.original.get("virtual_preheat"):
            continue
        target_key = (
            f"{item.task.recipe_id}:"
            f"{item.task.original.get('source_step_id')}"
        )
        target = by_key.get(target_key)
        if target is None or abs(item.end - target.start) > EPSILON:
            raise ValueError(f"预热未紧接放入设备步骤：{item.task.recipe_name}")


def create_schedule(
    recipes: list[dict],
    device_rules: dict[str, DeviceRule],
    max_continuous_human: float = 30.0,
    human_break: float = 1.0,
    solver: str | None = None,
) -> tuple[list[ScheduledTask], dict[str, float]]:
    """使用指定的静态求解器生成排程。

    HTTP 请求和响应不需要增加 solver 字段。求解器通过内部参数或环境变量
    COOKING_SCHEDULER_SOLVER 选择：cp_sat、genetic 或 heuristic。
    默认使用遗传算法；命令行测试可以用 --solver 切换。
    """

    solver_name = (solver or os.getenv("COOKING_SCHEDULER_SOLVER", "genetic")).strip().lower()
    if solver_name == "heuristic":
        result = create_schedule_heuristic(
            recipes, device_rules, max_continuous_human, human_break
        )
    elif solver_name == "cp_sat":
        try:
            from .cp_sat_solver import solve_cp_sat_schedule
        except ImportError:
            from cp_sat_solver import solve_cp_sat_schedule
        result = solve_cp_sat_schedule(
            recipes, device_rules, max_continuous_human, human_break
        )
    elif solver_name == "genetic":
        try:
            from .genetic_solver import solve_genetic_schedule
        except ImportError:
            from genetic_solver import solve_genetic_schedule
        result = solve_genetic_schedule(
            recipes, device_rules, max_continuous_human, human_break
        )
    else:
        raise ValueError(
            f"未知静态求解器：{solver_name}；可选 cp_sat、genetic、heuristic"
        )

    schedule, standalone = result
    schedule = repair_schedule_integrity(
        schedule,
        build_tasks(recipes),
        device_rules,
        max_continuous_human,
        human_break,
    )
    schedule = align_virtual_preheats(
        schedule,
        device_rules,
        max_continuous_human,
        human_break,
    )
    validate_preheat_contiguity(schedule)
    return schedule, standalone


def validate_schedule(
    schedule: list[ScheduledTask],
    device_rules: dict[str, DeviceRule],
    *,
    expected_keys: set[str] | None = None,
    max_continuous_human: float | None = None,
    human_break: float = 1.0,
) -> None:
    """最终安全检查：完整性、依赖、唯一人工、设备及连续人工。"""

    by_key = {item.task.key: item for item in schedule}
    if len(by_key) != len(schedule):
        raise ValueError("排程中存在重复步骤")
    if expected_keys is not None and set(by_key) != expected_keys:
        missing = sorted(expected_keys - set(by_key))
        extra = sorted(set(by_key) - expected_keys)
        raise ValueError(f"排程步骤不完整或包含多余步骤：缺少={missing}，多余={extra}")
    for item in schedule:
        if not math.isfinite(item.start) or item.start < -EPSILON:
            raise ValueError(f"步骤开始时间无效：{item.task.key}")
        for dependency in item.task.dependencies:
            if dependency not in by_key or by_key[dependency].end > item.start + EPSILON:
                raise ValueError(f"前置依赖被违反：{item.task.key} <- {dependency}")

    human = [interval for item in schedule for interval in item.human_intervals]
    for index, first in enumerate(human):
        for second in human[index + 1:]:
            if _overlaps(first, second):
                raise ValueError("同一时间安排了多个人工步骤")
    if (
        max_continuous_human is not None
        and _maximum_continuous_human(schedule, human_break)
        > max_continuous_human + EPSILON
    ):
        raise ValueError("连续人工操作超过设定上限")

    # 设备不能只逐步骤检查。例如“放入烤箱”本身没有温度参数，但它和后面的
    # “180℃烘烤”属于同一次设备占用，应整体按 180℃ 判断是否能与其它菜并行。
    # 延迟导入可以避免 scheduler 与共用资源模块互相导入时形成循环。
    try:
        from .static_solver_common import assert_no_resource_conflicts
    except ImportError:
        from static_solver_common import assert_no_resource_conflicts
    assert_no_resource_conflicts(schedule, device_rules)


def _critical_path_lower_bound(tasks: list[Task]) -> float:
    remaining = _remaining_path_lengths(tasks)
    roots = [task for task in tasks if not task.dependencies]
    return max((remaining[task.key] for task in roots), default=0.0)


def calculate_metrics(
    recipes: list[dict],
    schedule: list[ScheduledTask],
    standalone: dict[str, float],
    device_rules: dict[str, DeviceRule],
    human_break: float,
) -> dict:
    tasks = build_tasks(recipes)
    bounds = _recipe_bounds(schedule)
    finishes = [end for _, end in bounds.values()]
    makespan = max(finishes, default=0.0)
    sequential = sum(standalone.values())
    time_saved = max(0.0, sequential - makespan)

    lower_bounds = [_critical_path_lower_bound(tasks)]
    lower_bounds.append(sum(task.human_duration for task in tasks))
    for device, rule in device_rules.items():
        if rule.background:
            continue
        workload = sum(
            task.device_duration * len(_instance_options(rule, task.instance_hints)[0])
            for task in tasks if task.device == device
        )
        workload += sum(
            usage.duration * len(_instance_options(rule, usage.instance_hints)[0])
            for task in tasks
            for usage in task.additional_device_usages
            if usage.device == device
        )
        lower_bounds.append(workload / rule.capacity)
    theoretical_lower_bound = max(lower_bounds, default=0.0)

    return {
        "makespanMin": _clean_number(makespan),
        "sequentialTimeMin": _clean_number(sequential),
        "timeSavedMin": _clean_number(time_saved),
        "compressionRate": round(time_saved / sequential, 4) if sequential else 0.0,
        "finishSpreadMin": _clean_number(
            max(finishes, default=0.0) - min(finishes, default=0.0)
        ),
        "maxContinuousHumanMin": _clean_number(
            _maximum_continuous_human(schedule, human_break)
        ),
        "theoreticalLowerBoundMin": _clean_number(theoretical_lower_bound),
        "optimalityGap": round(
            max(0.0, makespan - theoretical_lower_bound) / theoretical_lower_bound, 4
        ) if theoretical_lower_bound else 0.0,
    }


def _clean_number(value: float) -> int | float:
    rounded = round(value, 3)
    return int(rounded) if float(rounded).is_integer() else rounded


def _minute_text(value: float) -> str:
    return str(_clean_number(value))


def _clock_time(plan_start: datetime, minute_offset: float, round_up: bool = False) -> str:
    minutes = math.ceil(minute_offset - EPSILON) if round_up else math.floor(minute_offset + EPSILON)
    return (plan_start + timedelta(minutes=minutes)).strftime("%H:%M")


def _temperature_text(task: Task) -> str:
    if task.temperatures:
        return "/".join(f"{zone}{_minute_text(value)}℃" for zone, value in task.temperatures)
    if task.heat_level_raw:
        return task.heat_level_raw
    if task.heat_level is not None:
        labels = {1: "微火", 2: "小火", 3: "中小火", 4: "中火", 5: "中大火", 6: "大火", 7: "猛火"}
        return labels.get(task.heat_level, f"{task.heat_level}档火力")
    if task.device == "灶具":
        return "中火（估计）"
    return "按步骤要求"


def _has_cooking_parameters(task: Task) -> bool:
    """放入/取出设备只是人工操作，不伪造烹饪参数。"""

    return bool(
        task.device == "灶具"
        or task.mode
        or task.temperatures
        or task.heat_level_raw
        or task.heat_level is not None
    )


def _instruction_text(
    task: Task,
    step_type: int,
    resource_instances: tuple[str, ...] = (),
) -> str:
    """把内部动作转换成接口要求的自然语言执行指令。"""

    action = task.action.rstrip("。；; ")
    instance_text = ""
    if resource_instances:
        instance_text = f"；资源位置：{'、'.join(resource_instances)}"
    if step_type == 1:
        if action.startswith("准备"):
            return action + instance_text
        return f"准备：{action}，备用{instance_text}"
    if step_type == 2:
        return action + instance_text
    if step_type in (3, 4, 5) and _has_cooking_parameters(task):
        mode = task.mode or ("火力档位" if task.device == "灶具" else "工作模式")
        return (
            f"{action}；使用{task.device}，设置{mode}、{_temperature_text(task)}，"
            f"设备工作{_minute_text(task.device_duration)}分钟{instance_text}"
        )
    if step_type == 6 and not re.search(r"出锅|装盘", action):
        return f"出锅/装盘：{action}{instance_text}"
    return action + instance_text


def _step_type(task: Task) -> int:
    action = task.action
    if task.device == "灶具":
        return 3 if re.search(r"炖|煮|焯|煲|熬", action) else 4
    finish_action = bool(re.search(
        r"烹饪结束|取出(?:蒸好|烤好|烤制|成品)|打开(?:箱门|烤箱门).*取出|出锅|装盘|脱模|点缀|撒上",
        action,
    ))
    if task.device in ("蒸箱", "烤箱"):
        return 6 if finish_action else 5
    if finish_action:
        return 6
    if re.search(r"腌|浸泡|调汁|酱汁|拌匀", action):
        return 2
    return 1


def _step_title(task: Task, step_type: int) -> str:
    if step_type == 1:
        return "食材处理"
    if step_type == 2:
        return "腌制备料"
    if step_type == 3:
        return "炖煮"
    if step_type == 4:
        if "炸" in task.action:
            return "油炸"
        if "煎" in task.action:
            return "煎制"
        return "炒制"
    if step_type == 5:
        if task.original.get("virtual_preheat") or (
            "预热" in task.action
            and "无需预热" not in task.action
            and not IMPLICIT_PREHEATED_LOAD_RE.search(task.action)
        ):
            return "预热"
        return "蒸煮" if task.device == "蒸箱" else "烘烤"
    return "出锅装盘"


def _main_device(recipe_id: str, schedule: list[ScheduledTask]) -> str:
    totals: dict[str, float] = {}
    for item in schedule:
        if item.task.recipe_id == recipe_id and item.task.device:
            totals[item.task.device] = totals.get(item.task.device, 0.0) + item.task.device_duration
    return max(totals, key=totals.get) if totals else "人工"


def _ingredient_unit(ingredient: dict) -> str:
    return ingredient.get("amount_text") or "适量"


def _ingredients_summary(recipes: list[dict]) -> list[dict]:
    categories = ("荤菜", "素菜", "调味品")
    output = []
    for category in categories:
        grouped: dict[str, list[dict]] = {}
        for recipe in recipes:
            for ingredient in recipe["ingredients"]:
                if ingredient["category"] == category:
                    grouped.setdefault(ingredient["name"], []).append(ingredient)
        items = []
        for name, values in grouped.items():
            units = {item.get("unit") for item in values}
            quantities = [item.get("quantity") for item in values]
            if len(units) == 1 and None not in units and all(value is not None for value in quantities):
                unit = f"{_minute_text(sum(float(value) for value in quantities))}{next(iter(units))}"
            else:
                amount_parts = list(dict.fromkeys(_ingredient_unit(item) for item in values))
                unit = "、".join(amount_parts)
            items.append({"name": name, "unit": unit})
        output.append({"type": category, "list": items})
    return output


def build_api_response(
    recipes: list[dict],
    schedule: list[ScheduledTask],
    standalone: dict[str, float],
    plan_start: datetime,
) -> dict:
    bounds = _recipe_bounds(schedule)
    makespan = max((end for _, end in bounds.values()), default=0.0)
    sequential = sum(standalone.values())

    cooking_timeline = []
    recipe_detail = []
    for recipe in recipes:
        recipe_id = str(recipe["recipe_id"])
        start, end = bounds[recipe_id]
        cooking_timeline.append({
            "name": recipe["name"],
            "product": _main_device(recipe_id, schedule),
            "startTime": _clock_time(plan_start, start),
            "endTime": _clock_time(plan_start, end, round_up=True),
            "timeSpent": _minute_text(math.ceil(end - start - EPSILON)),
        })

        cooking_steps = []
        for item in sorted(
            (item for item in schedule if item.task.recipe_id == recipe_id),
            # 独立食材的准备步骤允许换序，因此接口必须反映排程后的真实执行顺序，
            # 不能继续照抄原菜谱编号。相同开始时间再用原编号保证输出稳定。
            key=lambda value: (value.start, value.task.step_no),
        ):
            if item.task.duration <= EPSILON:
                if item.task.original.get("advance_preparation"):
                    cooking_steps.append({
                        "describe": f"需提前完成：{item.task.action}",
                    })
                continue
            step = {"describe": item.task.action}
            if (
                item.task.device
                and item.task.device_duration > EPSILON
                and _has_cooking_parameters(item.task)
            ):
                step["cookingParameters"] = {
                    "mode": item.task.mode or item.task.device,
                    "temperature": _temperature_text(item.task),
                    "time": _minute_text(item.task.device_duration),
                }
            cooking_steps.append(step)
        recipe_detail.append({
            "name": recipe["name"],
            "majorIngredients": [
                {"name": item["name"], "unit": _ingredient_unit(item)}
                for item in recipe["ingredients"] if item["role"] == "主料"
            ],
            "minorIngredients": [
                {"name": item["name"], "unit": _ingredient_unit(item)}
                for item in recipe["ingredients"] if item["role"] == "辅料"
            ],
            "cookingSteps": cooking_steps,
        })

    details = []
    for item in schedule:
        task = item.task
        if task.duration <= EPSILON:
            continue
        step_type = _step_type(task)
        detail = {
            "timeInterval": f"{_minute_text(item.start)}-{_minute_text(item.end)}",
            "title": _step_title(task, step_type),
            "type": step_type,
            "list": [_instruction_text(task, step_type, item.resource_instances)],
        }
        # 设备相关步骤始终标明所属菜谱；即使只是放入设备、暂时没有
        # 模式和温度，也要满足接口对 recipeNames 的必填要求。
        if step_type in (3, 4, 5) and task.device:
            detail["recipeNames"] = [task.recipe_name]
        # 只有确实存在烹饪参数时才输出 parameters，避免为放入设备等
        # 纯人工动作伪造模式或温度。
        if step_type in (3, 4, 5) and task.device and _has_cooking_parameters(task):
            detail["parameters"] = [{
                "recipeName": task.recipe_name,
                "temperature": _temperature_text(task),
                "time": int(math.ceil(task.device_duration - EPSILON)),
            }]
        details.append(detail)
    details.sort(key=lambda item: (item["type"], float(item["timeInterval"].split("-", 1)[0])))

    return {
        "overview": {
            "finishTime": _clock_time(plan_start, makespan, round_up=True),
            "timeSpent": _minute_text(math.ceil(makespan - EPSILON)),
            "timeSave": _minute_text(max(0, math.floor(sequential - makespan + EPSILON))),
            "recipeCount": len(recipes),
        },
        "cookingTimeline": cooking_timeline,
        "ingredientsSummary": _ingredients_summary(recipes),
        "detailTimeline": details,
        "recipeDetail": recipe_detail,
    }


def schedule_from_request(
    request_items: list[dict],
    recipe_dir: Path,
    device_path: Path,
    plan_start: datetime | None = None,
    max_continuous_human: float = 30.0,
    solver: str | None = None,
) -> SchedulePlan:
    """从接口请求生成完整排程，是后续 Web API 需要调用的主函数。"""

    recipes = load_requested_recipes(request_items, recipe_dir)
    scheduling_recipes = prepare_recipes_for_schedule(recipes)
    device_rules = load_device_rules(device_path)
    schedule, standalone = create_schedule(
        scheduling_recipes,
        device_rules,
        max_continuous_human=max_continuous_human,
        solver=solver,
    )
    try:
        from .static_solver_common import assign_resource_instances
    except ImportError:
        from static_solver_common import assign_resource_instances
    schedule = assign_resource_instances(schedule, device_rules)
    validate_schedule(
        schedule,
        device_rules,
        expected_keys={task.key for task in build_tasks(scheduling_recipes)},
        max_continuous_human=max_continuous_human,
    )
    start = (plan_start or datetime.now()).replace(second=0, microsecond=0)
    response = build_api_response(recipes, schedule, standalone, start)
    metrics = calculate_metrics(
        scheduling_recipes, schedule, standalone, device_rules, human_break=1.0
    )
    return SchedulePlan(response=response, metrics=metrics, scheduled_tasks=schedule)
