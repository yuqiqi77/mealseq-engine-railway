# 食序引擎 MealSeq Engine 接口文档

## 1. 接口概览

| 项目 | 内容 |
|---|---|
| 接口名称 | 多菜并行排程 / 动态加菜重排 |
| 请求方法 | `POST` |
| 默认地址 | `http://127.0.0.1:8000/schedule` |
| Content-Type | `application/json` |
| 默认算法 | 遗传算法 |
| 静态时限 | 比赛 3～5 道菜场景小于 5 秒 |
| 动态时限 | 活动排程中增加 1 道菜小于 3 秒 |

静态排程和动态重排使用同一个 `POST /schedule` 接口。服务器根据当前任务是否存在尚未执行完的活动排程，自动判断本次请求的模式。

接口没有版本前缀、登录认证或 CORS 响应头。公网部署时把下文的 `http://127.0.0.1:8000` 替换为 Railway 生成的 HTTPS 域名；从其他域名的浏览器前端调用时，需要额外增加认证与 CORS。

## 2. 请求地址

### 2.1 单会话

```http
POST /schedule
```

适用于单评委、本地演示等一次只运行一个排程的场景，内部使用默认任务编号 `default`。

### 2.2 多会话

```http
POST /schedule?task_id=demo_001
```

`task_id` 用于隔离不同用户、桌台或并发评测的活动排程。规则如下：

- 可省略；
- 长度为 1～64 位；
- 只能包含字母、数字、下划线和短横线；
- 必须以字母或数字开头。

### 2.3 指定动态插入时刻（可选）

```http
POST /schedule?task_id=demo_001&reschedule_at_min=10
```

`reschedule_at_min` 表示相对当前排程开始时刻的分钟数，仅用于已有活动排程时插入一道新菜。该参数可省略；省略或留空时，服务端仍按收到加菜请求的实际时刻重调度。参数必须是大于或等于 0 的有限数字，并且早于当前排程完成时刻。该参数只改变重调度时刻，请求正文格式不变。

## 3. 请求体

请求体必须是非空菜谱数组，每个对象必须且只能包含 `id` 和 `name`。

HTTP 请求体最大为 1 MB，字符编码为 UTF-8。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | string / integer | 是 | 菜谱唯一标识，服务端统一按字符串匹配 |
| `name` | string | 是 | 菜谱名称，必须与菜谱库中名称一致 |

### 3.1 首次静态排程

比赛场景一次提交 3～5 道菜。当前实现不限制最大菜数，超过 5 道仍可排程，但不属于比赛规定的 5 秒硬性规模。

```json
[
  {
    "id": "58e70ae1a3fd4a750f4b75af",
    "name": "双椒鳙鱼头"
  },
  {
    "id": "58e70b0e9f6429675ec805d8",
    "name": "清蒸鲈鱼"
  },
  {
    "id": "58e70b109f6429675ec805ed",
    "name": "蒜蓉粉丝蒸扇贝"
  }
]
```

### 3.2 动态加菜

同一 `task_id` 存在尚未执行完的活动排程时，请求必须且只能提交 1 道尚未在当前排程中的新菜：

```json
[
  {
    "id": "58e70b129f6429675ec80601",
    "name": "麻辣对虾"
  }
]
```

默认情况下，调用方不需要传入插入分钟，服务端以收到请求的时间减去原计划开始时间，计算动态加菜发生时刻。演示或复现指定时刻时，可以使用可选查询参数 `reschedule_at_min`；例如传入 `10`，系统会按原计划第 10 分钟的执行状态锁定步骤并重排。

## 4. 模式与状态规则

1. 当前任务没有活动排程时，请求创建静态排程。
2. 当前排程尚未完成时，后续请求作为动态加菜。
3. 刷新页面、关闭后重新打开浏览器或重启服务，不会丢失进行中的排程。
4. 当前排程最后一个步骤完成后，活动状态自动失效；下一次请求重新创建静态排程。
5. 需要提前结束本轮时，可调用 `POST /new-plan`。
6. 动态重排锁定已完成、正在执行和未来 2 分钟内即将开始的步骤，只调整其余未来步骤。
7. 指定的 `reschedule_at_min` 不能早于 0，也不能达到或超过当前排程完成时刻。
8. `/new-plan` 只清除指定任务的活动状态和通知，不删除菜谱库，也不接收 `reschedule_at_min`。

## 5. 成功响应

成功时返回 HTTP `200`。响应体直接是排程结果，不增加 `code`、`message` 或 `data` 包装，只包含以下五个顶层字段：

```json
{
  "overview": {},
  "cookingTimeline": [],
  "ingredientsSummary": [],
  "detailTimeline": [],
  "recipeDetail": []
}
```

### 5.1 overview

| 字段 | 类型 | 说明 |
|---|---|---|
| `finishTime` | string | 全部菜品预计完成时间，格式 `HH:mm` |
| `timeSpent` | string | 正式并行排程总耗时，单位为分钟 |
| `timeSave` | string | 相比逐道串行制作节省的分钟数 |
| `recipeCount` | integer | 当前完整排程中的菜品数 |

### 5.2 cookingTimeline

每道菜一项：

| 字段 | 类型 | 说明 |
|---|---|---|
| `name` | string | 菜谱名称 |
| `product` | string | 主要设备 |
| `startTime` | string | 开始时间，格式 `HH:mm` |
| `endTime` | string | 完成时间，格式 `HH:mm` |
| `timeSpent` | string | 从开始到完成的分钟数 |

