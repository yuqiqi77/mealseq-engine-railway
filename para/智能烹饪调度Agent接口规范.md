# 多菜谱并行烹饪规划接口文档

## 1. 接口说明

本接口根据输入的菜谱列表生成多菜并行烹饪计划，包括整体耗时、各菜谱烹饪时间线、食材汇总、详细执行时间线以及各菜谱制作步骤。

接口不接收当前时间字段，服务端以收到请求时的系统时间作为规划开始时间。

## 2. 接口信息

| 项目 | 内容 |
| --- | --- |
| 接口名称 | 生成多菜并行烹饪计划 |
| 请求方法 | `POST` |
| 请求地址 | 自行部署 |
| Content-Type | `application/json` |
| 成功响应 | HTTP `200`，直接返回规划结果 JSON，不增加 `code`、`message`、`data` 包装 |

## 3. 请求参数

请求体为菜谱对象数组。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | `integer/string` | 是 | 菜谱唯一标识，同一次请求中不得重复 |
| `name` | `string` | 是 | 菜谱名称，不能为空，必须与菜谱库中的名称完全一致 |

### 3.1 请求示例

```json
[
  {
    "id": 1001,
    "name": "照烧鸡腿"
  },
  {
    "id": 1002,
    "name": "清炒西兰花"
  }
]
```

## 4. 响应参数

### 4.1 顶层字段

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `overview` | `object` | 是 | 本次多菜烹饪计划概览 |
| `cookingTimeline` | `array` | 是 | 每道菜的开始时间、完成时间及烹饪设备 |
| `ingredientsSummary` | `array` | 是 | 按荤菜、素菜、调味品汇总的食材 |
| `detailTimeline` | `array` | 是 | 可直接执行的详细时间线 |
| `recipeDetail` | `array` | 是 | 每道菜的主料、辅料及制作步骤 |

### 4.2 overview

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `finishTime` | `string` | 全部菜品完成时间，格式为 `HH:mm` |
| `timeSpent` | `string` | 并行烹饪总耗时，单位为分钟 |
| `timeSave` | `string` | 相比逐道串行烹饪节省的时间，单位为分钟 |
| `recipeCount` | `integer` | 菜谱数量，必须等于请求数组长度 |

### 4.3 cookingTimeline

每个请求菜谱对应一项。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `name` | `string` | 菜谱名称，必须与请求中的名称完全一致 |
| `product` | `string` | 该菜谱使用的唯一主要设备，如“烤箱”“蒸箱”“灶具” |
| `startTime` | `string` | 菜谱开始时间，格式为 `HH:mm` |
| `endTime` | `string` | 菜谱完成时间，格式为 `HH:mm` |
| `timeSpent` | `string` | 从开始到完成的耗时，单位为分钟 |

### 4.4 ingredientsSummary

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `type` | `string` | 只能为“荤菜”“素菜”或“调味品” |
| `list` | `array` | 该类别下合并后的食材列表 |
| `list[].name` | `string` | 食材名称 |
| `list[].unit` | `string` | 汇总后的用量，如 `500g`、`10ml`、`2个` |

相同食材应合并用量，不得在同一分类中重复返回。

### 4.5 detailTimeline

| 字段 | 类型 | 必填条件 | 说明 |
| --- | --- | --- | --- |
| `timeInterval` | `string` | 始终必填 | 相对计划开始时间的分钟区间，如 `10-20` |
| `title` | `string` | 始终必填 | 简洁的步骤标题 |
| `type` | `integer` | 始终必填 | 步骤类型，只能取 `1～6` |
| `list` | `string[]` | 始终必填 | 当前时间段内要执行的具体操作 |
| `recipeNames` | `string[]` | 设备工作步骤必填 | 当前步骤涉及的菜谱名称 |
| `parameters` | `array` | 条件必填 | 当前步骤的设备工作参数 |

#### 4.5.1 detailTimeline.type 枚举

