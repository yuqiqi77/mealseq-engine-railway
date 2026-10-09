# 食序引擎排程模块

本目录包含 MealSeq Engine 的在线排程、动态重调度、HTTP 服务、可视化导出和自动化测试。项目总览见根目录 [README](../README.md)，完整接口、技术和部署说明分别见：

- [接口文档](../智能烹饪调度Agent接口文档.md)
- [技术方案](../智能烹饪调度Agent技术方案.md)
- [部署运行文档](../部署运行文档.md)

## 模块职责

| 文件 | 作用 |
|---|---|
| `scheduler.py` | 菜谱加载、任务建模、通用排程、指标、响应组装和最终校验 |
| `genetic_solver.py` | 默认静态遗传算法及同出锅改进 |
| `cp_sat_solver.py` | 可选 CP-SAT 求解器及短时全局修复 |
| `dynamic_scheduler.py` | 锁定历史、插入新菜、最小扰动和动态右移 |
| `static_solver_common.py` | 设备会话、实例分配和资源冲突检查 |
| `schedule_service.py` | 自动判断静态/动态模式并按 `task_id` 管理状态 |
| `api_server.py` | HTTP 接口与页面入口 |
| `web_ui.py` | 食序引擎演示页面 |
| `artifact_exporter.py` | JSON、通知和甘特图导出 |
| `notification_generator.py` | 前置准备与紧急设备操作通知 |

## 约束顺序

所有求解和后处理都必须先满足：

1. 步骤集合完整且不重复；
2. 前置依赖完成后才能开始后续步骤；
3. 单人人工区间不重叠；
4. 灶眼、蒸箱层、烤箱层等容量受限资源不冲突；
5. 同设备并行时模式、温度、湿度等参数兼容；
6. 预热步骤紧接对应的入炉/烹饪步骤；
7. 连续人工不超过 30 分钟，休息间隔至少 1 分钟；
8. 动态排程中的锁定步骤不移动，未执行步骤不早于重调度时刻。

冰箱和烟机是可并行后台资源，会保留占用记录，但不形成容量互斥。人工只在步骤声明的 `human_intervals` 内占用；无人等待且没有机器占用的区间在甘特图中使用虚线等待块表示。

## 求解预算

- 遗传搜索：0.8 秒；
- 静态 CP-SAT 修复：最多约 0.9 秒；
- 静态求解阶段总预算：2.6 秒，完整接口目标小于 5 秒；
- 动态优化阶段总预算：2.0 秒，完整接口目标小于 3 秒。

默认求解器为 `genetic`，也可设置：

```powershell
$env:COOKING_SCHEDULER_SOLVER = 'cp_sat'
```

可选值为 `genetic`、`cp_sat`、`heuristic`。

## 本地运行

在项目根目录执行：

```powershell
python -m pip install -r .\requirements.txt
python .\scheduler\api_server.py
```

命令行生成单次排程：

```powershell
python .\scheduler\run_scheduler.py `
  --request .\scheduler\example_request.json `
  --output .\scheduler\schedule_result.json `
  --solver genetic
```

完整静态到动态演示：

```powershell
python .\scheduler\demo_full_flow.py
```

`runtime/`、`demo_output/` 和 `experiment_runtime/` 均为运行时生成目录，不应提交到仓库。

## 测试

```powershell
python -m unittest discover -s scheduler -p 'test_*.py'
```

修改资源约束、右移逻辑、依赖修复或菜谱字段处理后，必须运行完整测试，不应只凭甘特图外观判断排程是否合法。
