"""使用单层工序顺序编码的遗传算法求解静态多菜排程。

染色体只保存所有步骤的优先顺序，不保存机器选择，也不保存菜谱开始时间：
- 每个步骤的设备由菜谱数据固定；
- 人工资源固定为 1 人；
- 前置依赖、人工和设备约束统一由解码器处理。

遗传算法只搜索“多个已经可以执行的步骤中，优先安排哪一个”。这样既符合
当前烹饪问题，也比柔性车间的双层编码更容易理解。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, replace


# 遗传算法是备选静态求解器，也必须服从接口响应时间预算。
GENETIC_TIME_LIMIT_SECONDS = 0.8
EXACT_REPAIR_TIME_LIMIT_SECONDS = 0.9
EXACT_REPAIR_SPREAD_THRESHOLD_MIN = 5.0
# 官方接口要求静态排程 5 秒内返回。求解阶段只使用其中 2.6 秒，
# 为菜谱读取、资源实例分配、最终合法性校验、状态落盘和 JSON 生成留出余量。
STATIC_SOLVER_TIME_LIMIT_SECONDS = 2.6


def _deadline_reached(deadline: float | None) -> bool:
    return deadline is not None and time.perf_counter() >= deadline

try:
    from .scheduler import (
        EPSILON,
        TIME_STEP_MIN,
        DeviceRule,
        ScheduledTask,
        Task,
        _maximum_continuous_human,
        _place_tasks,
        _recipe_bounds,
        _remaining_path_lengths,
        _right_justify,
        _score_schedule,
        build_tasks,
        validate_schedule,
    )
    from .static_solver_common import (
        assert_no_resource_conflicts,
        DeviceSession,
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
        _maximum_continuous_human,
        _place_tasks,
        _recipe_bounds,
        _remaining_path_lengths,
        _right_justify,
        _score_schedule,
        build_tasks,
        validate_schedule,
    )
    from static_solver_common import (
        assert_no_resource_conflicts,
        DeviceSession,
        infer_device_sessions,
        resource_conflicts,
        foreground_sessions_are_contiguous,
    )


@dataclass
class Chromosome:
    """一份候选排程：列表越靠前的步骤，调度优先级越高。"""

    operation_order: list[str]


def _add_stove_session_guard_dependencies(tasks: list[Task]) -> list[Task]:
    """先完成连续灶具链需要的独立准备步骤，再允许链首开始。

    例如糯米烧麦的“倒入糯米拌匀”同时依赖前面的糯米饭和炒锅步骤。
    若不把糯米饭作为链首的保护依赖，遗传算法会过早开始炒锅，随后让锅
    空等糯米饭，形成很长的虚假灶眼占用。
    """

    sessions = infer_device_sessions(tasks)
    by_key = {task.key: task for task in tasks}
    additions: dict[str, list[str]] = {}
    for session in sessions:
        if session.device in ("冰箱", "烟机"):
            continue
        members = set(session.task_keys)
        first = by_key[session.first_task_key]
        for task_key in session.task_keys[1:]:
            for dependency in by_key[task_key].dependencies:
                dependency_task = by_key.get(dependency)
                if (
                    dependency not in members
                    and dependency_task is not None
                    and dependency_task.step_no < first.step_no
                ):
                    additions.setdefault(first.key, []).append(dependency)

    guarded: list[Task] = []
    for task in tasks:
        extra = additions.get(task.key, [])
        if not extra:
            guarded.append(task)
            continue
        dependencies = tuple(dict.fromkeys((*task.dependencies, *extra)))
        guarded.append(replace(task, dependencies=dependencies))
    return guarded


def _copy_chromosome(item: Chromosome) -> Chromosome:
    """复制染色体，防止精英个体被后续变异意外修改。"""

    return Chromosome(list(item.operation_order))


def _calculate_standalone_times(
    tasks: list[Task],
    recipe_ids: list[str],
    device_rules: dict[str, DeviceRule],
    max_continuous_human: float,
    human_break: float,
) -> dict[str, float]:
    """计算每道菜单独制作需要多少分钟，供指标和安全兜底使用。"""

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
        standalone[recipe_id] = max(
            (item.end for item in own_schedule),
            default=0.0,
        )
    return standalone


def _random_chromosome(task_keys: list[str], rng: random.Random) -> Chromosome:
    """随机打乱全部步骤，生成一个单层工序顺序染色体。"""

    operation_order = list(task_keys)
    rng.shuffle(operation_order)
    return Chromosome(operation_order)


def _decode_chromosome(
    chromosome: Chromosome,
    tasks: list[Task],
    recipe_ids: list[str],
    device_rules: dict[str, DeviceRule],
    max_continuous_human: float,
    human_break: float,
) -> list[ScheduledTask]:
    """把工序顺序翻译成满足依赖、人工和设备约束的实际时间表。"""

    # 列表位置就是优先级：排在第 0 位的步骤最优先。
    task_priority = {
        task_key: index
        for index, task_key in enumerate(chromosome.operation_order)
    }
    # 单层编码不含开始时间。所有菜都从第 0 分钟起允许被调度。
    release_times = {recipe_id: 0.0 for recipe_id in recipe_ids}
    return _place_tasks(
        tasks,
        release_times,
        device_rules,
        max_continuous_human,
        human_break,
        task_priority=task_priority,
    )


def _evaluate_chromosome(
    chromosome: Chromosome,
    tasks: list[Task],
    recipe_ids: list[str],
    sessions: list[DeviceSession],
    device_rules: dict[str, DeviceRule],
    max_continuous_human: float,
    human_break: float,
    evaluation_cache: dict[tuple[str, ...], tuple[float, list[ScheduledTask]]],
) -> tuple[float, list[ScheduledTask]]:
    """解码并评分；分数越小，候选方案越好。"""

    cache_key = tuple(chromosome.operation_order)
    if cache_key in evaluation_cache:
        return evaluation_cache[cache_key]

    schedule = _decode_chromosome(
        chromosome,
        tasks,
        recipe_ids,
        device_rules,
        max_continuous_human,
        human_break,
    )
    base_score = _score_schedule(schedule, human_break)[0]
    conflicts = resource_conflicts(schedule, device_rules, sessions)
    continuous = _maximum_continuous_human(schedule, human_break)

    # 关键点：违规方案使用百万级惩罚，确保它不可能靠较短工期胜出。
    penalty = 1_000_000 * len(conflicts)
    if not foreground_sessions_are_contiguous(schedule, sessions):
        penalty += 1_000_000
    penalty += 1_000_000 * max(
        0.0,
        continuous - max_continuous_human,
    )
    result = (base_score + penalty, schedule)
    evaluation_cache[cache_key] = result
    return result


def _build_initial_population(
    tasks: list[Task],
    recipe_ids: list[str],
    population_size: int,
    rng: random.Random,
) -> list[Chromosome]:
    """用三种有意义的工序顺序加随机排列组成第一代。"""

    task_keys = [task.key for task in tasks]
    remaining = _remaining_path_lengths(tasks)

    # 种子1：菜谱输入顺序，同一道菜内部按原始步骤顺序。
    original_order = list(task_keys)

    # 种子2：菜谱顺序反转，但每道菜内部仍保持 s1、s2、s3……的顺序。
    reverse_recipe_order = [
        task.key
        for recipe_id in reversed(recipe_ids)
        for task in sorted(
            (item for item in tasks if item.recipe_id == recipe_id),
            key=lambda item: item.step_no,
        )
    ]

    # 种子3：剩余关键路径越长的步骤越靠前，优先保护长链任务。
    critical_path_order = [
        task.key
        for task in sorted(
            tasks,
            key=lambda item: (
                -remaining[item.key],
                item.recipe_name,
                item.step_no,
            ),
        )
    ]

    population = [
        Chromosome(original_order),
        Chromosome(reverse_recipe_order),
        Chromosome(critical_path_order),
    ]
    while len(population) < population_size:
        population.append(_random_chromosome(task_keys, rng))
    return population


def _tournament_select(
    scored: list[tuple[float, Chromosome]],
    rng: random.Random,
) -> Chromosome:
    """随机抽取 3 个候选，返回其中分数最低的一个作为父代。"""

    competitors = rng.sample(scored, 3)
    return min(competitors, key=lambda item: item[0])[1]


def _order_crossover(
    first: Chromosome,
    second: Chromosome,
    rng: random.Random,
) -> Chromosome:
    """顺序交叉：保留父代片段，同时保证每个步骤恰好出现一次。"""

    size = len(first.operation_order)
    if size < 2:
        return _copy_chromosome(first)

    left, right = sorted(rng.sample(range(size), 2))
    right += 1
    child_order: list[str | None] = [None] * size
    child_order[left:right] = first.operation_order[left:right]

    used = set(first.operation_order[left:right])
    remaining_genes = [
        key for key in second.operation_order
        if key not in used
    ]
    remaining_iterator = iter(remaining_genes)
    for index in range(size):
        if child_order[index] is None:
            child_order[index] = next(remaining_iterator)

    return Chromosome([str(key) for key in child_order])


def _mutate_chromosome(item: Chromosome, rng: random.Random) -> None:
    """通过交换或移动步骤改变局部工序顺序。"""

    size = len(item.operation_order)
    if size < 2:
        return

    # 交换变异：交换两个步骤的位置。
    if rng.random() < 0.35:
        first, second = rng.sample(range(size), 2)
        item.operation_order[first], item.operation_order[second] = (
            item.operation_order[second],
            item.operation_order[first],
        )

    # 插入变异：抽出一个步骤，再插入另一个位置，适合改变局部菜品穿插方式。
    if rng.random() < 0.20:
        source, target = rng.sample(range(size), 2)
        task_key = item.operation_order.pop(source)
        item.operation_order.insert(target, task_key)


def _build_serial_fallback(
    tasks: list[Task],
    recipe_ids: list[str],
    standalone: dict[str, float],
    device_rules: dict[str, DeviceRule],
    max_continuous_human: float,
    human_break: float,
) -> list[ScheduledTask]:
    """极端情况下按菜谱串行执行；它不是染色体，只是安全兜底。"""

    release_times: dict[str, float] = {}
    cursor = 0.0
    for recipe_id in recipe_ids:
        release_times[recipe_id] = cursor
        cursor += standalone[recipe_id]
    return _place_tasks(
        tasks,
        release_times,
        device_rules,
        max_continuous_human,
        human_break,
    )


def _schedule_is_legal(
    schedule: list[ScheduledTask],
    device_rules: dict[str, DeviceRule],
    max_continuous_human: float,
    human_break: float,
) -> bool:
    """统一判断候选排程是否满足依赖、人工和连续设备规则。"""

    try:
        validate_schedule(schedule, device_rules)
    except ValueError:
        return False
    sessions = infer_device_sessions([item.task for item in schedule])
    return (
        foreground_sessions_are_contiguous(schedule, sessions)
        and
        _maximum_continuous_human(schedule, human_break)
        <= max_continuous_human + EPSILON
    )


def _shift_one_recipe(
    schedule: list[ScheduledTask],
    recipe_id: str,
    delay: float,
) -> list[ScheduledTask]:
    """将一道菜的全部步骤整体向后移动，不改变菜内步骤相对位置。"""

    shifted = [
        ScheduledTask(
            task=item.task,
            start=item.start + delay,
        )
        if item.task.recipe_id == recipe_id
        else item
        for item in schedule
    ]
    return sorted(
        shifted,
        key=lambda item: (
            item.start,
            item.task.recipe_name,
            item.task.step_no,
        ),
    )


def _shift_task_group(
    schedule: list[ScheduledTask],
    task_keys: tuple[str, ...],
    delay: float,
) -> list[ScheduledTask]:
    """把一个连续设备会话整体后移，保持会话内部相对时间不变。"""

    members = set(task_keys)
    shifted = [
        ScheduledTask(task=item.task, start=item.start + delay)
        if item.task.key in members
        else item
        for item in schedule
    ]
    return sorted(
        shifted,
        key=lambda item: (
            item.start,
            item.task.recipe_name,
            item.task.step_no,
        ),
    )


def _right_justify_device_sessions(
    schedule: list[ScheduledTask],
    sessions: list[DeviceSession],
    device_rules: dict[str, DeviceRule],
    max_continuous_human: float,
    human_break: float,
    deadline: float | None = None,
) -> list[ScheduledTask]:
    """把连续设备会话作为整体右移，避免逐步骤移动把会话拆开。"""

    current = list(schedule)
    target_finish = max((item.end for item in current), default=0.0)
    session_order = sorted(
        sessions,
        key=lambda session: _recipe_bounds(current)[
            current[
                next(
                    index
                    for index, item in enumerate(current)
                    if item.task.key == session.first_task_key
                )
            ].task.recipe_id
        ][1],
    )

    for session in session_order:
        if _deadline_reached(deadline):
            break
        by_key = {item.task.key: item for item in current}
        recipe_id = by_key[session.first_task_key].task.recipe_id
        recipe_finish = _recipe_bounds(current)[recipe_id][1]
        delay = int(
            (target_finish - recipe_finish) / TIME_STEP_MIN
        ) * TIME_STEP_MIN
        while delay > EPSILON:
            if _deadline_reached(deadline):
                break
            candidate = _shift_task_group(current, session.task_keys, delay)
            if _schedule_is_legal(
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


def _align_recipe_blocks(
    schedule: list[ScheduledTask],
    device_rules: dict[str, DeviceRule],
    max_continuous_human: float,
    human_break: float,
    deadline: float | None = None,
) -> list[ScheduledTask]:
    """把提前完成的菜作为整体向后平移，尽量靠近最后出锅时刻。"""

    current = list(schedule)
    target_finish = max((item.end for item in current), default=0.0)
    original_bounds = _recipe_bounds(current)
    recipe_order = sorted(
        original_bounds,
        key=lambda recipe_id: original_bounds[recipe_id][1],
    )

    for recipe_id in recipe_order:
        if _deadline_reached(deadline):
            break
        current_finish = _recipe_bounds(current)[recipe_id][1]
        delay = target_finish - current_finish
        delay = int(delay / TIME_STEP_MIN) * TIME_STEP_MIN

        # 关键点：从“刚好同时出锅”开始向前试，找到最晚的合法位置。
        while delay > EPSILON:
            if _deadline_reached(deadline):
                break
            candidate = _shift_one_recipe(current, recipe_id, delay)
            if _schedule_is_legal(
                candidate,
                device_rules,
                max_continuous_human,
                human_break,
            ):
                current = candidate
                break
            delay -= TIME_STEP_MIN
    return current


def _improve_final_schedule(
    schedule: list[ScheduledTask],
    sessions: list[DeviceSession],
    device_rules: dict[str, DeviceRule],
    max_continuous_human: float,
    human_break: float,
    deadline: float | None = None,
) -> list[ScheduledTask]:
    """将提前完成的菜向右移动，在不违规的前提下缩小出锅时间差。"""

    # 关键点：先整道菜平移，保持“放入—运行—取出”的连续设备段不被拆开。
    aligned = _align_recipe_blocks(
        schedule,
        device_rules,
        max_continuous_human,
        human_break,
        deadline,
    )
    best = min(
        (schedule, aligned),
        key=lambda item: _score_schedule(item, human_break),
    )

    # 整道菜无法后移时，再把“预热/烹饪/取出”等连续设备会话整体后移。
    # 它比整道菜粒度更细，同时不会像逐步骤移动那样拆散设备会话。
    session_shifted = _right_justify_device_sessions(
        best,
        sessions,
        device_rules,
        max_continuous_human,
        human_break,
        deadline,
    )
    best = min(
        (best, session_shifted),
        key=lambda item: _score_schedule(item, human_break),
    )

    # 再用原来的逐步骤右移填补细小空隙；只处理最终赢家，不在每代调用。
    if _deadline_reached(deadline):
        return best
    shifted = _right_justify(
        best, device_rules, max_continuous_human, human_break
    )
    is_legal = (
        not resource_conflicts(shifted, device_rules, sessions)
        and foreground_sessions_are_contiguous(shifted, sessions)
    )
    human_is_legal = (
        _maximum_continuous_human(shifted, human_break)
        <= max_continuous_human + EPSILON
    )
    score_is_better = (
        _score_schedule(shifted, human_break)
        < _score_schedule(best, human_break)
    )
    if is_legal and human_is_legal and score_is_better:
        return shifted
    return best


def _build_finish_aligned_candidate(
    tasks: list[Task],
    recipe_ids: list[str],
    standalone: dict[str, float],
    sessions: list[DeviceSession],
    device_rules: dict[str, DeviceRule],
    max_continuous_human: float,
    human_break: float,
) -> list[ScheduledTask]:
    """按剩余关键路径反推短菜步骤的放行时间，生成同出锅候选。"""

    target_finish = max(standalone.values(), default=0.0)
    longest_recipe = max(
        recipe_ids,
        key=lambda recipe_id: standalone.get(recipe_id, 0.0),
        default="",
    )
    remaining = _remaining_path_lengths(tasks)
    task_release_times = {
        task.key: (
            0.0
            if task.recipe_id == longest_recipe
            else max(0.0, target_finish - remaining[task.key])
        )
        for task in tasks
    }

    # 先安排最长菜的骨架，短菜再按反推时间填入空档，避免短菜抢占后段资源
    # 后把长菜整体推迟。优先级仍然只有一层工序顺序，没有编码开始时间。
    ordered = sorted(
        tasks,
        key=lambda task: (
            task.recipe_id != longest_recipe,
            task.step_no,
            task.recipe_name,
        ),
    )
    task_priority = {
        task.key: index
        for index, task in enumerate(ordered)
    }
    release_times = {recipe_id: 0.0 for recipe_id in recipe_ids}
    candidate = _place_tasks(
        tasks,
        release_times,
        device_rules,
        max_continuous_human,
        human_break,
        task_priority=task_priority,
        task_release_times=task_release_times,
    )
    return _improve_final_schedule(
        candidate,
        sessions,
        device_rules,
        max_continuous_human,
        human_break,
    )


def solve_genetic_schedule(
    recipes: list[dict],
    device_rules: dict[str, DeviceRule],
    max_continuous_human: float = 30.0,
    human_break: float = 1.0,
) -> tuple[list[ScheduledTask], dict[str, float]]:
    """组织单层编码遗传算法的完整执行流程。"""

    # 首次载入求解器库可能耗时明显；先完成载入，再启动搜索预算计时。
    try:
        try:
            from .cp_sat_solver import solve_cp_sat_schedule as exact_solver
        except ImportError:
            from cp_sat_solver import solve_cp_sat_schedule as exact_solver
    except ImportError:
        exact_solver = None

    started_at = time.perf_counter()
    deadline = started_at + GENETIC_TIME_LIMIT_SECONDS
    overall_deadline = started_at + STATIC_SOLVER_TIME_LIMIT_SECONDS
    rng = random.Random(42)
    tasks = _add_stove_session_guard_dependencies(build_tasks(recipes))
    recipe_ids = [str(recipe["recipe_id"]) for recipe in recipes]
    sessions = infer_device_sessions(tasks)
    standalone = _calculate_standalone_times(
        tasks,
        recipe_ids,
        device_rules,
        max_continuous_human,
        human_break,
    )


    # 关键点：种群和代数保持适中，并同时设置真实时间上限。
    population_size = 18 if len(tasks) <= 50 else 14
    generations = 16 if len(tasks) <= 50 else 12
    population = _build_initial_population(
        tasks,
        recipe_ids,
        population_size,
        rng,
    )
    evaluation_cache: dict[
        tuple[str, ...],
        tuple[float, list[ScheduledTask]],
    ] = {}

    for _generation in range(generations):
        if _generation > 0 and time.perf_counter() >= deadline:
            break
        scored = sorted(
            (
                (
                    _evaluate_chromosome(
                        item,
                        tasks,
                        recipe_ids,
                        sessions,
                        device_rules,
                        max_continuous_human,
                        human_break,
                        evaluation_cache,
                    )[0],
                    item,
                )
                for item in population
            ),
            key=lambda pair: pair[0],
        )
        # 关键点：最好的 4 个个体直接保留，避免交叉、变异破坏当前最优解。
        next_population = [_copy_chromosome(item) for _, item in scored[:4]]
        while len(next_population) < population_size:
            first_parent = _tournament_select(scored, rng)
            second_parent = _tournament_select(scored, rng)
            child = _order_crossover(first_parent, second_parent, rng)
            _mutate_chromosome(child, rng)
            next_population.append(child)
        population = next_population

    best_score, best = min(
        (
            _evaluate_chromosome(
                item,
                tasks,
                recipe_ids,
                sessions,
                device_rules,
                max_continuous_human,
                human_break,
                evaluation_cache,
            )
            for item in population
        ),
        key=lambda pair: pair[0],
    )
    if best_score >= 1_000_000:
        best = _build_serial_fallback(
            tasks,
            recipe_ids,
            standalone,
            device_rules,
            max_continuous_human,
            human_break,
        )

    # 在逐菜右移耗尽预算前，先给全局求解留出时间。互斥的末段设备会话
    # 需要几道菜一起换位，单靠逐菜/逐会话右移无法穿过现有占用。
    if (
        exact_solver is not None
        and not _deadline_reached(overall_deadline)
        and _score_schedule(best, human_break)[2] > EXACT_REPAIR_SPREAD_THRESHOLD_MIN
    ):
        remaining_seconds = overall_deadline - time.perf_counter()
        if remaining_seconds > 0.45:
            try:
                exact_candidate, _ = exact_solver(
                    recipes,
                    device_rules,
                    max_continuous_human,
                    human_break,
                    time_limit_seconds=min(
                        EXACT_REPAIR_TIME_LIMIT_SECONDS,
                        max(0.1, remaining_seconds - 0.35),
                    ),
                )
                if (
                    _schedule_is_legal(
                        exact_candidate,
                        device_rules,
                        max_continuous_human,
                        human_break,
                    )
                    and _score_schedule(exact_candidate, human_break)
                    < _score_schedule(best, human_break)
                ):
                    best = exact_candidate
            except (ImportError, RuntimeError):
                pass

    # 单层染色体只决定顺序；剩余时间继续做同出锅右移后处理。
    if not _deadline_reached(overall_deadline):
        best = _improve_final_schedule(
            best,
            sessions,
            device_rules,
            max_continuous_human,
            human_break,
            overall_deadline,
        )

    # 遗传算法负责搜索工序顺序；解码结束后再生成一份按关键路径反推的
    # 同出锅候选。只有综合评分更好且全部硬约束合法时才替换遗传结果。
    if not _deadline_reached(overall_deadline):
        aligned_candidate = _build_finish_aligned_candidate(
            tasks,
            recipe_ids,
            standalone,
            sessions,
            device_rules,
            max_continuous_human,
            human_break,
        )
        if (
            _schedule_is_legal(
                aligned_candidate,
                device_rules,
                max_continuous_human,
                human_break,
            )
            and _score_schedule(aligned_candidate, human_break)
            < _score_schedule(best, human_break)
        ):
            best = aligned_candidate

    validate_schedule(best, device_rules)
    assert_no_resource_conflicts(best, device_rules)
    if not foreground_sessions_are_contiguous(best, sessions):
        raise RuntimeError("遗传算法未能生成连续的前台设备会话")
    return best, standalone
