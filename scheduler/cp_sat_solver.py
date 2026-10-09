"""使用 OR-Tools CP-SAT 求解静态多菜排程。

CP-SAT 会同时决定所有步骤的开始时间，并把依赖、唯一人工、设备容量、
同模式同温度并行等规则作为硬约束。它比逐步放置的启发式更接近车间调度
中的精确数学模型；在时间限制内会返回当前找到的最好可行解。
"""

from __future__ import annotations

import math

from ortools.sat.python import cp_model


# 比赛接口希望在3～5秒内返回。模型搜索最多使用2秒，剩余时间留给
# 菜谱读取、初始解、合法性校验和接口JSON生成。
CP_SAT_TIME_LIMIT_SECONDS = 2.0

try:
    from .scheduler import (
        EPSILON,
        TIME_STEP_MIN,
        DeviceRule,
        ScheduledTask,
        Task,
        _instance_options,
        _maximum_continuous_human,
        _place_tasks,
        _right_justify,
        _score_schedule,
        build_tasks,
        validate_schedule,
    )
    from .static_solver_common import (
        assert_no_resource_conflicts,
        infer_device_sessions,
        resource_conflicts,
        foreground_sessions_are_contiguous,
    )
except ImportError:
    from scheduler import (
        EPSILON,
        TIME_STEP_MIN,
        DeviceRule,
        ScheduledTask,
        Task,
        _instance_options,
        _maximum_continuous_human,
        _place_tasks,
        _right_justify,
        _score_schedule,
        build_tasks,
        validate_schedule,
    )
    from static_solver_common import (
        assert_no_resource_conflicts,
        infer_device_sessions,
        resource_conflicts,
        foreground_sessions_are_contiguous,
    )


def _ticks(minutes: float) -> int:
    """把分钟转换为半分钟整数，避免求解器处理浮点数。"""

    return int(math.ceil(minutes / TIME_STEP_MIN - EPSILON))


def _standalone_times(
    recipes: list[dict],
    tasks: list[Task],
    device_rules: dict[str, DeviceRule],
    max_continuous_human: float,
    human_break: float,
) -> tuple[list[ScheduledTask], dict[str, float]]:
    """快速计算单菜时长，并生成一份“短菜晚开始”的初始参考解。"""

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
    target = max(standalone.values(), default=0.0)
    aligned_release = {
        recipe_id: max(0.0, target - standalone[recipe_id])
        for recipe_id in recipe_ids
    }
    warm = _place_tasks(
        tasks,
        aligned_release,
        device_rules,
        max_continuous_human,
        human_break,
    )
    return warm, standalone


