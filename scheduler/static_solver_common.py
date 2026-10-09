"""静态求解器共用的资源建模工具。

这个文件只负责两件事：
1. 识别“放入设备 -> 烹饪 -> 取出”形成的连续设备占用；
2. 用同一套规则检查 CP-SAT 和遗传算法产生的结果。

把这部分独立出来，可以避免两个求解器各写一套略有差异的设备规则。
"""

from __future__ import annotations

from dataclasses import dataclass

try:
    from .scheduler import (
        DeviceRule,
        EPSILON,
        ScheduledTask,
        Task,
        _assign_calendar_instances,
        _instance_options,
        _overlaps,
    )
except ImportError:  # 允许直接从 scheduler 目录运行测试
    from scheduler import (
        DeviceRule,
        EPSILON,
        ScheduledTask,
        Task,
        _assign_calendar_instances,
        _instance_options,
        _overlaps,
    )


@dataclass(frozen=True)
class DeviceSession:
    """一道菜连续占用同一设备的一段过程。"""

    key: str
    device: str
    task_keys: tuple[str, ...]
    first_task_key: str
    last_task_key: str
    signature: tuple
    instance_hints: tuple[str, ...]


@dataclass(frozen=True)
class ResourceUse:
    """用于统一检查容量的设备占用区间。"""

    key: str
    device: str
    start: float
    end: float
    signature: tuple
    task_keys: tuple[str, ...]
    instance_hints: tuple[str, ...]


def foreground_sessions_are_contiguous(
    schedule: list[ScheduledTask],
    sessions: list[DeviceSession],
) -> bool:
    """检查灶具、蒸箱、烤箱等前台设备会话是否首尾相接。"""

    by_key = {item.task.key: item for item in schedule}
    for session in sessions:
        if session.device in ("冰箱", "烟机"):
            continue
        for previous_key, next_key in zip(
            session.task_keys, session.task_keys[1:]
        ):
            previous = by_key[previous_key]
            next_item = by_key[next_key]
            previous_device_end = (
                previous.start
                + previous.task.device_start
                + previous.task.device_duration
            )
            next_device_start = next_item.start + next_item.task.device_start
            if abs(previous_device_end - next_device_start) > EPSILON:
                return False
    return True


def infer_device_sessions(tasks: list[Task]) -> list[DeviceSession]:
    """识别同一菜谱中连续使用同一设备的步骤链。

    例如“放入烤箱(s8) -> 烘烤(s9) -> 取出(s10)”会成为一个 session。
    session 从 s8 真正放入设备的时刻开始，到 s10 取出结束为止。这样即使
    s9 的设备工作区间晚 1 分钟开始，烤箱中间也不会被误认为空闲。
    """

    grouped: dict[tuple[str, str], list[Task]] = {}
    for task in tasks:
        if task.device and task.device_duration > EPSILON:
            grouped.setdefault((task.recipe_id, task.device), []).append(task)

    sessions: list[DeviceSession] = []
    for (recipe_id, device), device_tasks in grouped.items():
        ordered = sorted(device_tasks, key=lambda item: item.step_no)
        chains: list[list[Task]] = []

        # 后台资源允许跨过中间的人工/制作步骤持续存在。例如食材放入冰箱后，
        # 期间可以继续处理其它食材，直到明确取出；烟机则持续到关闭。
        if device in ("冰箱", "烟机"):
            current_background: list[Task] = []
            close_words = ("取出", "拿出", "移出") if device == "冰箱" else ("关闭", "关掉")
            for task in ordered:
                current_background.append(task)
                if any(word in task.action for word in close_words):
                    chains.append(current_background)
                    current_background = []
            if current_background:
                chains.append(current_background)
        else:
            current: list[Task] = []
            for task in ordered:
                if not current:
                    current = [task]
                    continue
                previous = current[-1]
                is_next_step = task.step_no == previous.step_no + 1
                directly_depends = previous.key in task.dependencies
                # 预热任务只在预热期间整机独占。它结束后，正式烹饪步骤再按
                # 指定层位占用，不能把“整机独占”错误延长到整个烹饪会话。
                separates_whole_device_stage = (
                    previous.whole_device_exclusive
                    or task.whole_device_exclusive
                )
                if is_next_step and directly_depends and not separates_whole_device_stage:
                    current.append(task)
                else:
                    chains.append(current)
                    current = [task]
            if current:
                chains.append(current)

        for index, chain in enumerate(chains, start=1):
            # 单个设备步骤自身的 device_interval 已经足够，不必再包一层 session。
            if len(chain) < 2:
                continue
            non_empty_signatures = {
                task.device_signature
                for task in chain
                if any(value not in (None, (), "") for value in task.device_signature)
            }
            if len(non_empty_signatures) == 1:
                signature = next(iter(non_empty_signatures))
            elif not non_empty_signatures:
                signature = (None, (), None)
            else:
                # 同一道菜内部参数都不一致，不能与其它菜共用设备。
                signature = ("mixed", recipe_id, device, index)
            sessions.append(DeviceSession(
                key=f"session::{recipe_id}::{device}::{index}",
                device=device,
                task_keys=tuple(task.key for task in chain),
                first_task_key=chain[0].key,
                last_task_key=chain[-1].key,
                signature=signature,
                instance_hints=tuple(dict.fromkeys(
                    hint for task in chain for hint in task.instance_hints
                )),
            ))
    return sessions


