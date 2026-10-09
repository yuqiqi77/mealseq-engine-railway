# 用 DeepSeek 解析比赛菜谱

这个脚本读取比赛提供的 `recipes_100.csv`，逐道调用 DeepSeek API，把用于后续入库和排程的 JSON 保存到 `parsed_recipes_v5/`。它不会修改原始 CSV。运行环境为 Python 3.10 或更高版本，无需安装额外 Python 包。脚本默认读取项目 `para/设备参数清单参考.json`，也可以用 `--devices` 指定其他设备清单。

## 0. 补全设备调度配置

比赛提供的 `para/设备参数清单参考1.json` 只包含设备参数。下面的命令会把它和菜谱中的层位证据交给 DeepSeek，再由程序固定校验人工、灶眼、设备实例、后台资源和并行条件：

```powershell
py .\enrich_devices.py
```

默认输出 `para/设备参数清单生成.json`。需要直接作为正式配置时可以指定：

```powershell
py .\enrich_devices.py --output ..\para\设备参数清单参考.json
```

蒸箱和烤箱仍保留原有并行限制：只有模式、温度相同才能跨层并行；加湿烤还要求湿度相同。

## 1. 准备 DeepSeek API

在 DeepSeek 开放平台创建 API Key。DeepSeek 网页端的登录账号不能直接替代 API Key。在 PowerShell 中，先为**当前终端窗口**配置密钥：

```powershell
$env:DEEPSEEK_API_KEY = '你的DeepSeek API Key'
```

脚本默认使用官方地址 `https://api.deepseek.com` 和 `deepseek-flash`。如需更换模型或兼容代理地址，可以设置：

```powershell
$env:DEEPSEEK_MODEL = 'deepseek-flash'
$env:DEEPSEEK_BASE_URL = 'https://api.deepseek.com'
```

密钥只放在环境变量中，不要发送到聊天或提交进代码仓库。

## 2. 运行

在此文件夹打开 PowerShell：

```powershell
py .\parse_recipes.py --sample --limit 10 --dry-run
py .\parse_recipes.py --sample --limit 1
py .\parse_recipes.py --sample --limit 10
```

第一行只检查 CSV；第二行实际调用 1 道菜；确认输出正确后第三行处理选定的 10 道。已有的 JSON 会跳过，因此第二行处理过的菜不会重复收费。之后处理全部 100 道：

```powershell
py .\parse_recipes.py --limit 100
```

输出文件以菜谱 ID 命名。每个 JSON 包含 `schema_version`、原文、结构化食材和与原始步骤一一对应的排程步骤。缺失时长由模型合理估计，`duration_source` 标为 `estimated`；原文给出的全过程时长标为 `explicit`。冷藏、冷冻、腌制等长时间准备完整计入 `duration_min`。

`human_active_min` 表示该步骤全部人工片段的合计时长；`human_intervals` 记录每段人工相对步骤开始的起点与时长。例如“启动预热1分钟—等待9分钟—放入食材1分钟”记录为两段人工，中间9分钟会释放唯一人工资源，但仍属于同一条原始菜谱步骤。`device_active_start_min` 和 `device_active_min` 表示主设备占用；同一步骤先后使用其它设备时记录在 `additional_device_usages`，因此“从烤箱取出—放凉—放入冰箱冷藏”不需要拆分原始步骤，也不会丢失冰箱占用。没有明确离火阶段的灶具步骤必须占用到步骤结束；相邻的“放入设备—运行—取出”步骤必须标记同一设备并形成直接依赖链。`temperature_settings` 保存完整温度设置；上下火温度不同会分别记录，`temperature_c` 保留为接口兼容用的主要温度。

本阶段的 JSON 是内部数据，不是最终 API 响应。脚本会校验步骤数量、依赖关系、人工与设备占用区间、设备模式和全部温度设置。为减少 Token 消耗，主要温度、人工总时长、人工状态和资源来源等派生字段由程序本地补齐；未离火的灶具占用、紧邻前一步的取出设备和直接依赖等确定性问题也会本地修正，不再为它们重新调用模型。模型结果仍不合格时，每道菜默认最多请求3次，之后记录失败并继续下一道。按 Ctrl+C 可以停止，已经写入的 JSON 会保留。缺失信息的估计可能不符合实际烹饪过程，批量处理前应人工检查样本。

依赖校验会额外检查同一非调味食材的加工链，以及“处理好的、完成后、继续、再次”等前序引用；不同食材的独立切配仍允许并行。人工校验会强制“按屏幕提示、按键、通电、放入、取出”等动作占用人工，并校验多段人工不重叠、总和等于 `human_active_min`；其中屏幕/按键操作还必须标记对应设备。`preheat` 单独记录是否需要预热、预热时长、发生在当前步骤内还是步骤前，以及是否整机独占。实际预热必须具有温度和正数设备时间；仅写“放入预热好的烤箱”时，排程器会补一个独立的估计预热任务，不会改动原始解析步骤数量。

旧的 `parsed_recipes/`、`parsed_recipes_v2/`、`parsed_recipes_v4/` 和 `parsed_recipes_enriched/` 不会被覆盖。版本 5 默认写入 `parsed_recipes_v5/`；同格式的已有 JSON 会跳过，避免重复调用。要重新解析某道菜，请指定一个新的输出目录，例如：

```powershell
py .\parse_recipes.py --sample --limit 1 --output .\parsed_recipes_review
```

确认需要更新当前 `parsed_recipes_v5/` 时，使用 `--overwrite`。脚本会先完成DeepSeek调用和全部本地校验，只有新结果合格后才原子替换旧文件；调用失败或按 `Ctrl+C` 不会破坏旧JSON：

```powershell
py .\parse_recipes.py --sample --limit 10 --overwrite
```

批量处理中断后，可用 `--start` 从指定序号继续，避免重新调用前面的菜谱；`--max-attempts` 控制每道菜的最大请求次数。超过5个步骤的长菜谱默认按每批5步分段解析，避免完整JSON被输出长度限制截断，可用 `--batch-size` 调整。例如从第6道继续处理到末尾：

```powershell
py .\parse_recipes.py --start 6 --limit 100 --overwrite --max-attempts 3
```

只有明确愿意承担无限重试的时间和费用时，才使用 `--max-attempts 0`。

批处理完成后，失败项会写入 `parsed_recipes_v5/failed_recipes.json`。只重试失败清单：

```powershell
py .\parse_recipes.py --retry-failures --max-attempts 3
```

如果失败清单来自旧版本尚未生成，可直接扫描并解析当前目录缺失的菜谱：

```powershell
py .\parse_recipes.py --retry-missing --max-attempts 3 --batch-size 5
```

## 3. 人工核对

逐道对照输出文件中的 `source_steps` 和 `steps`：检查是否漏掉实际动作、设备别名是否映射恰当、明确数值是否准确、估计时长是否合理、人工操作和等待是否区分、设备是否及时释放、上下火等多温度是否完整、依赖是否合理。同时核对 `ingredients` 是否覆盖原始食材清单，数量和单位是否准确。测试样例没有写明的时间应标记为估计值，不要把估计值误认为原文事实。

如果 API 返回 400，请先核对地址、模型名以及请求格式；若返回 401，请检查 API Key。DeepSeek 的 JSON 模式只保证返回合法 JSON，不保证字段一定完整，所以脚本会在本地检查字段、依赖和原始步骤编号。