def solve_cp_sat_schedule(
    recipes: list[dict],
    device_rules: dict[str, DeviceRule],
    max_continuous_human: float = 30.0,
    human_break: float = 1.0,
    time_limit_seconds: float = CP_SAT_TIME_LIMIT_SECONDS,
    max_finish_spread_min: float | None = None,
    fixed_task_starts: dict[str, float] | None = None,
    unfixed_start_floor_min: float = 0.0,
) -> tuple[list[ScheduledTask], dict[str, float]]:
    """建立并求解 CP-SAT 模型，返回格式与原 create_schedule 完全一致。"""

    tasks = build_tasks(recipes)
    by_key = {task.key: task for task in tasks}
    constrained_reoptimization = (
        fixed_task_starts is not None or unfixed_start_floor_min > 0
    )
    fixed_task_starts = fixed_task_starts or {}
    if set(fixed_task_starts) - set(by_key):
        raise ValueError("锁定步骤不属于当前菜谱")
    warm_schedule, standalone = _standalone_times(
        recipes, tasks, device_rules, max_continuous_human, human_break
    )
    warm_starts = {item.task.key: _ticks(item.start) for item in warm_schedule}

    def build_serial_fallback() -> list[ScheduledTask]:
        """构造一定可行的串行方案，只在求解器无解或超时时兜底。"""

        releases: dict[str, float] = {}
        cursor = 0.0
        for recipe in recipes:
            recipe_id = str(recipe["recipe_id"])
            releases[recipe_id] = cursor
            cursor += standalone[recipe_id]
        return _place_tasks(
            tasks,
            releases,
            device_rules,
            max_continuous_human,
            human_break,
        )

    # 最坏情况下把全部步骤串行执行即可，因此总时长之和是安全上界。
    horizon = max(
        1,
        sum(_ticks(task.duration) for task in tasks)
        + 2 * len(tasks)
        + _ticks(unfixed_start_floor_min),
        *(
            _ticks(start) + _ticks(by_key[key].duration) + 2 * len(tasks)
            for key, start in fixed_task_starts.items()
        ),
    )
    model = cp_model.CpModel()
    starts: dict[str, cp_model.IntVar] = {}
    ends: dict[str, cp_model.IntVar] = {}
    for task in tasks:
        duration = _ticks(task.duration)
        starts[task.key] = model.new_int_var(0, horizon, f"start::{task.key}")
        ends[task.key] = model.new_int_var(0, horizon, f"end::{task.key}")
        model.add(ends[task.key] == starts[task.key] + duration)
        if task.key in fixed_task_starts:
            model.add(starts[task.key] == _ticks(fixed_task_starts[task.key]))
        elif unfixed_start_floor_min > 0:
            model.add(starts[task.key] >= _ticks(unfixed_start_floor_min))
        # 不给短菜根步骤设置强制晚启动时间。短菜可能需要先完成备料、等待设备，
        # 再把最后烹饪步骤延后；硬性晚启动反而会破坏同出锅可行解。
        for dependency in task.dependencies:
            model.add(starts[task.key] >= ends[dependency])
        if task.key in warm_starts:
            model.add_hint(starts[task.key], warm_starts[task.key])

    # 虚拟预热和对应的“放入设备”必须首尾相接，不能只满足宽松的先后关系。
    for task in tasks:
        if not task.original.get("virtual_preheat"):
            continue
        target_key = f"{task.recipe_id}:{task.original.get('source_step_id')}"
        if target_key in starts:
            model.add(starts[target_key] == ends[task.key])

    # 人只有一个：人工活跃区间不能重叠。连续操作30分钟是优化目标而非
    # “每做一步都必须休息”的硬限制，超出部分由统一评分施加高惩罚。
    human_intervals = []
    for task in tasks:
        # 一条菜谱步骤可能在开始和结束各需要一次人工，中间设备自动运行。
        # 每一段都单独进入 NoOverlap，避免把中间等待时间错误占成人工。
        for segment_index, (offset_min, duration_min) in enumerate(task.human_segments):
            offset = _ticks(offset_min)
            duration = _ticks(duration_min)
            if duration <= 0:
                continue
            human_start = model.new_int_var(
                0, horizon, f"human_start::{task.key}::{segment_index}"
            )
            human_end = model.new_int_var(
                0, horizon, f"human_end::{task.key}::{segment_index}"
            )
            model.add(human_start == starts[task.key] + offset)
            model.add(human_end == human_start + duration)
            human_intervals.append(model.new_interval_var(
                human_start,
                duration,
                human_end,
                f"human::{task.key}::{segment_index}",
            ))

    # CP-SAT 的原始目标会倾向于把全部人工步骤首尾紧密排列，可能形成超过
    # 30 分钟的连续操作。按“最多连续操作 max_continuous_human 分钟，至少
    # 休息 human_break 分钟”划分人工工作窗口，把休息段作为人工不可用区间。
    # 当前菜谱的单个人工片段最长为30分钟，因此不会拆分原始操作。
    work_ticks = _ticks(max_continuous_human)
    break_ticks = _ticks(human_break)
    if work_ticks > 0 and break_ticks > 0:
        cycle_ticks = work_ticks + break_ticks
        for break_index, break_start in enumerate(
            range(work_ticks, horizon, cycle_ticks), start=1
        ):
            break_end = min(horizon, break_start + break_ticks)
            if break_end <= break_start:
                continue
            human_intervals.append(model.new_interval_var(
                break_start,
                break_end - break_start,
                break_end,
                f"mandatory_human_break::{break_index}",
            ))
    model.add_no_overlap(human_intervals)

    # 连续 session 替代其中的零散设备区间，保证放入后到取出前设备始终被占用。
    sessions = infer_device_sessions(tasks)
    session_members = {key for session in sessions for key in session.task_keys}
    device_intervals: dict[str, list[tuple]] = {}

    # 前台制作设备中的连续步骤不能在中间暂停。例如“放入烤箱 -> 启动烹饪”
    # 以及“煸炒 -> 拌匀 -> 收汁”必须首尾相接，避免生成数小时的虚假占用条。
    # 冰箱和烟机属于后台保持型资源，允许跨步骤长时间持续。
    for session in sessions:
        if session.device in ("冰箱", "烟机"):
            continue
        for previous_key, next_key in zip(
            session.task_keys, session.task_keys[1:]
        ):
            previous = by_key[previous_key]
            next_task = by_key[next_key]
            model.add(
                starts[next_task.key] + _ticks(next_task.device_start)
                == starts[previous.key]
                + _ticks(previous.device_start + previous.device_duration)
            )

    def add_device_interval(
        device: str,
        key: str,
        start_expression,
        end_expression,
        signature: tuple,
        instance_hints: tuple[str, ...],
    ) -> None:
        interval_start = model.new_int_var(0, horizon, f"device_start::{key}")
        interval_end = model.new_int_var(0, horizon, f"device_end::{key}")
        interval_size = model.new_int_var(1, horizon, f"device_size::{key}")
        model.add(interval_start == start_expression)
        model.add(interval_end == end_expression)
        model.add(interval_size == interval_end - interval_start)
        interval = model.new_interval_var(
            interval_start, interval_size, interval_end, f"device::{key}"
        )
        device_intervals.setdefault(device, []).append((
            interval,
            signature,
            instance_hints,
            interval_start,
            interval_size,
            interval_end,
            key,
        ))

    for task in tasks:
        if (
            task.device
            and task.device_duration > EPSILON
            and task.key not in session_members
        ):
            add_device_interval(
                task.device,
                task.key,
                starts[task.key] + _ticks(task.device_start),
                starts[task.key] + _ticks(task.device_start + task.device_duration),
                task.device_signature,
                task.instance_hints,
            )
        for usage_index, usage in enumerate(task.additional_device_usages):
            add_device_interval(
                usage.device,
                f"extra::{task.key}::{usage_index}",
                starts[task.key] + _ticks(usage.start),
                starts[task.key] + _ticks(usage.start + usage.duration),
                usage.signature,
                usage.instance_hints,
            )

    for session in sessions:
        first = by_key[session.first_task_key]
        last = by_key[session.last_task_key]
        add_device_interval(
            session.device,
            session.key,
            starts[first.key] + _ticks(first.device_start),
            starts[last.key] + _ticks(last.device_start + last.device_duration),
            session.signature,
            session.instance_hints,
        )

    for device, records in device_intervals.items():
        rule = device_rules.get(device, DeviceRule())
        if rule.background:
            continue

        # 每段设备占用在模型内直接选择具体灶眼或箱体层位，避免先按总容量
        # 求解、事后才发现两段都指定了同一层而被迫丢弃整个CP方案。
        by_instance: dict[str, list[cp_model.IntervalVar]] = {}
        for (
            _interval,
            _signature,
            instance_hints,
            interval_start,
            interval_size,
            interval_end,
            key,
        ) in records:
            options = _instance_options(rule, instance_hints)
            if not options:
                raise ValueError(f"{device} 的指定实例或层位不存在：{instance_hints}")
            selectors = []
            for option_index, option in enumerate(options):
                selected = model.new_bool_var(
                    f"choose::{device}::{key}::{option_index}"
                )
                selectors.append(selected)
                for instance in option:
                    optional = model.new_optional_interval_var(
                        interval_start,
                        interval_size,
                        interval_end,
                        selected,
                        f"instance::{device}::{instance}::{key}::{option_index}",
                    )
                    by_instance.setdefault(instance, []).append(optional)
            model.add_exactly_one(selectors)

        for intervals in by_instance.values():
            model.add_no_overlap(intervals)

        if rule.same_setting_required:
            # 容量为 2 并不代表任意两道菜都能同放；参数不同的两段必须错开。
            for index, first_record in enumerate(records):
                first_interval, first_signature = first_record[:2]
                for second_record in records[index + 1:]:
                    second_interval, second_signature = second_record[:2]
                    if first_signature != second_signature:
                        model.add_no_overlap([first_interval, second_interval])

    # 每道菜完成时间 = 该菜所有步骤结束时间的最大值。
    recipe_finishes: list[cp_model.IntVar] = []
    for recipe in recipes:
        recipe_id = str(recipe["recipe_id"])
        finish = model.new_int_var(0, horizon, f"finish::{recipe_id}")
        own_ends = [ends[task.key] for task in tasks if task.recipe_id == recipe_id]
        model.add_max_equality(finish, own_ends)
        recipe_finishes.append(finish)

    makespan = model.new_int_var(0, horizon, "makespan")
    earliest_finish = model.new_int_var(0, horizon, "earliest_finish")
    spread = model.new_int_var(0, horizon, "finish_spread")
    excess = model.new_int_var(0, horizon, "spread_over_5_min")
    model.add_max_equality(makespan, recipe_finishes)
    model.add_min_equality(earliest_finish, recipe_finishes)
    model.add(spread == makespan - earliest_finish)
    model.add_max_equality(excess, [spread - _ticks(5), 0])
    if max_finish_spread_min is not None:
        model.add(spread <= _ticks(max_finish_spread_min))

    # 与统一评分一致：超过5分钟的出锅差使用高惩罚，同时兼顾总工期。
    # 超过5分钟的出锅差给予最高惩罚；在5分钟以内继续靠拢，同时兼顾总工期。
    model.minimize(10 * makespan + 100 * spread + 1200 * excess)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_seconds
    solver.parameters.num_search_workers = 8
    solver.parameters.random_seed = 42
    status = solver.solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        if constrained_reoptimization:
            raise RuntimeError("锁定步骤下未找到可行的全局重排候选")
        # 理论上串行执行总能可行；若限时内没有找到解，返回安全兜底。
        fallback = build_serial_fallback()
        validate_schedule(fallback, device_rules)
        return fallback, standalone

    cp_schedule = [
        ScheduledTask(task=task, start=solver.value(starts[task.key]) * TIME_STEP_MIN)
        for task in tasks
    ]
    cp_schedule.sort(key=lambda item: (item.start, item.task.recipe_name, item.task.step_no))
    if constrained_reoptimization:
        sessions = infer_device_sessions(tasks)
        validate_schedule(cp_schedule, device_rules)
        if (
            resource_conflicts(cp_schedule, device_rules, sessions)
            or not foreground_sessions_are_contiguous(cp_schedule, sessions)
            or _maximum_continuous_human(cp_schedule, human_break)
            > max_continuous_human + EPSILON
        ):
            raise RuntimeError("锁定步骤下的全局候选不满足设备或人工约束")
        return cp_schedule, standalone
    # 右移能进一步靠拢出锅时间，但只接收仍满足连续设备约束的版本。
    candidates = [cp_schedule]
    # CP模型擅长找到紧凑的工序先后，但没有直接表达“累计操作30分钟后休息”。
    # 按CP开始时间提取优先顺序，再由统一放置器重新安排，可以保留大部分
    # 并行结构，同时补上必要休息并再次遵守具体层位/灶眼规则。
    cp_priority = {
        item.task.key: index for index, item in enumerate(cp_schedule)
    }
    # 保留CP-SAT为了同出锅而计算出的“整道菜最早启动时刻”。如果修复人工
    # 休息时把所有菜重新从0分钟放置，会把已经对齐的短菜再次推到最前面。
    cp_release = {
        str(recipe["recipe_id"]): min(
            item.start
            for item in cp_schedule
            if item.task.recipe_id == str(recipe["recipe_id"])
        )
        for recipe in recipes
    }
    repaired_cp = _place_tasks(
        tasks,
        cp_release,
        device_rules,
        max_continuous_human,
        human_break,
        task_priority=cp_priority,
    )
    repaired_cp = _right_justify(
        repaired_cp, device_rules, max_continuous_human, human_break
    )
    if not resource_conflicts(repaired_cp, device_rules, sessions):
        candidates.append(repaired_cp)

    # 含数小时等待的菜谱逐格右移代价很高；这类情况交给模型本身决定开始时间。
    if max((task.duration for task in tasks), default=0.0) <= 300:
        shifted = _right_justify(
            cp_schedule, device_rules, max_continuous_human, human_break
        )
        if not resource_conflicts(shifted, device_rules, sessions):
            candidates.append(shifted)
    if not resource_conflicts(warm_schedule, device_rules, sessions):
        candidates.append(warm_schedule)
    # 完全按菜谱串行是保底可行解：不会有跨菜设备冲突，并保留人工休息规则。
    candidates.append(build_serial_fallback())

    legal = [
        item for item in candidates
        if (
            not resource_conflicts(item, device_rules, sessions)
            and foreground_sessions_are_contiguous(item, sessions)
            and _maximum_continuous_human(item, human_break)
            <= max_continuous_human + EPSILON
        )
    ]
    if not legal:
        raise RuntimeError("CP-SAT 未能生成满足人工和连续设备约束的排程")
    best = min(legal, key=lambda item: _score_schedule(item, human_break))
    validate_schedule(best, device_rules)
    assert_no_resource_conflicts(best, device_rules)
    return best, standalone