### 5.3 ingredientsSummary

按“荤菜、素菜、调味品”汇总食材：

```json
{
  "type": "荤菜",
  "list": [
    {"name": "鸡腿", "unit": "500g"}
  ]
}
```

### 5.4 detailTimeline

可执行的详细时间段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `timeInterval` | string | 相对计划开始的分钟区间，如 `10-20` |
| `title` | string | 步骤标题 |
| `type` | integer | 1 食材处理、2 腌制备料、3 炖煮、4 炒制、5 蒸烤、6 出锅装盘 |
| `list` | string[] | 当前时间段需要执行的动作 |
| `recipeNames` | string[] | 设备步骤涉及的菜名 |
| `parameters` | array | 温度、火力、时长等设备参数 |

### 5.5 recipeDetail

返回每道菜的主料、辅料以及完整制作步骤。设备工作步骤的 `cookingParameters` 包括 `mode`、`temperature` 和 `time`。

完整字段规范及响应样例见：`para/智能烹饪调度Agent接口规范.md`。

## 6. 响应头指标

为了不改变正式 JSON 结构，评测辅助指标放在响应头中：

| 响应头 | 说明 |
|---|---|
| `X-Schedule-Runtime-Ms` | 本次接口排程运行时长，单位毫秒 |
| `X-Max-Continuous-Human-Min` | 最大连续人工工作时长，单位分钟 |
| `X-Finish-Spread-Min` | 最早和最晚菜品出锅时间差，单位分钟 |
| `X-Reschedule-At-Min` | 动态请求相对首轮计划开始的重调度分钟；静态排程为空 |

## 7. 错误响应

请求不合法时返回 HTTP `400`：

```json
{
  "error": "活动排程的动态加菜请求必须且只能包含1道菜"
}
```

内部排程失败时返回 HTTP `500`：

```json
{
  "error": "排程失败：RuntimeError: 错误说明"
}
```

常见错误包括：请求体为空、字段多余或缺失、菜谱不存在、名称与 ID 不匹配、动态请求不止一道菜、重复添加已有菜谱以及非法 `task_id`。

不存在的页面或接口返回 HTTP `404`。请求超过 1 MB、JSON 无法解析或查询参数不合法均返回 HTTP `400`。

## 8. 演示辅助接口

这些接口用于本地页面，不改变正式测评入口：

| 方法与地址 | 说明 |
|---|---|
| `GET /` | 本地评委操作页面 |
| `GET /recipes` | 返回可选菜谱的 `id + name` 目录 |
| `GET /session` | 查询默认会话是否存在活动排程 |
| `GET /session?task_id=demo_001` | 查询指定会话状态 |
| `POST /new-plan` | 清除默认活动排程并开始新一轮 |
| `POST /new-plan?task_id=demo_001` | 清除指定会话 |
| `GET /artifacts/<文件名>` | 查看已生成的 JSON 或甘特图 |

`GET /session` 成功响应：

```json
{
  "hasPlan": true,
  "recipes": [
    {"id": "菜谱ID", "name": "菜谱名称"}
  ],
  "version": 2
}
```

`POST /new-plan` 成功响应：

```json
{"ok": true}
```

允许通过 `/artifacts/` 查看以下固定文件：

- `01_static_request.json`
- `02_static_result.json`
- `03_static_gantt.html`
- `03_static_notifications.json`
- `04_add_recipe_request.json`
- `05_dynamic_result.json`
- `06_notifications.json`
- `07_dynamic_gantt.html`
- `08_dynamic_comparison.html`

这些文件只有在对应排程成功后才存在。非默认 `task_id` 的输出会保存在该任务自己的子目录中；当前 `/artifacts/<文件名>` 只公开默认任务根目录中的固定文件，内置网页也使用默认任务。多会话系统如需在线查看各自导出物，应扩展带 `task_id` 校验的读取接口，不能把任意文件路径直接暴露出去。

## 9. 调用示例

### PowerShell

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri 'http://127.0.0.1:8000/schedule?task_id=demo_001' `
  -ContentType 'application/json' `
  -InFile '.\scheduler\initial_request.json'
```

动态加菜继续使用同一个地址：

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri 'http://127.0.0.1:8000/schedule?task_id=demo_001' `
  -ContentType 'application/json' `
  -InFile '.\scheduler\add_recipe_request.json'
```

### curl

```bash
curl -X POST "http://127.0.0.1:8000/schedule" \
  -H "Content-Type: application/json" \
  --data-binary @scheduler/example_request.json
```

指定第 10 分钟动态插入：

```bash
curl -X POST "http://127.0.0.1:8000/schedule?task_id=demo_001&reschedule_at_min=10" \
  -H "Content-Type: application/json" \
  --data-binary @scheduler/add_recipe_request.json
```

## 10. 调用方处理建议

1. 为每个并发用户或桌台生成稳定且合法的 `task_id`；
2. 首次提交后保存该编号，后续加菜和重置始终携带同一编号；
3. 使用响应体驱动业务展示，使用四个 `X-*` 响应头展示性能指标；
4. 收到 HTTP `400` 时修正请求，不要自动重试；
5. 收到 HTTP `500` 时记录错误正文，可在短暂退避后重试一次；
6. 动态调用成功后，以返回的完整排程替换旧排程，不要只追加新菜步骤。

