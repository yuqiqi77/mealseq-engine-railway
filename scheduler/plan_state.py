"""活动排程状态的保存与读取。

HTTP 接口使用 task_id 区分排程任务；本类只负责读写某一个任务的状态文件。
状态使用 JSON 文件保存，服务重启后仍可继续动态重排。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from scheduler import ScheduledTask


@dataclass
class ActivePlan:
    plan_start: datetime
    recipes: list[dict]
    task_starts: dict[str, float]
    notifications: list[dict]
    version: int = 1

    @property
    def finish_minute(self) -> float:
        return max(self.task_starts.values(), default=0.0)

    def elapsed_minutes(self, now: datetime) -> float:
        return max(0.0, (now - self.plan_start).total_seconds() / 60)

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "planStart": self.plan_start.isoformat(),
            "recipes": self.recipes,
            "taskStarts": self.task_starts,
            "notifications": self.notifications,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ActivePlan":
        return cls(
            version=int(data.get("version", 1)),
            plan_start=datetime.fromisoformat(data["planStart"]),
            recipes=data["recipes"],
            task_starts={key: float(value) for key, value in data["taskStarts"].items()},
            notifications=data.get("notifications", []),
        )

    @classmethod
    def from_schedule(
        cls,
        plan_start: datetime,
        recipes: list[dict],
        schedule: list[ScheduledTask],
        notifications: list[dict],
        version: int = 1,
    ) -> "ActivePlan":
        return cls(
            plan_start=plan_start,
            recipes=recipes,
            task_starts={item.task.key: item.start for item in schedule},
            notifications=notifications,
            version=version,
        )


class PlanStateStore:
    """一个很小的 JSON 状态仓库，方便以后替换成数据库。"""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> ActivePlan | None:
        if not self.path.exists():
            return None
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return ActivePlan.from_dict(data)

    def save(self, plan: ActivePlan) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(plan.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()
