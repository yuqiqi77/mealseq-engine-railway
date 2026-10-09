"""根据排程生成简洁、可操作的中文通知。"""

from __future__ import annotations

import math

from scheduler import EPSILON, ScheduledTask


URGENT_DEVICES = {"灶具", "烤箱", "蒸箱", "洗碗机", "净咖机"}
URGENT_ACTION_WORDS = (
    "关火", "离火", "翻面", "按屏幕", "开始烹饪", "打开箱门", "关闭箱门",
)
DEVICE_CONTEXT_WORDS = ("灶", "烤箱", "蒸箱", "机器", "设备")


def _has_urgent_action(action: str) -> bool:
    """兜底识别未标设备但显然需要准点处理的操作。"""

    if any(word in action for word in URGENT_ACTION_WORDS):
        return True
    return "取出" in action and any(word in action for word in DEVICE_CONTEXT_WORDS)


def _minute_text(value: float) -> str:
    rounded = round(value, 1)
    return str(int(rounded)) if rounded.is_integer() else str(rounded)


def advance_preparation_events(schedule: list[ScheduledTask]) -> list[dict]:
    """返回已在正式排程前完成的准备事项及其首个使用时刻。"""

    by_key = {item.task.key: item for item in schedule}
    ancestor_cache: dict[str, set[str]] = {}

    def ancestors(key: str) -> set[str]:
        if key in ancestor_cache:
            return ancestor_cache[key]
        result: set[str] = set()
        for dependency in by_key[key].task.dependencies:
            result.add(dependency)
            if dependency in by_key:
                result.update(ancestors(dependency))
        ancestor_cache[key] = result
        return result

    grouped: dict[tuple[str, float], dict] = {}
    positive_items = [item for item in schedule if item.task.duration > EPSILON]
    for item in schedule:
        if not item.task.original.get("advance_preparation"):
            continue
        dependent_starts = [
            candidate.start
            for candidate in positive_items
            if (
                candidate.task.recipe_id == item.task.recipe_id
                and item.task.key in ancestors(candidate.task.key)
            )
        ]
        same_recipe_starts = [
            candidate.start
            for candidate in positive_items
            if candidate.task.recipe_id == item.task.recipe_id
        ]
        target = min(dependent_starts or same_recipe_starts or [0.0])
        key = (item.task.recipe_id, target)
        event = grouped.setdefault(key, {
            "recipeId": item.task.recipe_id,
            "recipeName": item.task.recipe_name,
            "notifyAtMin": target,
            "stepIds": [],
            "actions": [],
        })
        event["stepIds"].append(item.task.step_id)
        event["actions"].append(item.task.action)
    return sorted(
        grouped.values(),
        key=lambda item: (item["notifyAtMin"], item["recipeName"]),
    )


def build_schedule_changes(
    old_starts: dict[str, float],
    new_schedule: list[ScheduledTask],
    elapsed_min: float,
) -> list[dict]:
    """列出尚未开始步骤的时间变化，已执行的历史不会出现在这里。"""

    changes = []
    for item in new_schedule:
        old_start = old_starts.get(item.task.key)
        if old_start is None or old_start < elapsed_min - EPSILON:
            continue
        if abs(old_start - item.start) < 0.5 - EPSILON:
            continue
        changes.append({
            "recipeName": item.task.recipe_name,
            "stepId": item.task.step_id,
            "oldStartMin": _minute_text(old_start),
            "newStartMin": _minute_text(item.start),
            "delayMin": _minute_text(item.start - old_start),
            "reason": "临时增加菜谱后重新分配人工和设备资源",
        })
    return sorted(changes, key=lambda item: float(item["newStartMin"]))


def build_notifications(
    schedule: list[ScheduledTask],
    elapsed_min: float,
    reminder_before_min: float = 3.0,
) -> list[dict]:
    """为全部紧急设备操作生成人工提醒，不限制每道菜的节点数量。

    灶具、烤箱、蒸箱等前台设备上的人工介入均属于时效性操作；另外，
    即使菜谱漏标设备，关火、取出、翻面等关键词也会兜底识别。普通切配、
    腌制和无须准点处理的准备步骤不通知，避免弹窗淹没真正紧急的节点。
    """

    urgent_segments: list[tuple[ScheduledTask, int, float, float, str]] = []
    for item in sorted(schedule, key=lambda value: value.start):
        task = item.task
        devices = {
            device
            for device in (
                task.device,
                *(usage.device for usage in task.additional_device_usages),
            )
            if device
        }
        urgent_device = next(
            (device for device in devices if device in URGENT_DEVICES),
            None,
        )
        keyword_urgent = _has_urgent_action(task.action)
        if urgent_device is None and not keyword_urgent:
            continue
        attention = urgent_device or "关键操作"
        for segment_index, (human_start, human_end) in enumerate(
            item.human_intervals, start=1
        ):
            if human_start < elapsed_min - EPSILON:
                continue
            urgent_segments.append(
                (item, segment_index, human_start, human_end, attention)
            )

    notifications: list[dict] = []
    for event in advance_preparation_events(schedule):
        target = float(event["notifyAtMin"])
        notify_at = max(elapsed_min, target - reminder_before_min)
        if target < elapsed_min - EPSILON:
            continue
        notifications.append({
            "notifyAtMin": _minute_text(notify_at),
            "type": "PREPARATION",
            "operationType": "前置准备",
            "recipeName": event["recipeName"],
            "stepId": "→".join(event["stepIds"]),
            "message": (
                f"{event['recipeName']}即将开始正式处理，请确认已提前完成："
                f"{'；'.join(event['actions'])}。"
            ),
        })

    for item, segment_index, human_start, human_end, attention in sorted(
        urgent_segments,
        key=lambda value: (value[2], value[0].task.recipe_name),
    ):
        task = item.task
        if human_start - elapsed_min >= reminder_before_min - EPSILON:
            reminder_at = max(elapsed_min, human_start - reminder_before_min)
            minutes_until = human_start - reminder_at
            notifications.append({
                "notifyAtMin": _minute_text(reminder_at),
                "type": "REMINDER",
                "operationType": f"{attention}操作",
                "recipeName": task.recipe_name,
                "stepId": task.step_id,
                "message": (
                    f"{_minute_text(minutes_until)}分钟后需要操作{attention}："
                    f"{task.action}。"
                ),
            })
        notifications.append({
            "notifyAtMin": _minute_text(human_start),
            "type": "ACTION",
            "operationType": f"{attention}操作",
            "recipeName": task.recipe_name,
            "stepId": task.step_id,
            "message": (
                f"现在需要操作{attention}：{task.action}"
                f"（第{segment_index}段人工操作），"
                f"预计用时{_minute_text(human_end - human_start)}分钟。"
            ),
        })

    unique: dict[tuple, dict] = {}
    for item in notifications:
        key = (
            item["notifyAtMin"], item["type"], item["recipeName"],
            item["stepId"], item["message"],
        )
        unique[key] = item
    return sorted(
        unique.values(),
        key=lambda item: (float(item["notifyAtMin"]), item["recipeName"], item["stepId"]),
    )


def current_notifications(notifications: list[dict], elapsed_min: float) -> list[dict]:
    """取出当前半分钟窗口内应该发送的通知。"""

    return [
        item for item in notifications
        if math.isclose(float(item["notifyAtMin"]), elapsed_min, abs_tol=0.5)
    ]
