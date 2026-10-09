"""同一排程接口的业务逻辑。

没有活动排程时，请求数组创建新计划；存在活动排程时，请求数组只表示
临时新增的菜谱，服务端自动与原计划合并并动态重排。
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from dynamic_scheduler import rebuild_old_schedule, reschedule_after_addition
from notification_generator import build_notifications, build_schedule_changes
from plan_state import ActivePlan, PlanStateStore
from scheduler import (
    ScheduledTask,
    build_tasks,
    build_api_response,
    calculate_metrics,
    load_device_rules,
    load_requested_recipes,
    prepare_recipes_for_schedule,
    schedule_from_request,
)
from static_solver_common import assign_resource_instances


@dataclass
class ServiceResult:
    api_response: dict
    notifications: list[dict]
    changes: list[dict]
    mode: str
    plan_version: int
    scheduled_tasks: list[ScheduledTask]
    metrics: dict


class CookingScheduleService:
    """线程安全的多任务排程服务；每个 task_id 拥有独立活动计划。"""

    TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

    def __init__(
        self,
        recipe_dir: Path,
        device_path: Path,
        state_path: Path,
        notification_path: Path | None = None,
    ):
        self.recipe_dir = recipe_dir
        self.device_path = device_path
        self.state_path = state_path
        self.notification_path = notification_path
        self._lock = threading.Lock()

    @classmethod
    def _normalise_task_id(cls, task_id: str) -> str:
        """校验任务编号，避免空编号、歧义编号和非法文件路径。"""

        if not isinstance(task_id, str) or not cls.TASK_ID_PATTERN.fullmatch(task_id):
            raise ValueError("task_id 必须为1～64位字母、数字、下划线或短横线，且以字母或数字开头")
        return task_id

    @staticmethod
    def _task_file(base_path: Path, task_id: str) -> Path:
        """把原来的单文件路径转换为某个任务自己的运行时文件路径。"""

        return base_path.parent / "tasks" / task_id / base_path.name

    def _store_for(self, task_id: str) -> PlanStateStore:
        return PlanStateStore(self._task_file(self.state_path, task_id))

    def _clear_task_files(self, task_id: str, store: PlanStateStore) -> None:
        """清除一个任务的活动状态及配套通知文件；调用方必须持有锁。"""

        store.clear()
        if self.notification_path is not None:
            notification_file = self._task_file(self.notification_path, task_id)
            if notification_file.exists():
                notification_file.unlink()

    def _old_plan_is_finished(self, state: ActivePlan, now: datetime) -> bool:
        """按最后一个步骤的真实结束时间判断本轮是否已经执行完毕。"""

        source_recipes = load_requested_recipes(state.recipes, self.recipe_dir)
        recipes = prepare_recipes_for_schedule(source_recipes)
        old_schedule = rebuild_old_schedule(build_tasks(recipes), state.task_starts)
        finish_minute = max((item.end for item in old_schedule), default=0.0)
        return state.elapsed_minutes(now) >= finish_minute

    def task_summary(
        self,
        task_id: str = "default",
        now: datetime | None = None,
    ) -> dict:
        """返回网页恢复会话模式所需的最小状态，不暴露内部排程细节。"""

        normalised_task_id = self._normalise_task_id(task_id)
        current_time = (now or datetime.now()).replace(second=0, microsecond=0)
        with self._lock:
            store = self._store_for(normalised_task_id)
            state = store.load()
            if state is not None and self._old_plan_is_finished(state, current_time):
                self._clear_task_files(normalised_task_id, store)
                state = None
            return {
                "hasPlan": state is not None,
                "recipes": [] if state is None else state.recipes,
                "version": 0 if state is None else state.version,
            }

    def reset_task(self, task_id: str = "default") -> None:
        """仅在用户明确点击“开始新一轮”时清除对应活动计划。"""

        normalised_task_id = self._normalise_task_id(task_id)
        with self._lock:
            store = self._store_for(normalised_task_id)
            self._clear_task_files(normalised_task_id, store)

    @staticmethod
    def _normalise_request(request_items: list[dict]) -> list[dict]:
        if not isinstance(request_items, list) or not request_items:
            raise ValueError("请求体必须是非空菜谱数组")
        for item in request_items:
            if not isinstance(item, dict) or set(item) != {"id", "name"}:
                raise ValueError("每个菜谱必须且只能包含 id 和 name")
        return [{"id": str(item["id"]), "name": item["name"]} for item in request_items]

    def _save_notifications(
        self,
        task_id: str,
        notifications: list[dict],
        changes: list[dict],
    ) -> None:
        if self.notification_path is None:
            return
        output_path = self._task_file(self.notification_path, task_id)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(
                {
                    "taskId": task_id,
                    "notifications": notifications,
                    "changes": changes,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def handle_request(
        self,
        request_items: list[dict],
        task_id: str = "default",
        now: datetime | None = None,
        reschedule_at_min: float | None = None,
    ) -> ServiceResult:
        """处理请求并记录从读取状态到完成排程的真实运行时长。"""

        started = time.perf_counter()
        result = self._handle_request(
            request_items,
            task_id=task_id,
            now=now,
            reschedule_at_min=reschedule_at_min,
        )
        result.metrics["runtimeMs"] = round(
            (time.perf_counter() - started) * 1000,
            1,
        )
        return result

    def _handle_request(
        self,
        request_items: list[dict],
        task_id: str = "default",
        now: datetime | None = None,
        reschedule_at_min: float | None = None,
    ) -> ServiceResult:
        """处理首次排程或动态加菜；HTTP 层始终调用这个方法。"""

        received_time = (now or datetime.now()).replace(second=0, microsecond=0)
        if reschedule_at_min is not None:
            try:
                reschedule_at_min = float(reschedule_at_min)
            except (TypeError, ValueError) as exc:
                raise ValueError("插入菜品时刻必须是分钟数") from exc
            if not math.isfinite(reschedule_at_min) or reschedule_at_min < 0:
                raise ValueError("插入菜品时刻必须是大于或等于0的有限分钟数")
        normalised_task_id = self._normalise_task_id(task_id)
        incoming = self._normalise_request(request_items)

        with self._lock:
            store = self._store_for(normalised_task_id)
            state = store.load()
            if state is not None:
                current_time = (
                    state.plan_start + timedelta(minutes=reschedule_at_min)
                    if reschedule_at_min is not None
                    else received_time
                )
                if self._old_plan_is_finished(state, current_time):
                    if reschedule_at_min is not None:
                        raise ValueError("插入菜品时刻必须早于当前排程的完成时刻")
                    self._clear_task_files(normalised_task_id, store)
                    state = None

            if state is None:
                if reschedule_at_min is not None:
                    raise ValueError("只有活动排程的动态加菜可以指定插入菜品时刻")
                plan = schedule_from_request(
                    incoming,
                    self.recipe_dir,
                    self.device_path,
                    plan_start=received_time,
                )
                notifications = build_notifications(plan.scheduled_tasks, elapsed_min=0.0)
                new_state = ActivePlan.from_schedule(
                    plan_start=received_time,
                    recipes=incoming,
                    schedule=plan.scheduled_tasks,
                    notifications=notifications,
                )
                store.save(new_state)
                self._save_notifications(normalised_task_id, notifications, [])
                return ServiceResult(
                    api_response=plan.response,
                    notifications=notifications,
                    changes=[],
                    mode="initial",
                    plan_version=1,
                    scheduled_tasks=plan.scheduled_tasks,
                    metrics=plan.metrics,
                )

            # 官方动态加菜约定：同一 task_id 的后续请求每次只增加一道菜。
            if len(incoming) != 1:
                raise ValueError("活动排程的动态加菜请求必须且只能包含1道菜")

            old_ids = {str(item["id"]) for item in state.recipes}
            duplicate_ids = old_ids & {str(item["id"]) for item in incoming}
            if duplicate_ids:
                raise ValueError(f"新增菜谱已经存在于活动排程：{sorted(duplicate_ids)}")

            # 先单独读取新增菜谱，尽早发现 id 或名称错误。
            load_requested_recipes(incoming, self.recipe_dir)
            full_request = [*state.recipes, *incoming]
            source_recipes = load_requested_recipes(full_request, self.recipe_dir)
            recipes = prepare_recipes_for_schedule(source_recipes)
            device_rules = load_device_rules(self.device_path)
            elapsed = state.elapsed_minutes(current_time)
            dynamic = reschedule_after_addition(
                recipes=recipes,
                old_starts=state.task_starts,
                old_recipe_ids=old_ids,
                elapsed_min=elapsed,
                device_rules=device_rules,
            )
            dynamic.scheduled_tasks = assign_resource_instances(
                dynamic.scheduled_tasks,
                device_rules,
            )
            changes = build_schedule_changes(
                state.task_starts,
                dynamic.scheduled_tasks,
                elapsed,
            )
            notifications = build_notifications(dynamic.scheduled_tasks, elapsed)
            api_response = build_api_response(
                source_recipes,
                dynamic.scheduled_tasks,
                dynamic.standalone_durations,
                state.plan_start,
            )
            # 指标用于测试和评估，不添加到正式接口 JSON 中。
            metrics = calculate_metrics(
                recipes,
                dynamic.scheduled_tasks,
                dynamic.standalone_durations,
                device_rules,
                human_break=1.0,
            )
            rounded_elapsed = round(elapsed, 3)
            metrics["rescheduleAtMin"] = (
                int(rounded_elapsed)
                if float(rounded_elapsed).is_integer()
                else rounded_elapsed
            )

            version = state.version + 1
            new_state = ActivePlan.from_schedule(
                plan_start=state.plan_start,
                recipes=full_request,
                schedule=dynamic.scheduled_tasks,
                notifications=notifications,
                version=version,
            )
            store.save(new_state)
            self._save_notifications(normalised_task_id, notifications, changes)
            return ServiceResult(
                api_response=api_response,
                notifications=notifications,
                changes=changes,
                mode="dynamic",
                plan_version=version,
                scheduled_tasks=dynamic.scheduled_tasks,
                metrics=metrics,
            )