def build_resource_uses(
    schedule: list[ScheduledTask],
    sessions: list[DeviceSession] | None = None,
) -> list[ResourceUse]:
    """把排程转换为真正的设备占用区间。"""

    by_key = {item.task.key: item for item in schedule}
    sessions = sessions if sessions is not None else infer_device_sessions(
        [item.task for item in schedule]
    )
    session_members = {key for session in sessions for key in session.task_keys}
    uses: list[ResourceUse] = []

    for item in schedule:
        interval = item.device_interval
        if (
            interval
            and item.task.device
            and item.task.key not in session_members
        ):
            uses.append(ResourceUse(
                key=item.task.key,
                device=item.task.device,
                start=interval[0],
                end=interval[1],
                signature=item.task.device_signature,
                task_keys=(item.task.key,),
                instance_hints=item.task.instance_hints,
            ))
        for usage_index, (usage, extra_interval) in enumerate(
            item.additional_device_intervals
        ):
            uses.append(ResourceUse(
                key=f"extra::{item.task.key}::{usage_index}",
                device=usage.device,
                start=extra_interval[0],
                end=extra_interval[1],
                signature=usage.signature,
                task_keys=(item.task.key,),
                instance_hints=usage.instance_hints,
            ))

    for session in sessions:
        first = by_key[session.first_task_key]
        last = by_key[session.last_task_key]
        start = first.start + first.task.device_start
        end = last.start + last.task.device_start + last.task.device_duration
        uses.append(ResourceUse(
            key=session.key,
            device=session.device,
            start=start,
            end=end,
            signature=session.signature,
            task_keys=session.task_keys,
            instance_hints=session.instance_hints,
        ))
    return uses


def assign_resource_instances(
    schedule: list[ScheduledTask],
    device_rules: dict[str, DeviceRule],
) -> list[ScheduledTask]:
    """在排程完成后，为每个设备会话确定具体灶眼、层位或后台分区。"""

    uses = build_resource_uses(schedule)
    by_device: dict[str, list[ResourceUse]] = {}
    for use in uses:
        by_device.setdefault(use.device, []).append(use)

    task_instances: dict[str, tuple[str, ...]] = {}
    for device, records in by_device.items():
        rule = device_rules.get(device, DeviceRule(instances=(device,)))
        assigned = _assign_calendar_instances(
            [((item.start, item.end), item.instance_hints) for item in records],
            rule,
        )
        if assigned is None:
            raise ValueError(f"{device} 无法分配到具体实例或层位")
        for use, instances in zip(records, assigned):
            if use.key.startswith("extra::"):
                continue
            for key in use.task_keys:
                task_instances[key] = instances

    return [
        ScheduledTask(
            task=item.task,
            start=item.start,
            resource_instances=task_instances.get(item.task.key, ()),
        )
        for item in schedule
    ]


def resource_conflicts(
    schedule: list[ScheduledTask],
    device_rules: dict[str, DeviceRule],
    sessions: list[DeviceSession] | None = None,
) -> list[str]:
    """返回所有连续设备占用冲突；空列表表示设备安排合法。"""

    uses = build_resource_uses(schedule, sessions)
    by_device: dict[str, list[ResourceUse]] = {}
    for use in uses:
        by_device.setdefault(use.device, []).append(use)

    conflicts: list[str] = []
    for device, records in by_device.items():
        rule = device_rules.get(device, DeviceRule())
        if rule.background:
            continue
        assigned = _assign_calendar_instances(
            [((item.start, item.end), item.instance_hints) for item in records],
            rule,
        )
        if assigned is None:
            conflicts.append(f"{device} 的任务无法分配到具体实例或层位")
        points = sorted({point for item in records for point in (item.start, item.end)})
        for left, right in zip(points, points[1:]):
            if right - left <= EPSILON:
                continue
            middle = (left + right) / 2
            active = [item for item in records if item.start < middle < item.end]
            occupied_units = sum(
                len(_instance_options(rule, item.instance_hints)[0])
                for item in active
            )
            if occupied_units > rule.capacity:
                conflicts.append(
                    f"{device} 在 {left:g}-{right:g} 分钟占用 {occupied_units} 份，容量为 {rule.capacity}"
                )
            if rule.same_setting_required and len({item.signature for item in active}) > 1:
                conflicts.append(
                    f"{device} 在 {left:g}-{right:g} 分钟存在不同模式、温度或湿度"
                )
    return conflicts


def assert_no_resource_conflicts(
    schedule: list[ScheduledTask],
    device_rules: dict[str, DeviceRule],
) -> None:
    """发现连续设备占用冲突时立即报出第一条易读错误。"""

    conflicts = resource_conflicts(schedule, device_rules)
    if conflicts:
        raise ValueError(f"连续设备占用冲突：{conflicts[0]}")