| 值 | 类型 | 说明 |
| ---: | --- | --- |
| `1` | 食材处理 | 清洗、切配、预处理等 |
| `2` | 腌制备料 | 腌制、调料混合、酱汁准备等 |
| `3` | 炖煮类 | 炖、煮、焯水、煲等灶具长时间加热 |
| `4` | 炒制类 | 炒、煎、炸、爆香等灶具短时间操作 |
| `5` | 蒸烤类 | 蒸箱或烤箱工作，包括预热、蒸、烤 |
| `6` | 出锅装盘 | 出锅、装盘、浇汁、摆盘等收尾操作 |

#### 4.5.2 parameters

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `recipeName` | `string` | 必须存在于当前步骤的 `recipeNames` 中 |
| `temperature` | `number/string` | 工作温度，如 `200`；无法数字化时可使用“中小火” |
| `time` | `integer` | 设备工作时间，单位为分钟 |

设备工作步骤通常为 `type=3/4/5`，必须返回 `recipeNames`。

除预热后的正式烹饪步骤外，设备工作步骤应返回 `parameters`。涉及预热时，应满足以下要求：

1. 预热步骤必须返回工作温度和预热时间。
2. `timeInterval` 必须反映真实预热时间。例如预热 10 分钟应为 `10-20`，不能写成 `10-10`。
3. 预热完成后的正式蒸烤步骤可以省略 `parameters`，但仍须返回 `recipeNames`。

### 4.6 recipeDetail

每个请求菜谱对应一项。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `name` | `string` | 菜谱名称，必须与请求名称完全一致 |
| `majorIngredients` | `array` | 主料列表 |
| `minorIngredients` | `array` | 辅料及调味品列表 |
| `cookingSteps` | `array` | 按实际制作顺序排列的步骤 |

主料和辅料对象格式：

```json
{
  "name": "鸡腿",
  "unit": "500g"
}
```

#### 4.6.1 cookingSteps

| 字段 | 类型 | 必填条件 | 说明 |
| --- | --- | --- | --- |
| `describe` | `string` | 始终必填 | 步骤描述 |
| `cookingParameters` | `object` | 设备工作步骤必填 | 当前步骤的烹饪参数 |
| `cookingParameters.mode` | `string` | 设备工作步骤必填 | 工作模式，如“烘烤模式” |
| `cookingParameters.temperature` | `string` | 设备工作步骤必填 | 温度或火力，如 `180`、`中小火` |
| `cookingParameters.time` | `string` | 设备工作步骤必填 | 工作时间，单位为分钟 |

## 5. 格式及校验要求

1. 必须根据服务端当前时间规划，所有绝对时间采用 `HH:mm` 格式。
2. 多道菜应尽可能并行执行，并以接近同时完成为规划目标。
3. `finishTime` 不得早于任何一道菜的 `endTime`。
4. `timeSpent` 为整个并行计划从开始到完成的实际跨度。
5. `timeSave` 为串行完成预计耗时与并行计划耗时之差，不得小于 `0`。
6. `timeInterval` 表示相对计划开始时间的分钟区间，其结束值不得大于 `overview.timeSpent`。
7. 菜谱名称必须与请求完全一致，不得使用简称、别名或相似名称。
8. `cookingTimeline` 和 `recipeDetail` 必须覆盖请求中的全部菜谱，不得遗漏或增加菜谱。
9. `detailTimeline` 必须先按 `type` 升序排列；相同 `type` 再按 `timeInterval` 的开始时间升序排列。
10. `parameters[].recipeName` 必须同时存在于当前步骤的 `recipeNames` 中。
11. 步骤标题应优先使用“预热、烘烤、蒸煮、蒸饪、煎炸、炖煮、爆炒、油炸、焯水、装盘”等简洁名称。
12. 食材处理描述使用“准备……备用”。
13. 腌制描述使用“将……腌制……”。
14. 设备工作描述使用“将……放入{设备}，设置……”。
15. 收尾步骤描述使用“出锅/装盘……”。
16. 成功响应只能包含合法 JSON，不得包含 Markdown、解释文字或生成过程。

## 6. 成功响应示例

以下示例假设服务端规划开始时间为 `11:50`。

