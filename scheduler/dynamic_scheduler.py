"""执行过程中的增量重排。

核心原则：
- 已完成、正在执行和马上要开始的步骤锁定；
- 其余旧步骤与新增菜从当前时刻共同重排，可前移到空档也可后移；
- 全部候选继续满足依赖、唯一人工、设备容量与连续会话约束。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from scheduler import (
    EPSILON,
    TIME_STEP_MIN,
    DeviceRule,
    ScheduledTask,
    Task,
    _append_device_calendar_item,
    _find_earliest_start,
    _maximum_continuous_human,
    _place_tasks,
    _recipe_bounds,
    _remaining_path_lengths,
    _score_schedule,
    _is_generic_preparation_task,
    align_virtual_preheats,
    build_tasks,
    validate_schedule,
    validate_preheat_contiguity,
)
from static_solver_common import (
    infer_device_sessions,
)

DYNAMIC_OPTIMIZATION_TIME_LIMIT_SECONDS = 2.0


def _deadline_reached(deadline: float | None) -> bool:
    return deadline is not None and time.perf_counter() >= deadline


@dataclass
class DynamicSchedule:
    scheduled_tasks: list[ScheduledTask]
    standalone_durations: dict[str, float]
    locked_keys: set[str]
    completed_keys: set[str]
    running_keys: set[str]


def rebuild_old_schedule(tasks: list[Task], old_starts: dict[str, float]) -> list[ScheduledTask]:
    """使用当前菜谱数据和状态文件中的开始时间恢复旧排程。"""

    by_key = {task.key: task for task in tasks}
    missing = set(by_key) - set(old_starts)
    extra = set(old_starts) - set(by_key)
    if missing or extra:
        raise ValueError(
            f"活动排程步骤与菜谱不一致：缺少={sorted(missing)}，多余={sorted(extra)}"
        )
    return [
        ScheduledTask(task=by_key[key], start=start)
        for key, start in old_starts.items()
    ]


def _ancestor_closure(initial: set[str], task_by_key: dict[str, Task]) -> set[str]:
    """锁定步骤时同时锁定其全部前置步骤，避免依赖被移到它后面。"""

    locked = set(initial)
    stack = list(initial)
    while stack:
        key = stack.pop()
        for dependency in task_by_key[key].dependencies:
            if dependency not in locked:
                locked.add(dependency)
                stack.append(dependency)
    return locked


def _standalone_durations(
    recipes: list[dict],
    device_rules: dict[str, DeviceRule],
    max_continuous_human: float,
    human_break: float,
) -> dict[str, float]:
    tasks = build_tasks(recipes)
    result: dict[str, float] = {}
    for recipe in recipes:
        recipe_id = str(recipe["recipe_id"])
        own_tasks = [task for task in tasks if task.recipe_id == recipe_id]
        own_schedule = _place_tasks(
            own_tasks,
            {recipe_id: 0.0},
            device_rules,
            max_continuous_human,
            human_break,
        )
        result[recipe_id] = max((item.end for item in own_schedule), default=0.0)
    return result


def _dynamic_schedule_is_legal(
    schedule: list[ScheduledTask],
    device_rules: dict[str, DeviceRule],
    max_continuous_human: float,
    human_break: float,
) -> bool:
    """检查动态右移候选是否仍满足全部硬约束。

    同一设备会话的可见步骤之间允许存在空隙；资源检查会把“放入—运行—
    取出”的整段会话视为持续占用，因此空隙内设备仍不会分配给冲突任务。
    甘特图只画实际步骤，不把这段逻辑占用画成长色块。
    """

    try:
        validate_schedule(schedule, device_rules)
    except ValueError:
        return False
    return (
        _maximum_continuous_human(schedule, human_break)
        <= max_continuous_human + EPSILON
    )


def _shift_unlocked_recipe_tasks(
    schedule: list[ScheduledTask],
    recipe_id: str,
    locked_keys: set[str],
    delay: float,
) -> list[ScheduledTask]:
    """把一道菜尚未锁定的步骤整体后移，锁定步骤保持原位。"""

    shifted = [
        ScheduledTask(task=item.task, start=item.start + delay)
        if item.task.recipe_id == recipe_id and item.task.key not in locked_keys
        else item
        for item in schedule
    ]
    return sorted(
        shifted,
        key=lambda item: (item.start, item.task.recipe_name, item.task.step_no),
    )


def _shift_task_group(
    schedule: list[ScheduledTask],
    task_keys: tuple[str, ...],
    delay: float,
) -> list[ScheduledTask]:
    """把一个连续设备会话整体后移。"""

    members = set(task_keys)
    shifted = [
        ScheduledTask(task=item.task, start=item.start + delay)
        if item.task.key in members
        else item
        for item in schedule
    ]
    return sorted(
        shifted,
        key=lambda item: (item.start, item.task.recipe_name, item.task.step_no),
    )


def _reschedule_recipe_from_release(
    schedule: list[ScheduledTask],
    recipe_id: str,
    release_min: float,
    locked_keys: set[str],
    device_rules: dict[str, DeviceRule],
    max_continuous_human: float,
    human_break: float,
) -> list[ScheduledTask] | None:
    """固定其它菜及本菜锁定步骤，把其余步骤从较晚时刻重新插入空档。"""

    recipe_items = [item for item in schedule if item.task.recipe_id == recipe_id]
    moving_tasks = [
        item.task for item in recipe_items if item.task.key not in locked_keys
    ]
    if not moving_tasks:
        return None

    fixed = [
        item
        for item in schedule
        if item.task.recipe_id != recipe_id or item.task.key in locked_keys
    ]
    scheduled = {item.task.key: item for item in fixed}
    unscheduled = {task.key: task for task in moving_tasks}
    # 剩余路径必须按本菜全部步骤计算，因为未锁定步骤可能依赖一个已完成的
    # 零时长前置准备标记。
    remaining_path = _remaining_path_lengths([item.task for item in recipe_items])
    human_calendar = [
        interval for item in fixed for interval in item.human_intervals
    ]
    device_calendar: dict[
        str,
        list[tuple[tuple[float, float], tuple, tuple[str, ...]]],
    ] = {}
    for item in fixed:
        _append_device_calendar_item(item, device_calendar)

    while unscheduled:
        ready = [
            task
            for task in unscheduled.values()
            if all(dependency in scheduled for dependency in task.dependencies)
        ]
        if not ready:
            return None
        ready.sort(key=lambda task: (-remaining_path[task.key], task.step_no))
        task = ready[0]
        dependency_end = max(
            (scheduled[key].end for key in task.dependencies),
            default=release_min,
        )
        start = _find_earliest_start(
            task,
            max(release_min, dependency_end),
            human_calendar,
            device_calendar,
            device_rules,
            max_continuous_human,
            human_break,
        )
        item = ScheduledTask(task=task, start=start)
        scheduled[task.key] = item
        del unscheduled[task.key]
        human_calendar.extend(item.human_intervals)
        _append_device_calendar_item(item, device_calendar)

    candidate = sorted(
        scheduled.values(),
        key=lambda item: (item.start, item.task.recipe_name, item.task.step_no),
    )
    if not _dynamic_schedule_is_legal(
        candidate,
        device_rules,
        max_continuous_human,
        human_break,
    ):
        return None
    return candidate


def _reinsert_movable_recipes_late(
    schedule: list[ScheduledTask],
    recipe_ids: set[str],
    locked_keys: set[str],
    device_rules: dict[str, DeviceRule],
    max_continuous_human: float,
    human_break: float,
    deadline: float | None,
) -> list[ScheduledTask]:
    """把尚可移动的菜重新排入后段空档，缩小动态加菜后的出锅差。"""

    current = list(schedule)
    initial_bounds = _recipe_bounds(current)
    recipe_order = sorted(
        recipe_ids,
        key=lambda recipe_id: initial_bounds.get(recipe_id, (0.0, 0.0))[1],
    )
    for recipe_id in recipe_order:
        if _deadline_reached(deadline):
            break
        bounds = _recipe_bounds(current)
        if recipe_id not in bounds:
            continue
        recipe_start, recipe_finish = bounds[recipe_id]
        target_finish = max((finish for _, finish in bounds.values()), default=0.0)
        if target_finish - recipe_finish <= EPSILON:
            continue
        recipe_span = recipe_finish - recipe_start
        desired_release = max(recipe_start, target_finish - recipe_span)
        release = int(desired_release / TIME_STEP_MIN) * TIME_STEP_MIN
        best = current
        best_score = _score_schedule(current, human_break)
        while release > recipe_start + EPSILON:
            if _deadline_reached(deadline):
                break
            candidate = _reschedule_recipe_from_release(
                current,
                recipe_id,
                release,
                locked_keys,
                device_rules,
                max_continuous_human,
                human_break,
            )
            if candidate is not None:
                candidate_score = _score_schedule(candidate, human_break)
                if candidate_score < best_score:
                    best = candidate
                    best_score = candidate_score
                    # 从最晚目标向前搜索，首个改进通常就是最接近同出锅的候选。
                    break
            release -= TIME_STEP_MIN
        current = best
    return current


def _recipes_safe_for_whole_late_reinsertion(
    tasks: list[Task],
    old_recipe_ids: set[str],
) -> set[str]:
    """筛出适合整道后移的菜。

    新菜若在最终步骤前需要另一类设备先完成长阶段（例如先低温烤箱、
    最后灶具煎制），应保留前段尽早执行，只右移最终出锅步骤。若最终
    步骤与最近的设备前置步骤属于同一设备，则仍可把连续会话整体后移。
    旧菜保持原有重插策略，用于利用尚未执行的后段空档。
    """

    by_key = {task.key: task for task in tasks}
    dependent_keys = {
        dependency
        for task in tasks
        for dependency in task.dependencies
    }
    result = set(old_recipe_ids)

    def nearest_ancestor_devices(task: Task) -> set[str]:
        devices: set[str] = set()
        stack = list(task.dependencies)
        visited: set[str] = set()
        while stack:
            key = stack.pop()
            if key in visited or key not in by_key:
                continue
            visited.add(key)
            ancestor = by_key[key]
            if ancestor.device:
                devices.add(ancestor.device)
            else:
                stack.extend(ancestor.dependencies)
        return devices

    new_recipe_ids = {task.recipe_id for task in tasks} - old_recipe_ids
    for recipe_id in new_recipe_ids:
        final_tasks = [
            task
            for task in tasks
            if task.recipe_id == recipe_id and task.key not in dependent_keys
        ]
        preserve_early_phase = any(
            any(device != final_task.device for device in nearest_ancestor_devices(final_task))
            for final_task in final_tasks
        )
        if not preserve_early_phase:
            result.add(recipe_id)
    return result


def _right_align_dynamic_sessions(
    schedule: list[ScheduledTask],
    locked_keys: set[str],
    device_rules: dict[str, DeviceRule],
    max_continuous_human: float,
    human_break: float,
    deadline: float | None = None,
) -> list[ScheduledTask]:
    """把未锁定的连续设备会话整体后移，进一步靠拢出锅时间。"""

    current = list(schedule)
    target_finish = max((item.end for item in current), default=0.0)
    sessions = infer_device_sessions([item.task for item in current])
    initial_bounds = _recipe_bounds(current)
    session_order = sorted(
        sessions,
        key=lambda session: initial_bounds[
            next(
                item.task.recipe_id
                for item in current
                if item.task.key == session.first_task_key
            )
        ][1],
    )

    for session in session_order:
        if _deadline_reached(deadline):
            break
        if session.device in ("冰箱", "烟机"):
            continue
        if any(key in locked_keys for key in session.task_keys):
            continue
        by_key = {item.task.key: item for item in current}
        recipe_id = by_key[session.first_task_key].task.recipe_id
        recipe_finish = _recipe_bounds(current)[recipe_id][1]
        # 只移动包含本菜最终完成步骤的会话；移动中间会话不会改善出锅差。
        session_finish = max(by_key[key].end for key in session.task_keys)
        if abs(session_finish - recipe_finish) > EPSILON:
            continue
        desired_delay = max(0.0, target_finish - recipe_finish)
        # 从“刚好与最晚菜同时完成”向前搜索，找到最晚的合法空档后立即采用。
        # 旧实现从 0.5 分钟一路向后枚举到目标之后，常把 2 秒预算耗在
        # 大量不会成为最优解的候选上，导致后面的菜没有机会右移。
        delay = int(desired_delay / TIME_STEP_MIN) * TIME_STEP_MIN
        old_score = _score_schedule(current, human_break)
        while delay > EPSILON:
            if _deadline_reached(deadline):
                break
            candidate = _shift_task_group(
                current,
                session.task_keys,
                delay,
            )
            if _dynamic_schedule_is_legal(
                candidate,
                device_rules,
                max_continuous_human,
                human_break,
            ):
                candidate_score = _score_schedule(candidate, human_break)
                candidate_finish = _recipe_bounds(candidate)[recipe_id][1]
                if (
                    candidate_score < old_score
                    or (
                        candidate_score == old_score
                        and candidate_finish > recipe_finish + EPSILON
                    )
                ):
                    current = candidate
                    break
            delay -= TIME_STEP_MIN
    return current


def _right_align_dynamic_final_steps(
    schedule: list[ScheduledTask],
    locked_keys: set[str],
    device_rules: dict[str, DeviceRule],
    max_continuous_human: float,
    human_break: float,
    deadline: float | None = None,
) -> list[ScheduledTask]:
    """右移不属于连续会话的最终步骤，填补会话级优化之外的空隙。"""

    current = list(schedule)
    target_finish = max((item.end for item in current), default=0.0)
    sessions = infer_device_sessions([item.task for item in current])
    session_members = {
        key
        for session in sessions
        for key in session.task_keys
    }
    bounds = _recipe_bounds(current)
    recipe_order = sorted(bounds, key=lambda recipe_id: bounds[recipe_id][1])

    for recipe_id in recipe_order:
        if _deadline_reached(deadline):
            break
        by_key = {item.task.key: item for item in current}
        recipe_finish = _recipe_bounds(current)[recipe_id][1]
        final_tasks = [
            item
            for item in current
            if item.task.recipe_id == recipe_id
            and abs(item.end - recipe_finish) <= EPSILON
            and item.task.key not in locked_keys
            and item.task.key not in session_members
            and not _is_generic_preparation_task(item.task)
        ]
        for item in final_tasks:
            if _deadline_reached(deadline):
                break
            maximum_delay = max(0.0, target_finish - recipe_finish)
            delay = int(maximum_delay / TIME_STEP_MIN) * TIME_STEP_MIN
            while delay > EPSILON:
                if _deadline_reached(deadline):
                    break
                candidate = _shift_task_group(
                    current,
                    (item.task.key,),
                    delay,
                )
                if _dynamic_schedule_is_legal(
                    candidate,
                    device_rules,
                    max_continuous_human,
                    human_break,
                ):
                    old_score = _score_schedule(current, human_break)
                    candidate_score = _score_schedule(candidate, human_break)
                    candidate_finish = _recipe_bounds(candidate)[recipe_id][1]
                    if (
                        candidate_score < old_score
                        or (
                            candidate_score == old_score
                            and candidate_finish > recipe_finish + EPSILON
                        )
                    ):
                        current = candidate
                    break
                delay -= TIME_STEP_MIN
    return current


def _right_align_dynamic_schedule(
    schedule: list[ScheduledTask],
    locked_keys: set[str],
    device_rules: dict[str, DeviceRule],
    max_continuous_human: float,
    human_break: float,
    deadline: float | None = None,
) -> list[ScheduledTask]:
    """保持锁定步骤不动，将其余菜尽量右移到共同出锅时刻。"""

    current = list(schedule)
    target_finish = max((item.end for item in current), default=0.0)
    initial_bounds = _recipe_bounds(current)
    recipe_order = sorted(
        initial_bounds,
        key=lambda recipe_id: initial_bounds[recipe_id][1],
    )

    for recipe_id in recipe_order:
        if _deadline_reached(deadline):
            break
        movable = [
            item
            for item in current
            if item.task.recipe_id == recipe_id
            and item.task.key not in locked_keys
        ]
        if not movable:
            continue
        recipe_finish = _recipe_bounds(current)[recipe_id][1]
        delay = int(
            (target_finish - recipe_finish) / TIME_STEP_MIN
        ) * TIME_STEP_MIN
        while delay > EPSILON:
            if _deadline_reached(deadline):
                break
            candidate = _shift_unlocked_recipe_tasks(
                current,
                recipe_id,
                locked_keys,
                delay,
            )
            if _dynamic_schedule_is_legal(
                candidate,
                device_rules,
                max_continuous_human,
                human_break,
            ):
                if (
                    _score_schedule(candidate, human_break)
                    < _score_schedule(current, human_break)
                ):
                    current = candidate
                break
            delay -= TIME_STEP_MIN
    return current


def _build_global_reoptimization_candidate(
    recipes: list[dict],
    locked_schedule: list[ScheduledTask],
    locked_keys: set[str],
    elapsed_min: float,
    device_rules: dict[str, DeviceRule],
    max_continuous_human: float,
    human_break: float,
    time_limit_seconds: float,
) -> list[ScheduledTask] | None:
    """在全局求解中直接固定已执行步骤，让剩余步骤共同插空。"""

    if time_limit_seconds < 0.1:
        return None
    try:
        try:
            from .cp_sat_solver import solve_cp_sat_schedule
        except ImportError:
            from cp_sat_solver import solve_cp_sat_schedule
        global_schedule, _ = solve_cp_sat_schedule(
            recipes,
            device_rules,
            max_continuous_human,
            human_break,
            time_limit_seconds=time_limit_seconds,
            fixed_task_starts={
                item.task.key: item.start for item in locked_schedule
            },
            unfixed_start_floor_min=elapsed_min,
        )
    except (ImportError, RuntimeError, ValueError):
        return None

    if not any(item.task.key not in locked_keys for item in global_schedule):
        return None
    return global_schedule if _dynamic_schedule_is_legal(
        global_schedule,
        device_rules,
        max_continuous_human,
        human_break,
    ) else None


def reschedule_after_addition(
    recipes: list[dict],
    old_starts: dict[str, float],
    old_recipe_ids: set[str],
    elapsed_min: float,
    device_rules: dict[str, DeviceRule],
    freeze_window_min: float = 2.0,
    max_continuous_human: float = 30.0,
    human_break: float = 1.0,
) -> DynamicSchedule:
    """在旧活动排程中加入新菜谱，只重新放置还未锁定的步骤。"""

    optimization_deadline = (
        time.perf_counter() + DYNAMIC_OPTIMIZATION_TIME_LIMIT_SECONDS
    )

    tasks = build_tasks(recipes)
    task_by_key = {task.key: task for task in tasks}
    old_tasks = [task for task in tasks if task.recipe_id in old_recipe_ids]
    old_schedule = rebuild_old_schedule(old_tasks, old_starts)

    completed = {item.task.key for item in old_schedule if item.end <= elapsed_min}
    running = {
        item.task.key for item in old_schedule
        if item.start <= elapsed_min < item.end
    }
    imminent = {
        item.task.key for item in old_schedule
        if elapsed_min < item.start <= elapsed_min + freeze_window_min
    }
    locked_keys = _ancestor_closure(completed | running | imminent, task_by_key)
    locked_schedule = [item for item in old_schedule if item.task.key in locked_keys]

    remaining_path = _remaining_path_lengths(tasks)

    def build_greedy_candidate(relax_old_starts: bool) -> list[ScheduledTask]:
        """重建未锁定步骤；优先尝试完全插空，失败时保留旧计划时序。"""

        scheduled = {item.task.key: item for item in locked_schedule}
        unscheduled = {
            task.key: task for task in tasks
            if task.key not in locked_keys
        }
        human_calendar = [
            interval
            for item in locked_schedule
            for interval in item.human_intervals
        ]
        device_calendar: dict[
            str,
            list[tuple[tuple[float, float], tuple, tuple[str, ...]]],
        ] = {}
        for item in locked_schedule:
            _append_device_calendar_item(item, device_calendar)

        while unscheduled:
            ready = [
                task for task in unscheduled.values()
                if all(dependency in scheduled for dependency in task.dependencies)
            ]
            if not ready:
                raise ValueError("动态重排时发现循环依赖或未满足的前置步骤")

            def preferred_start(task: Task) -> float:
                if relax_old_starts:
                    return elapsed_min
                return max(elapsed_min, old_starts.get(task.key, elapsed_min))

            ready.sort(key=lambda task: (
                preferred_start(task),
                -remaining_path[task.key],
                task.recipe_name,
                task.step_no,
            ))
            task = ready[0]
            dependency_end = max(
                (scheduled[key].end for key in task.dependencies),
                default=elapsed_min,
            )
            start = _find_earliest_start(
                task,
                max(elapsed_min, dependency_end, preferred_start(task)),
                human_calendar,
                device_calendar,
                device_rules,
                max_continuous_human,
                human_break,
            )
            item = ScheduledTask(task=task, start=start)
            scheduled[task.key] = item
            del unscheduled[task.key]
            human_calendar.extend(item.human_intervals)
            _append_device_calendar_item(item, device_calendar)

        return sorted(
            scheduled.values(),
            key=lambda item: (item.start, item.task.recipe_name, item.task.step_no),
        )

    # 先让新增菜和全部未开始步骤共同插空。若这种顺序导致连续设备会话
    # 参数冲突，则退回保留旧开始时间的稳定候选，绝不输出非法排程。
    insertion_candidate = build_greedy_candidate(relax_old_starts=True)
    stable_candidate = build_greedy_candidate(relax_old_starts=False)
    legal_candidates = [
        candidate
        for candidate in (insertion_candidate, stable_candidate)
        if _dynamic_schedule_is_legal(
            candidate,
            device_rules,
            max_continuous_human,
            human_break,
        )
    ]
    if not legal_candidates:
        raise RuntimeError("动态重排未能生成满足人工和设备约束的候选")
    remaining_for_global = optimization_deadline - time.perf_counter() - 0.2
    global_candidate = _build_global_reoptimization_candidate(
        recipes,
        locked_schedule,
        locked_keys,
        elapsed_min,
        device_rules,
        max_continuous_human,
        human_break,
        min(1.0, max(0.0, remaining_for_global)),
    )
    if global_candidate is not None:
        legal_candidates.append(global_candidate)
    shortest_makespan = min(
        _score_schedule(candidate, human_break)[1]
        for candidate in legal_candidates
    )
    duration_safe_candidates = [
        candidate
        for candidate in legal_candidates
        if _score_schedule(candidate, human_break)[1] <= shortest_makespan + 10
    ]
    final_schedule = min(
        duration_safe_candidates,
        key=lambda candidate: _score_schedule(candidate, human_break),
    )
    # 后续右移只负责改善目标；如果后处理意外破坏完整性或锁定位置，
    # 自动回退到这里已通过硬约束检查的完整候选，而不是让接口直接报错。
    recovery_schedule = list(final_schedule)
    # 新增菜若包含“前段机器处理 + 另一设备最终出锅”，就保留前段最早
    # 执行，仅由后续步骤/会话右移对齐出锅；同设备连续完成的新增菜仍可
    # 整体后移。
    before_late_reinsertion = final_schedule
    final_schedule = _reinsert_movable_recipes_late(
        final_schedule,
        _recipes_safe_for_whole_late_reinsertion(tasks, old_recipe_ids),
        locked_keys,
        device_rules,
        max_continuous_human,
        human_break,
        optimization_deadline - 0.6,
    )
    # 后处理也必须服从候选阶段的总时长边界；允许少量延长来换取
    # 真正更接近的出锅时间，但不能让整场排程无限向后推。
    if _score_schedule(final_schedule, human_break)[1] > shortest_makespan + 15:
        final_schedule = before_late_reinsertion
    final_schedule = _right_align_dynamic_schedule(
        final_schedule,
        locked_keys,
        device_rules,
        max_continuous_human,
        human_break,
        optimization_deadline,
    )
    final_schedule = _right_align_dynamic_sessions(
        final_schedule,
        locked_keys,
        device_rules,
        max_continuous_human,
        human_break,
        optimization_deadline,
    )
    # 最终设备会话可能为了避开冲突被安排到原计划末尾之后，形成新的目标
    # 出锅时刻；再右移一次其它未锁定步骤，向这个新目标继续靠拢。
    final_schedule = _right_align_dynamic_schedule(
        final_schedule,
        locked_keys,
        device_rules,
        max_continuous_human,
        human_break,
        optimization_deadline,
    )
    final_schedule = _right_align_dynamic_sessions(
        final_schedule,
        locked_keys,
        device_rules,
        max_continuous_human,
        human_break,
        optimization_deadline,
    )
    final_schedule = _right_align_dynamic_final_steps(
        final_schedule,
        locked_keys,
        device_rules,
        max_continuous_human,
        human_break,
        optimization_deadline,
    )
    final_schedule = _right_align_dynamic_sessions(
        final_schedule,
        locked_keys,
        device_rules,
        max_continuous_human,
        human_break,
        optimization_deadline,
    )
    final_schedule = align_virtual_preheats(
        final_schedule,
        device_rules,
        max_continuous_human,
        human_break,
        locked_keys,
    )
    recovery_schedule = align_virtual_preheats(
        recovery_schedule,
        device_rules,
        max_continuous_human,
        human_break,
        locked_keys,
    )

    def result_is_safe(candidate: list[ScheduledTask]) -> bool:
        try:
            validate_schedule(
                candidate,
                device_rules,
                expected_keys=set(task_by_key),
                max_continuous_human=max_continuous_human,
                human_break=human_break,
            )
            validate_preheat_contiguity(candidate)
        except ValueError:
            return False
        starts = {item.task.key: item.start for item in candidate}
        if any(
            abs(starts[item.task.key] - item.start) > EPSILON
            for item in locked_schedule
        ):
            return False
        return all(
            key in locked_keys or start >= elapsed_min - EPSILON
            for key, start in starts.items()
        )

    if not result_is_safe(final_schedule):
        final_schedule = recovery_schedule
    if not result_is_safe(final_schedule):
        # 理论上不会到这里；只有连后处理前的合法候选也被外部数据破坏时，
        # 才停止输出，避免伪造一个无法执行的计划。
        raise RuntimeError("动态重排无法恢复为满足硬约束的完整方案")
    standalone = _standalone_durations(
        recipes,
        device_rules,
        max_continuous_human,
        human_break,
    )
    return DynamicSchedule(
        scheduled_tasks=final_schedule,
        standalone_durations=standalone,
        locked_keys=locked_keys,
        completed_keys=completed,
        running_keys=running,
    )
