# 食序引擎 MealSeq Engine

食序引擎是一个面向多菜并行烹饪的排程系统。它读取结构化菜谱，在菜品依赖、单人人工、设备容量、设备模式与温度、连续设备会话和最大连续人工等约束下，生成静态排程；当烹饪过程中临时加入一道菜时，同一个接口会自动切换到动态重调度。

## 核心能力

- 多道菜静态并行排程，默认使用遗传算法；
- 在活动排程中插入一道新菜，自动执行动态重调度；
- 锁定已完成、执行中及未来 2 分钟内即将开始的步骤；
- 最小扰动前提下压缩总时长和出锅时间差；
- 校验步骤完整性、依赖、唯一人工、设备冲突、预热连续性和 30 分钟连续人工上限；
- 输出正式 JSON、通知、静态/动态甘特图和动态前后对比图；
- 静态接口目标小于 5 秒，动态重调度目标小于 3 秒。

## 项目结构

| 路径 | 说明 |
|---|---|
| `ai/parsed_recipes_v5/` | 在线排程直接使用的结构化菜谱库 |
| `ai/` | 菜谱离线解析、设备补全和校验工具 |
| `para/` | 设备配置及原始接口规范 |
| `scheduler/` | 排程算法、HTTP 服务、页面、导出器和测试 |
| `start_demo.bat` | Windows 本地一键启动入口 |
| `智能烹饪调度Agent接口文档.md` | HTTP 接口与字段说明 |
| `智能烹饪调度Agent技术方案.md` | 建模、算法、动态重调度与安全校验说明 |
| `部署运行文档.md` | 本地、Linux 和 Railway 部署步骤 |

以下目录不属于源码，会在运行时自动生成并已加入 `.gitignore`：

- `scheduler/runtime/`：按 `task_id` 保存活动排程和通知；
- `scheduler/demo_output/`：导出的请求、响应和甘特图；
- `scheduler/experiment_runtime/`：实验脚本的临时通知数据。

## 快速开始

推荐 Python 3.11 或 3.12。在项目根目录执行：

```powershell
python -m pip install -r .\requirements.txt
python .\scheduler\api_server.py
```

Windows 也可以直接双击 `start_demo.bat`。启动后打开：

```text
http://127.0.0.1:8000/
```

正式排程接口：

```text
POST http://127.0.0.1:8000/schedule
```

## 统一接口语义

- 同一 `task_id` 没有活动排程：请求中的多道菜生成静态排程；
- 同一 `task_id` 有未完成排程：请求中的一道新菜触发动态重调度；
- 可用 `reschedule_at_min` 指定演示中的插入分钟；省略时按实际收到请求的时间处理；
- 只有排程执行完毕或调用 `POST /new-plan`，下一次请求才重新成为静态排程。

示例：

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri 'http://127.0.0.1:8000/schedule?task_id=demo_001' `
  -ContentType 'application/json' `
  -InFile '.\scheduler\initial_request.json'
```

第 10 分钟插入一道菜：

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri 'http://127.0.0.1:8000/schedule?task_id=demo_001&reschedule_at_min=10' `
  -ContentType 'application/json' `
  -InFile '.\scheduler\add_recipe_request.json'
```

## 算法与约束优先级

系统先保证硬约束，再优化软目标：

1. 输出步骤完整且不重复；
2. 菜谱依赖正确；
3. 人工和容量受限设备无冲突；
4. 预热紧接对应的入炉/烹饪步骤；
5. 最大连续人工不超过 30 分钟；
6. 动态重排不移动锁定步骤，也不把未来步骤排到重调度时刻之前；
7. 在合法候选中优先缩小出锅时间差，并兼顾总时长与动态扰动。

出锅时间差 5 分钟是优化目标，不是对所有菜谱组合都能保证的硬约束。设备模式互斥、长时机器步骤或已锁定历史可能决定不可消除的下界。

## 测试

```powershell
python -m unittest discover -s scheduler -p 'test_*.py'
```

测试覆盖菜谱依赖、人工分段、设备容量、后台资源、连续设备会话、预热、静态/动态时限、状态恢复和导出页面。

## 公网部署

Railway 推荐启动命令：

```bash
python scheduler/api_server.py --host 0.0.0.0 --port $PORT --state /data/active_plan.json --notifications /data/notifications.json --output-dir /data/demo_output --no-browser
```

为 `/data` 添加持久化 Volume，并将健康检查路径设为 `/`。完整步骤见 [部署运行文档](./部署运行文档.md)。

## 使用边界

- 默认页面使用 `task_id=default`，多个访客同时操作会共享同一活动排程；正式多用户接入时应为每位用户分配独立 `task_id`。
- 当前服务没有登录、权限控制和跨域配置；公开部署前应根据使用范围增加访问控制。
- 大模型仅用于离线解析菜谱；正常在线排程不调用大模型，也不需要 API Key。