```json
{
  "overview": {
    "finishTime": "12:30",
    "timeSpent": "40",
    "timeSave": "40",
    "recipeCount": 2
  },
  "cookingTimeline": [
    {
      "name": "照烧鸡腿",
      "product": "烤箱",
      "startTime": "11:50",
      "endTime": "12:30",
      "timeSpent": "40"
    },
    {
      "name": "清炒西兰花",
      "product": "灶具",
      "startTime": "11:50",
      "endTime": "12:30",
      "timeSpent": "40"
    }
  ],
  "ingredientsSummary": [
    {
      "type": "荤菜",
      "list": [
        {
          "name": "鸡腿",
          "unit": "500g"
        }
      ]
    },
    {
      "type": "素菜",
      "list": [
        {
          "name": "西兰花",
          "unit": "300g"
        }
      ]
    },
    {
      "type": "调味品",
      "list": [
        {
          "name": "照烧酱",
          "unit": "50g"
        },
        {
          "name": "盐",
          "unit": "5g"
        }
      ]
    }
  ],
  "detailTimeline": [
    {
      "timeInterval": "0-10",
      "title": "食材处理",
      "type": 1,
      "list": [
        "准备鸡腿，清洗并划刀备用",
        "准备西兰花，清洗并切成小朵备用"
      ]
    },
    {
      "timeInterval": "10-20",
      "title": "腌制备料",
      "type": 2,
      "list": [
        "将鸡腿与照烧酱腌制10分钟"
      ]
    },
    {
      "timeInterval": "30-38",
      "title": "爆炒",
      "type": 4,
      "recipeNames": [
        "清炒西兰花"
      ],
      "list": [
        "将西兰花放入炒锅，设置大火翻炒8分钟"
      ],
      "parameters": [
        {
          "recipeName": "清炒西兰花",
          "temperature": "大火",
          "time": 8
        }
      ]
    },
    {
      "timeInterval": "10-20",
      "title": "预热",
      "type": 5,
      "recipeNames": [
        "照烧鸡腿"
      ],
      "list": [
        "将烤箱预热，设置烘烤模式200℃，预热10分钟"
      ],
      "parameters": [
        {
          "recipeName": "照烧鸡腿",
          "temperature": 200,
          "time": 10
        }
      ]
    },
    {
      "timeInterval": "20-40",
      "title": "烘烤",
      "type": 5,
      "recipeNames": [
        "照烧鸡腿"
      ],
      "list": [
        "将照烧鸡腿放入烤箱，设置烘烤模式200℃，烹饪20分钟"
      ]
    },
    {
      "timeInterval": "38-40",
      "title": "出锅装盘",
      "type": 6,
      "list": [
        "清炒西兰花出锅装盘",
        "照烧鸡腿取出装盘并浇汁"
      ]
    }
  ],
  "recipeDetail": [
    {
      "name": "照烧鸡腿",
      "majorIngredients": [
        {
          "name": "鸡腿",
          "unit": "500g"
        }
      ],
      "minorIngredients": [
        {
          "name": "照烧酱",
          "unit": "50g"
        }
      ],
      "cookingSteps": [
        {
          "describe": "准备鸡腿，清洗并划刀备用"
        },
        {
          "describe": "将鸡腿与照烧酱腌制10分钟"
        },
        {
          "describe": "将鸡腿放入烤箱，设置烘烤模式200℃，烹饪20分钟",
          "cookingParameters": {
            "mode": "烘烤模式",
            "temperature": "200",
            "time": "20"
          }
        },
        {
          "describe": "取出照烧鸡腿，装盘浇汁即可"
        }
      ]
    },
    {
      "name": "清炒西兰花",
      "majorIngredients": [
        {
          "name": "西兰花",
          "unit": "300g"
        }
      ],
      "minorIngredients": [
        {
          "name": "盐",
          "unit": "5g"
        }
      ],
      "cookingSteps": [
        {
          "describe": "准备西兰花，清洗并切成小朵备用"
        },
        {
          "describe": "将西兰花放入炒锅，设置大火翻炒8分钟",
          "cookingParameters": {
            "mode": "爆炒",
            "temperature": "大火",
            "time": "8"
          }
        },
        {
          "describe": "清炒西兰花出锅装盘即可"
        }
      ]
    }
  ]
}
```

