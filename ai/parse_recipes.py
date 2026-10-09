"""Prepare enriched recipe records from the competition CSV with DeepSeek.

Requires Python 3.10+. Uses only the standard library. API credentials are read
from environment variables and are never written to output files.
"""

from __future__ import annotations

import argparse
import csv
import http.client
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib import error, request


DEFAULT_CSV = Path(
    r"D:\study2\汇报\方太人工智能专项赛\方太人工智能专项赛\recipes_100.csv"
)
DEFAULT_OUTPUT = Path(__file__).resolve().parent / "parsed_recipes_v5"
DEFAULT_DEVICES = Path(__file__).resolve().parent.parent / "para" / "设备参数清单参考.json"
DEFAULT_BASE_URL = "https://api.deepseek.com"
SCHEMA_VERSION = 5
GENERIC_PREPARATION_RE = re.compile(
    r"^(?:准备(?:好|齐|所有|全部)?食材|备齐(?:所有|全部)?食材)$"
)
SAMPLE_NAMES = [
    "鸡翅包饭",
    "家常鲈鱼",
    "苹果芒果派",
    "低温牛排",
    "双椒鳙鱼头",
    "鸡仔饼",
    "半熟芝士蛋糕",
    "紫甘蓝沙拉",
    "麻辣对虾",
    "三味蒸鳗鱼&香茅烤鳗鱼同烹",
]

STEP_FIELDS = {
    "step_id", "source_step_no", "action", "device_raw", "device",
    "device_source", "mode_raw", "mode", "mode_source", "temperature_c",
    "temperature_source", "temperature_settings", "heat_level_raw", "heat_level", "humidity",
    "duration_min", "duration_source", "human_active_min",
    "human_active_source", "device_active_start_min", "device_active_min",
    "device_active_source", "human_intervals", "preheat", "depends_on",
    "requires_human", "can_run_unattended", "additional_device_usages",
}
HUMAN_INTERVAL_FIELDS = {"start_min", "duration_min", "source"}
ADDITIONAL_DEVICE_USAGE_FIELDS = {
    "device", "mode", "temperature_settings", "start_min", "duration_min", "action",
}
PREHEAT_FIELDS = {
    "required", "duration_min", "source", "whole_device_exclusive", "placement",
}
TEMPERATURE_SETTING_FIELDS = {"zone", "value_c", "source"}
TEMPERATURE_ZONES = {
    "整体", "上火", "下火", "锅内油温", "食材中心",
    "处理用水", "冷藏室", "冷冻室", "出水",
}
INGREDIENT_FIELDS = {"name", "amount_text", "quantity", "unit", "role", "category", "source_text"}
RESPONSE_FORMAT = {"type": "json_object"}
# 批量解析默认止损：同一道菜连续失败后跳过，避免无限消耗 API Token。
# 命令行可用 --max-attempts 调整；显式传 0 才表示不限次数。
DEFAULT_MAX_RESULT_ATTEMPTS = 3
RETRY_WAIT_SECONDS = 3
ENCLOSED_DEVICES = {"烤箱", "蒸箱", "冰箱"}
STOVE_RELEASE_PHRASES = (
    "离火", "关火待凉", "关火后倒入", "关火，倒入",
    "离火放凉", "离火后放凉", "离火静置", "离火后静置",
    "关火放凉", "关火后放凉", "关火静置", "关火后静置",
    "盛出后放凉", "盛出后静置",
)
LOAD_PHRASES = ("放入", "放进", "送入", "置入", "推进", "推入")
REMOVE_PHRASES = ("取出", "拿出", "移出", "端出")
RUN_PHRASES = ("开始烹饪", "选择开始", "蒸制", "蒸熟", "烘烤", "烤制")
SCREEN_OPERATION_PHRASES = (
    "按屏幕提示", "按屏幕操作", "开始烹饪按键", "选择开始", "按开始键",
    "开启电源", "打开电源",
)
MANUAL_ACTION_PHRASES = (
    "清洗", "洗净", "切", "剁", "去骨", "去皮", "搅拌", "翻炒", "煸炒",
    "擀", "揉", "包", "刷", "涂", "摆放", "装盘", "撒上", "浇在",
)
UNATTENDED_PHASE_PHRASES = (
    "腌", "浸泡", "泡发", "泡软", "静置", "松弛", "醒发", "放凉", "待凉",
    "冷却", "冷藏", "冷冻", "解冻", "发酵", "晾干", "风干",
    "蒸", "烤", "焖", "炖", "煮", "按屏幕提示",
)
CONTINUOUS_MANUAL_PHRASES = (
    "持续搅拌", "不停搅拌", "不断搅拌", "持续翻炒", "不停翻炒", "不断翻炒",
)
DEPENDENCY_REFERENCE_RE = re.compile(
    r"(?:烹饪|预热|腌制|发酵|冷藏|冷冻)?完成后|"
    r"(?:处理|切|洗|炒|煮|蒸|烤|打发|拌|调|腌)好(?:的|后)|"
    r"上述|剩下的|继续(?:烹饪|加热|蒸|烤)|再次放入"
)
STRONG_DEPENDENCY_REFERENCE_RE = re.compile(
    r"(?:烹饪|预热|腌制|发酵|冷藏|冷冻)完成后|"
    r"上述|剩下的|继续(?:烹饪|加热|蒸|烤)|再次放入"
)
MATERIAL_SPLIT_RE = re.compile(r"分(?:成|为).{0,8}(?:两|二|2|三|3|多)份")
OTHER_BRANCH_RE = re.compile(r"(?:另|另外|另一|其余|剩余)\s*[一二三四五六七八九\d]*\s*份")
IMPLICIT_PREHEATED_LOAD_RE = re.compile(
    r"(?:放入|放进|送入|推入|置入).{0,12}预热(?:好)?的(?:烤箱|蒸箱|设备)"
)

SYSTEM_PROMPT = """你是菜谱信息抽取器。只返回合法 JSON 对象，包含 ingredients 和 steps 两个数组，不要添加解释。
输出必须简洁：不要输出 Markdown、代码围栏、解释、推理过程、字段说明或重复原始菜谱；action 用一句简短中文概括该步骤的全部动作，不要堆砌无关修饰词；JSON 中只保留规定字段和值，确保一次性输出完整、可解析并以最后一个 } 结束。简洁不能导致步骤、动作、食材、设备参数、时间或依赖缺失。
ingredients 中每项必须且只能有：name, amount_text, quantity, unit, role, category, source_text。
name 是去掉用量后的食材名；amount_text 如“60克”“3个”“少许”；quantity 为可计算的数字，否则 null；unit 如“克”“个”，无法分解时 null；role 只能是“主料”或“辅料”；category 只能是“荤菜”“素菜”“调味品”；source_text 必须是原始食材清单中连续出现的原文片段，不能凭空改写。主料/辅料/A料/B料/C料等分组要理解后归类，不能遗漏食材。分组标签只保留原文中实际出现的位置：例如原文为“B料：大葱2段；生姜2片”，大葱的 source_text 可以是“B料：大葱2段”，但生姜的 source_text 必须是“生姜2片”，不能重复写成“B料：生姜2片”。不要把食谱步骤中提到但食材清单没有的食材凭空加入；同一种食材的常见名称差异不视为数据异常。
steps 中每项必须且只能有：step_id, source_step_no, action, device_raw, device, device_source, mode_raw, mode, mode_source, temperature_settings, heat_level_raw, heat_level, humidity, duration_min, duration_source, device_active_start_min, device_active_min, additional_device_usages, human_intervals, preheat, depends_on。不要输出 temperature_c、temperature_source、human_active_min、human_active_source、device_active_source、requires_human、can_run_unattended，这些派生字段由程序自动计算，以减少输出长度。
1. 每条输入步骤只输出一条解析步骤，按原顺序编号 s1、s2……，不得拆开或合并；action 概括该步骤全部动作。depends_on 只引用之前的 step_id，并按食材/中间产物的流转建立依赖：同一食材的“清洗→切配→腌制/焯水→正式烹饪”、以及“调好料汁→使用料汁”“预热完成→放入”“烹饪完成→取出”必须依赖产生该状态的前序步骤；用“处理好的、腌好的、炒好的、上述、剩下的、继续、再次”等表述时，也必须依赖对应生产步骤。不同食材的独立准备不要强加依赖，例如切素菜和切荤菜可以都不依赖对方。若某一步把同一食材明确“分成两份/多份”，之后“1份”和“另1份”是从该分份步骤产生的并行分支：两个分支都依赖分份步骤，但第二个分支不得错误依赖第一个分支；分支后续只依赖本分支自己的最近步骤。
2. device_raw/mode_raw/heat_level_raw 保留原文中明确出现的设备、模式、火力词语，未出现填 null。device 只能从用户提供的设备清单选择；“锅”“炒锅”可归为灶具，“蒸烤一体机”按实际蒸或烤功能归类，模糊名称选最合适的。device_source 为 explicit（原名相同）、normalized（原名不同但可映射）、inferred（依据动作推断）、unknown（无法判断）。无法合理映射时 device 为 null，不能硬编。
3. mode 只能是该设备清单里的模式/程序/档位之一；“常规烘焙”等别名应结合动作和参数选最合适的模式，mode_raw 保留原文。mode_source 为 explicit、normalized、estimated 或 unknown。temperature_settings 必须保留本步骤所有温度，每项只能含 zone、value_c、source；zone 只能是“整体”“上火”“下火”“锅内油温”“食材中心”“处理用水”“冷藏室”“冷冻室”“出水”，source 只能是 explicit 或 estimated。上下火温度不同必须分别输出，不得丢失其中一个。没有温度时 temperature_settings=[]；主要温度及来源由程序从第一项自动生成。原文仅描述“用90℃热水烫”等处理用水温度而未指定设备时，device=null、mode=null、heat_level=null，温度区域写“处理用水”，不得虚构净咖机或其它设备。烤箱、蒸箱正式运行，以及冰箱冷藏或冷冻时，即使原文没写温度也必须结合设备清单合理估计，不能留空；放入、取出设备等短暂操作可不填温度。对于灶具，温度可表示锅内油温或食材中心温度，不是灶具设定温度；只要 device=“灶具”且设备实际工作，heat_level 必须是 1～7 的整数，不得为 null。原文有“大火”“中火”等词时放入 heat_level_raw 并映射到 heat_level；原文没写火力时 heat_level_raw=null，但仍必须根据煮沸、爆炒、煎制等动作合理估计 heat_level。建议映射为微火1、小火2、中小火3、中火4、中大火5、大火6、猛火7。加湿烤的 humidity 为“低”“中”“高”，其他模式填 null。具有温控参数的设备，其设定温度必须符合清单范围。
4. duration_min 是从人开始该步骤到该步骤全部动作结束的总时长，必须是正数分钟。冷藏、冷冻、腌制等长时间准备也必须完整计入，例如24小时写1440分钟，不能截短或忽略。原文给出明确全过程时长时 duration_source=explicit；若只给出设备运行或等待时长，要另估设置/放入食材的人力时间，因此总时长来源为 estimated。原文未给出时长、只说“片刻”“按屏幕提示”等时，根据动作合理估计并用 estimated。不得把估计值标为 explicit。
5. human_intervals 精确描述人工在本步骤内的每段占用，每项只能含 start_min、duration_min、source，source 为 explicit 或 estimated。所有区间必须在0～duration_min内且互不重叠；人工总时长和人工相关派生字段由程序自动计算。若全程需人工（切配、持续翻炒），使用 [{"start_min":0,"duration_min":duration_min,"source":"explicit或estimated"}]。若只需人工启动，区间只覆盖开始操作。“切配后泡发/浸泡/腌制/静置，等待结束后再挤干或整形”仍是一条原始步骤，但必须用至少两段人工表示开始和收尾，数小时等待绝不能连续占用人工。若“启动预热→等待→预热完成后放入食材”在同一原始步骤内，也必须输出至少两段人工，例如0～1分钟启动、10～11分钟放入，不能把两段合并到开头，也不能占满中间等待。凡是“按屏幕提示/操作、按开始键、选择开始、开启电源、放入设备、取出食材、开关箱门”等动作，human_intervals 不能为空；按屏幕或按键操作还必须标记实际控制的设备。确实无须人参与时 human_intervals=[]。原始步骤仍然只输出一条，不因多段人工而拆分。
6. device_active_start_min 表示主设备资源从步骤开始后第几分钟开始被占用，device_active_min 是主设备无法分配给其他不兼容任务的总时长。没有主设备时 start=0、active=0；有主设备时 active 必须大于0，且 start+active 不得超过 duration_min，设备占用来源由程序自动生成。若同一原始步骤先后使用多个设备，device/device_active_* 记录最先使用或需要保持连续会话的主设备，其余设备必须放入 additional_device_usages；每项只能含 device、mode、temperature_settings、start_min、duration_min、action。例如“从烤箱取出，放凉后放入冰箱冷藏120分钟”应以烤箱为主设备，并为冰箱增加一项附加占用。没有附加设备时必须输出 []。灶具步骤只要没有明确写出“离火后放凉、关火后静置”等在设备外继续的阶段，设备占用就必须持续到步骤结束，即 device_active_start_min + device_active_min = duration_min；连续炒制、烧热食用油后浇油等全程人工操作通常还应从0开始占用。只有明确离火、关火后把食材倒入其它容器、放凉、冷却或冷藏时才能提前释放灶具；进入冰箱的起点必须不早于灶具释放时刻，不能让同一食材同时占用灶具和冰箱。“关火待凉后再搅拌”要在开头记录关火人工、等待期间释放人工和灶具、末尾再记录搅拌人工。“从冰箱取出后擀制”冰箱只按取出动作短时占用。食材或容器一旦放入烤箱/蒸箱，设备就已被占用，即使人还在关门、设置模式或按开始键，设备占用也可与人工占用重叠；如果步骤一开始就放入设备，或食材已在设备内，device_active_start_min=0，device_active_min 应覆盖设置和运行阶段，直到本步骤结束或明确取出。若放入、设置运行、取出分在相邻原始步骤，这些步骤必须标记同一设备，并形成直接依赖链。从实际放入设备的时刻起直到食材真正取出，设备连续占用。烟机和冰箱属于后台资源：仍要准确标记开启/放入、关闭/取出及持续时间，但可与其它菜和人工操作同时运行；烟机档位不影响排程，冰箱必须按冷藏、冷冻或解冻模式标记，以便排程图显示对应分区。
7. preheat 每项必须且只能含 required、duration_min、source、whole_device_exclusive、placement。无预热时填 {"required":false,"duration_min":0,"source":"not_applicable","whole_device_exclusive":false,"placement":"none"}。实际执行预热时 required=true、duration_min为正数、source为explicit或estimated、whole_device_exclusive=true，placement="within_step"；必须标记烤箱或蒸箱、工作模式、温度、正数步骤和设备占用时间。若原文只是“放入预热好的烤箱/推入预热的烤箱”，填required=true、合理估计duration_min、source="estimated"、whole_device_exclusive=true、placement="before_step"，不要把放入后的时间冒充预热时间，程序会在排程阶段补独立预热任务。“无需预热”必须使用无预热对象。预热期间默认独占整个箱体，不只是某一层。
8. 如需估计缺失的温度、模式、时长或设备占用，优先合理且保守，确保设备范围合法；原文中的明确数字、设备动作和依赖不可篡改。用户数据中有测试用设备别名，保留原文并另存归一化结果。
格式示例（仅示意，不作为待解析菜谱）：
{"ingredients":[{"name":"鸡翅","amount_text":"3个","quantity":3,"unit":"个","role":"主料","category":"荤菜","source_text":"鸡翅3个"}],"steps":[{"step_id":"s1","source_step_no":1,"action":"将鸡翅放入烤箱，设定上火180℃、下火165℃烤15分钟","device_raw":"烤箱","device":"烤箱","device_source":"explicit","mode_raw":"上下火","mode":"上下火","mode_source":"explicit","temperature_settings":[{"zone":"上火","value_c":180,"source":"explicit"},{"zone":"下火","value_c":165,"source":"explicit"}],"heat_level_raw":null,"heat_level":null,"humidity":null,"duration_min":16,"duration_source":"estimated","device_active_start_min":0,"device_active_min":16,"additional_device_usages":[],"human_intervals":[{"start_min":0,"duration_min":1,"source":"estimated"}],"preheat":{"required":false,"duration_min":0,"source":"not_applicable","whole_device_exclusive":false,"placement":"none"},"depends_on":[]}]}"""

STEP_RE = re.compile(r"第\s*(\d+)\s*步\s*[：:]")


def split_steps(text: str) -> list[dict]:
    matches = list(STEP_RE.finditer(text))
    if not matches:
        return [{"no": 1, "text": text.strip()}]
    parts = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        parts.append({"no": int(match.group(1)), "text": text[match.end() : end].strip(" ；;\n")})
    return parts


def read_recipes(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    required = {"菜谱id", "名称", "食材清单", "烹饪步骤"}
    if not rows or not required.issubset(rows[0]):
        raise ValueError(f"CSV 缺少字段：{sorted(required - set(rows[0] if rows else []))}")
    ids = [row["菜谱id"].strip() for row in rows]
    if any(not recipe_id for recipe_id in ids) or len(ids) != len(set(ids)):
        raise ValueError("CSV 中菜谱 ID 为空或重复")
    return rows


def select_recipes(rows: list[dict], sample: bool, limit: int) -> list[dict]:
    if sample:
        selected = []
        for name in SAMPLE_NAMES:
            row = next((item for item in rows if item["名称"] == name), None)
            if row is None:
                raise ValueError(f"找不到样本菜谱：{name}")
            selected.append(row)
        return selected[:limit]
    return rows[:limit]


def read_devices(path: Path) -> dict[str, dict]:
    catalog = json.loads(path.read_text(encoding="utf-8-sig"))
    devices = catalog.get("设备清单")
    if not isinstance(devices, list) or not devices:
        raise ValueError("设备参数文件缺少设备清单")
    by_name = {item["设备名称"]: item for item in devices}
    if len(by_name) != len(devices):
        raise ValueError("设备清单存在重复设备名称")
    return by_name


def call_deepseek(
    recipe: dict,
    source_steps: list[dict],
    devices: dict[str, dict],
    api_key: str,
    base_url: str,
    model: str,
    feedback: str | None = None,
    batch_context: dict | None = None,
) -> dict:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "name": recipe["名称"],
                    "ingredients_raw": recipe["食材清单"],
                    "source_steps": source_steps,
                    "device_catalog": list(devices.values()),
                    "batch_context": batch_context,
                },
                ensure_ascii=False,
            ),
        },
    ]
    if feedback:
        messages.append({"role": "user", "content": f"上次结果不符合校验要求：{feedback}。请重新输出完整 JSON 对象。"})
    payload = {
        "model": model,
        "messages": messages,
        "response_format": RESPONSE_FORMAT,
        "max_tokens": 16000,
        "temperature": 0.2,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    for attempt in range(3):
        try:
            with request.urlopen(req, timeout=120) as response:
                result = json.load(response)
            if result["choices"][0].get("finish_reason") == "length":
                raise ValueError("模型输出被截断")
            content = result["choices"][0]["message"]["content"]
            if not content:
                raise ValueError("模型返回空内容")
            return json.loads(content)
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            if exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"DeepSeek HTTP {exc.code}: {detail}") from exc
        except (
            http.client.IncompleteRead,
            http.client.RemoteDisconnected,
            TimeoutError,
            ConnectionError,
            error.URLError,
        ) as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"DeepSeek 响应传输中断：{exc}") from exc
    raise RuntimeError("DeepSeek 调用重试失败")


def call_deepseek_chunked(
    recipe: dict,
    source_steps: list[dict],
    devices: dict[str, dict],
    api_key: str,
    base_url: str,
    model: str,
    feedback: str | None = None,
    batch_size: int = 5,
) -> dict:
    """长菜谱分段输出，避免一个超长JSON被模型长度上限截断。"""

    if len(source_steps) <= batch_size:
        return call_deepseek(
            recipe, source_steps, devices, api_key, base_url, model, feedback
        )

    ingredients: list[dict] | None = None
    merged_steps: list[dict] = []
    for start in range(0, len(source_steps), batch_size):
        batch = source_steps[start:start + batch_size]
        expected_ids = [f"s{index + 1}" for index in range(start, start + len(batch))]
        context = {
            "is_chunked": True,
            "output_step_ids": expected_ids,
            "ingredient_rule": (
                "输出完整ingredients" if start == 0 else "ingredients必须输出空数组"
            ),
            "previous_steps": [
                {"step_id": item.get("step_id"), "action": item.get("action")}
                for item in merged_steps
            ],
            "instruction": (
                "只解析本批source_steps；steps数量必须等于本批输入数量，"
                "step_id依次严格使用output_step_ids中的全局编号。depends_on可以引用"
                "previous_steps中的全局编号。不得输出其它批次步骤。"
            ),
        }
        result = call_deepseek(
            recipe,
            batch,
            devices,
            api_key,
            base_url,
            model,
            feedback,
            batch_context=context,
        )
        batch_steps = result.get("steps")
        if not isinstance(batch_steps, list) or len(batch_steps) != len(batch):
            raise ValueError(
                f"分段解析步骤数错误：预期{len(batch)}，实际"
                f"{len(batch_steps) if isinstance(batch_steps, list) else '非数组'}"
            )
        for offset, step in enumerate(batch_steps):
            if not isinstance(step, dict):
                raise ValueError("分段解析返回了非对象步骤")
            step["step_id"] = expected_ids[offset]
            step["source_step_no"] = batch[offset]["no"]
        if start == 0:
            ingredients = result.get("ingredients")
        merged_steps.extend(batch_steps)
    return {"ingredients": ingredients, "steps": merged_steps}


def _number(value: object, label: str, minimum: float = 0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not (minimum <= value < float("inf")):
        raise ValueError(f"{label} 必须是大于等于 {minimum} 的有限数字")
    return float(value)


def _check_nullable_text(value: object, label: str) -> None:
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise ValueError(f"{label} 必须是非空字符串或 null")


def _contains_any(text: str, phrases: tuple[str, ...]) -> bool:
    """判断步骤文字中是否出现任一业务关键词。"""
    return any(phrase in text for phrase in phrases)


def _mentioned_enclosed_device(text: str) -> str | None:
    """从明确设备词中识别封闭式设备；模糊的“设备”仍交给模型判断。"""
    if "烤箱" in text:
        return "烤箱"
    if "蒸箱" in text or "蒸锅" in text:
        return "蒸箱"
    if "冰箱" in text:
        return "冰箱"
    return None


def _check_stove_occupancy(
    step: dict,
    source_text: str,
    duration: float,
    device_start: float,
    device_active: float,
) -> None:
    """灶具没有明确离火阶段时，必须一直占用到本步骤结束。"""
    if step["device"] != "灶具":
        return
    combined_text = f"{source_text} {step['action']}"
    if _contains_any(combined_text, STOVE_RELEASE_PHRASES):
        return
    if abs(device_start + device_active - duration) > 1e-9:
        raise ValueError(
            f"{step['step_id']} 的灶具步骤未说明离火，设备占用必须持续到步骤结束"
        )


def _dependency_ancestors(step_id: str, by_id: dict[str, dict]) -> set[str]:
    """返回一个步骤直接或间接依赖的全部前序步骤。"""

    ancestors: set[str] = set()
    stack = list(by_id[step_id]["depends_on"])
    while stack:
        dependency = stack.pop()
        if dependency in ancestors:
            continue
        ancestors.add(dependency)
        stack.extend(by_id[dependency]["depends_on"])
    return ancestors


def _is_generic_preparation_step(step: dict, source_text: str = "") -> bool:
    """识别只表示整道菜开始前备齐食材的通用准备步骤。"""

    action = re.sub(r"[，。；：、\s]+", "", str(step.get("action") or ""))
    source = re.sub(r"[，。；：、\s]+", "", source_text)
    return bool(
        GENERIC_PREPARATION_RE.fullmatch(action)
        or GENERIC_PREPARATION_RE.fullmatch(source)
    )


def _check_semantic_dependencies(
    steps: list[dict],
    source_steps: list[dict],
    ingredients: list[dict],
) -> None:
    """检查常见的食材流转依赖，避免结构合法但烹饪顺序错误。"""

    by_id = {step["step_id"]: step for step in steps}
    generic_preparation_ids = [
        step["step_id"]
        for index, step in enumerate(steps)
        if _is_generic_preparation_step(step, source_steps[index]["text"])
    ]
    for preparation_id in generic_preparation_ids:
        for step in steps:
            if step["step_id"] == preparation_id:
                continue
            if preparation_id not in _dependency_ancestors(step["step_id"], by_id):
                raise ValueError(
                    f"{step['step_id']} 必须直接或间接依赖通用准备步骤{preparation_id}"
                )
    # 调味品经常在不同支路分别取用，不能因为都出现“盐/油”就强制串行。
    material_names = {
        item["name"]
        for item in ingredients
        if item["category"] != "调味品" and len(item["name"].strip()) >= 2
    }
    latest_material_step: dict[str, str] = {}
    split_material_step: dict[str, str] = {}

    for index, step in enumerate(steps):
        combined_text = f"{source_steps[index]['text']} {step['action']}"
        ancestors = _dependency_ancestors(step["step_id"], by_id)

        mentioned = {name for name in material_names if name in combined_text}
        has_previous_material = any(name in latest_material_step for name in mentioned)

        # “切好的五花肉”可能是原菜谱直接提供的预处理状态，前文没有对应
        # 制作步骤时不能随意依赖上一条。只有确有同一食材前序，或文字明确
        # 指向前序结果时，才要求 depends_on。
        if (
            index > 0
            and DEPENDENCY_REFERENCE_RE.search(combined_text)
            and not step["depends_on"]
            and (
                has_previous_material
                or STRONG_DEPENDENCY_REFERENCE_RE.search(combined_text)
            )
        ):
            raise ValueError(
                f"{step['step_id']} 引用了前序处理结果或继续操作，必须填写对应 depends_on"
            )

        for name in mentioned:
            previous_step = latest_material_step.get(name)
            split_step = split_material_step.get(name)
            is_parallel_branch = bool(
                split_step
                and split_step in ancestors
                and OTHER_BRANCH_RE.search(combined_text)
            )
            if previous_step and previous_step not in ancestors and not is_parallel_branch:
                raise ValueError(
                    f"{step['step_id']} 继续处理同一食材“{name}”，必须依赖前序步骤{previous_step}"
                )
            latest_material_step[name] = step["step_id"]
            if MATERIAL_SPLIT_RE.search(combined_text):
                split_material_step[name] = step["step_id"]


def _check_human_intervention(
    step: dict,
    source_text: str,
    duration: float,
    human: float,
    device_active: float,
) -> None:
    """把明显需要人的动作变成硬校验，避免模型把人工错误释放。"""

    combined_text = f"{source_text} {step['action']}"
    needs_touch = _contains_any(combined_text, LOAD_PHRASES + REMOVE_PHRASES)
    screen_operation = _contains_any(combined_text, SCREEN_OPERATION_PHRASES)
    if (needs_touch or screen_operation) and human <= 0:
        raise ValueError(f"{step['step_id']} 含人工操作，human_active_min 必须大于0")
    if screen_operation and (step["device"] is None or device_active <= 0):
        raise ValueError(
            f"{step['step_id']} 含按屏幕/按键/通电操作，必须标记设备及正数设备占用时长"
        )
    preheat = step["preheat"]
    if (
        preheat["required"]
        and preheat["placement"] == "within_step"
        and needs_touch
        and duration > preheat["duration_min"] + 1e-9
    ):
        intervals = step["human_intervals"]
        if len(intervals) < 2:
            raise ValueError(
                f"{step['step_id']} 同一步内包含启动预热和预热后放入，必须使用至少两段 human_intervals"
            )
        if max(item["start_min"] for item in intervals) < preheat["duration_min"] - 1e-9:
            raise ValueError(
                f"{step['step_id']} 缺少预热完成后的人工放入区间"
            )

    manual_action = _contains_any(combined_text, MANUAL_ACTION_PHRASES)
    has_unattended_phase = _contains_any(combined_text, UNATTENDED_PHASE_PHRASES)
    if (
        duration > 30
        and has_unattended_phase
        and not _contains_any(combined_text, CONTINUOUS_MANUAL_PHRASES)
        and any(item["duration_min"] > 30 for item in step["human_intervals"])
    ):
        raise ValueError(
            f"{step['step_id']} 含长时间等待，不能用超过30分钟的连续人工区间"
        )
    if manual_action and not has_unattended_phase and human < duration - 1e-9:
        raise ValueError(
            f"{step['step_id']} 是持续人工步骤，human_active_min 必须等于 duration_min"
        )


def _check_preheat_step(
    step: dict,
    source_text: str,
    human: float,
    device_start: float,
    device_active: float,
) -> None:
    """实际执行预热的步骤必须具有真实温度、时长和设备占用。"""

    combined_text = f"{source_text} {step['action']}"
    if "无需预热" in combined_text or "预热" not in combined_text:
        return
    # “放入预热好的烤箱”只说明之前应有预热，不把当前放入步骤伪装成预热。
    if IMPLICIT_PREHEATED_LOAD_RE.search(combined_text):
        return
    if step["device"] not in ("烤箱", "蒸箱"):
        raise ValueError(f"{step['step_id']} 是预热步骤，必须标记烤箱或蒸箱")
    if step["temperature_c"] is None or not step["temperature_settings"]:
        raise ValueError(f"{step['step_id']} 是预热步骤，必须提供工作温度")
    if human <= 0:
        raise ValueError(f"{step['step_id']} 启动预热需要人工操作")
    if device_start != 0 or device_active <= 0:
        raise ValueError(f"{step['step_id']} 的预热必须从步骤开始占用设备并具有正数时长")


def _inferred_preheat(step: dict, source_text: str) -> dict:
    """为旧版JSON推断新预热字段，保证升级后仍可读取已有数据。"""

    combined_text = f"{source_text} {step.get('action', '')}"
    empty = {
        "required": False,
        "duration_min": 0,
        "source": "not_applicable",
        "whole_device_exclusive": False,
        "placement": "none",
    }
    if "无需预热" in combined_text or "预热" not in combined_text:
        return empty
    placement = "before_step" if IMPLICIT_PREHEATED_LOAD_RE.search(combined_text) else "within_step"
    match = re.search(
        r"预热[^\d]{0,8}(\d+(?:\.\d+)?)分钟|(\d+(?:\.\d+)?)分钟[^。；]{0,8}预热",
        combined_text,
    )
    explicit_value = next((value for value in match.groups() if value), None) if match else None
    preheat_min = float(explicit_value) if explicit_value else 10.0
    return {
        "required": True,
        "duration_min": preheat_min,
        "source": "explicit" if explicit_value else "estimated",
        "whole_device_exclusive": True,
        "placement": placement,
    }


def _normalise_derived_step_fields(step: dict) -> None:
    """补齐可由其它字段确定的值，避免让模型重复输出并浪费 Token。"""

    settings = step.get("temperature_settings")
    if "temperature_c" not in step or "temperature_source" not in step:
        first = settings[0] if isinstance(settings, list) and settings else None
        step["temperature_c"] = first.get("value_c") if isinstance(first, dict) else None
        step["temperature_source"] = (
            first.get("source", "unknown") if isinstance(first, dict) else "unknown"
        )

    intervals = step.get("human_intervals")
    if "human_active_min" not in step:
        if isinstance(intervals, list):
            step["human_active_min"] = sum(
                item.get("duration_min", 0)
                for item in intervals
                if isinstance(item, dict)
                and isinstance(item.get("duration_min"), (int, float))
                and not isinstance(item.get("duration_min"), bool)
            )
        else:
            step["human_active_min"] = 0
    if "human_active_source" not in step:
        interval_sources = [
            item.get("source") for item in intervals or [] if isinstance(item, dict)
        ] if isinstance(intervals, list) else []
        step["human_active_source"] = (
            "explicit" if interval_sources and all(value == "explicit" for value in interval_sources)
            else "estimated"
        )

    if "device_active_source" not in step:
        step["device_active_source"] = (
            "not_applicable" if step.get("device") is None else "estimated"
        )
    # 最终值会在完整校验中依据人工时长重新计算；这里先补字段以满足结构检查。
    step.setdefault("requires_human", False)
    step.setdefault("can_run_unattended", False)
    step.setdefault("additional_device_usages", [])


def _normalise_extended_step_fields(step: dict, source_text: str) -> None:
    """给旧版模型结果补默认扩展字段；新版结果仍接受严格校验。"""

    if "preheat" not in step:
        step["preheat"] = _inferred_preheat(step, source_text)
    if "human_intervals" in step:
        return
    human = step.get("human_active_min", 0)
    duration = step.get("duration_min", 0)
    if not isinstance(human, (int, float)) or human <= 0:
        step["human_intervals"] = []
        return

    preheat = step["preheat"]
    combined_text = f"{source_text} {step.get('action', '')}"
    if (
        preheat.get("required")
        and preheat.get("placement") == "within_step"
        and _contains_any(combined_text, LOAD_PHRASES)
        and human >= 2
        and duration >= human
    ):
        first_duration = 1.0
        last_duration = float(human) - first_duration
        step["human_intervals"] = [
            {"start_min": 0, "duration_min": first_duration, "source": "estimated"},
            {
                "start_min": float(duration) - last_duration,
                "duration_min": last_duration,
                "source": "estimated",
            },
        ]
    else:
        step["human_intervals"] = [{
            "start_min": 0,
            "duration_min": human,
            "source": step.get("human_active_source", "estimated"),
        }]


def _repair_material_dependencies(
    steps: list[dict],
    source_steps: list[dict],
    ingredients: list[dict],
) -> list[str]:
    """按食材流转补依赖，解决长菜谱分批解析时丢失跨批次前序的问题。"""

    material_names = {
        item["name"]
        for item in ingredients
        if isinstance(item, dict)
        and item.get("category") != "调味品"
        and isinstance(item.get("name"), str)
        and len(item["name"].strip()) >= 2
    }
    latest_material_step: dict[str, str] = {}
    split_material_step: dict[str, str] = {}
    previous_steps: dict[str, dict] = {}
    repairs: list[str] = []

    for index, step in enumerate(steps):
        if not isinstance(step, dict) or index >= len(source_steps):
            continue
        step_id = step.get("step_id")
        dependencies = step.get("depends_on")
        if not isinstance(step_id, str) or not isinstance(dependencies, list):
            continue

        # 这里只沿着已经出现过且格式正确的依赖向前查，避免错误模型输出触发 KeyError。
        ancestors: set[str] = set()
        stack = [item for item in dependencies if item in previous_steps]
        while stack:
            dependency = stack.pop()
            if dependency in ancestors:
                continue
            ancestors.add(dependency)
            stack.extend(
                item
                for item in previous_steps[dependency].get("depends_on", [])
                if item in previous_steps
            )

        combined_text = f"{source_steps[index]['text']} {step.get('action', '')}"
        mentioned = {name for name in material_names if name in combined_text}
        for name in mentioned:
            previous_id = latest_material_step.get(name)
            split_id = split_material_step.get(name)
            is_parallel_branch = bool(
                split_id
                and split_id in ancestors
                and OTHER_BRANCH_RE.search(combined_text)
            )
            if (
                previous_id
                and previous_id not in ancestors
                and previous_id not in dependencies
                and not is_parallel_branch
            ):
                dependencies.append(previous_id)
                ancestors.add(previous_id)
                repairs.append(f"{step_id} 补充同一食材{name}的前置依赖{previous_id}")
            latest_material_step[name] = step_id
            if MATERIAL_SPLIT_RE.search(combined_text):
                split_material_step[name] = step_id
        previous_steps[step_id] = step

    return repairs


def _repair_stove_release(step: dict, combined_text: str) -> str | None:
    """明确离火并进入等待/其它设备后，停止继续占用灶眼。"""

    if step.get("device") != "灶具":
        return None
    duration = step.get("duration_min")
    device_start = step.get("device_active_start_min")
    device_active = step.get("device_active_min")
    if any(
        isinstance(value, bool) or not isinstance(value, (int, float))
        for value in (duration, device_start, device_active)
    ):
        return None

    release_at: float | None = None
    # 同一步后半段已经转入冰箱等其它设备时，附加设备开始时主设备必须释放。
    extra_starts = [
        float(item["start_min"])
        for item in step.get("additional_device_usages", [])
        if isinstance(item, dict)
        and isinstance(item.get("start_min"), (int, float))
        and not isinstance(item.get("start_min"), bool)
        and float(item["start_min"]) > float(device_start)
    ]
    if "离火" in combined_text and extra_starts:
        release_at = min(extra_starts)

    # 步骤一开始关火，等待冷却后才再次人工搅拌：开头关火、末尾搅拌，
    # 中间等待不占人，也不再占用灶眼。
    if "关火待凉后" in combined_text:
        release_at = min(float(duration), 1.0)
        later_human = step.get("human_active_min", 2)
        if not isinstance(later_human, (int, float)) or isinstance(later_human, bool):
            later_human = 2
        later_human = min(max(float(later_human), 1.0), max(1.0, float(duration) - release_at))
        later_start = max(release_at, float(duration) - later_human)
        step["human_intervals"] = [
            {"start_min": 0, "duration_min": release_at, "source": "estimated"},
            {"start_min": later_start, "duration_min": later_human, "source": "estimated"},
        ]
        step.pop("human_active_min", None)
        step.pop("human_active_source", None)

    # “煮沸后关火，倒入碗中放凉”说明后续冷却已经离开灶具。
    if (
        "煮沸后关火" in combined_text
        and "倒入" in combined_text
        and ("放凉" in combined_text or "冷却" in combined_text)
    ):
        human = step.get("human_active_min", 1)
        if not isinstance(human, (int, float)) or isinstance(human, bool):
            human = 1
        release_at = min(float(duration), max(1.0, float(human)))

    if release_at is None:
        return None
    expected_active = max(0.0, release_at - float(device_start))
    if expected_active <= 0 or float(device_active) <= expected_active + 1e-9:
        return None
    step["device_active_min"] = expected_active
    step["device_active_source"] = "estimated"
    return f"{step.get('step_id')} 离火后在{release_at:g}分钟释放灶具"


def _repair_long_unattended_human(step: dict, combined_text: str) -> str | None:
    """把明显的长时间无人等待从连续人工中释放出来。

    原始菜谱步骤仍保持一条，只把人工拆成开始处理和等待结束后的收尾两段。
    这里只修复非常明确的异常：步骤超过30分钟、文字含无人等待阶段，并且
    模型把一段人工写成超过30分钟或把整步都标成了人工。
    """

    duration = step.get("duration_min")
    intervals = step.get("human_intervals")
    if (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or float(duration) <= 30
        or not isinstance(intervals, list)
        or not intervals
        or not _contains_any(combined_text, UNATTENDED_PHASE_PHRASES)
        or _contains_any(combined_text, CONTINUOUS_MANUAL_PHRASES)
    ):
        return None

    valid_intervals = [
        item for item in intervals
        if isinstance(item, dict)
        and isinstance(item.get("duration_min"), (int, float))
        and not isinstance(item.get("duration_min"), bool)
    ]
    if not valid_intervals:
        return None
    total_human = sum(float(item["duration_min"]) for item in valid_intervals)
    has_long_segment = any(float(item["duration_min"]) > 30 for item in valid_intervals)
    covers_whole_step = total_human >= float(duration) - 1e-9
    if not has_long_segment and not covers_whole_step:
        return None

    # 开始处理最多3分钟，等待结束后的挤干、整形等收尾最多7分钟。
    # 总人工不超过10分钟，避免把数小时等待再次算成人工工作。
    manual_total = min(10.0, max(2.0, float(duration) * 0.05))
    start_duration = min(3.0, manual_total / 2)
    end_duration = manual_total - start_duration
    end_start = float(duration) - end_duration
    if end_start < start_duration:
        return None
    step["human_intervals"] = [
        {"start_min": 0, "duration_min": start_duration, "source": "estimated"},
        {"start_min": end_start, "duration_min": end_duration, "source": "estimated"},
    ]
    # 这两个字段由 human_intervals 重新推导，防止保留旧的整步人工时长。
    step.pop("human_active_min", None)
    step.pop("human_active_source", None)
    return f"{step.get('step_id')} 长等待拆为开始和结束两段人工"


def _repair_deterministic_result(result: dict, source_steps: list[dict]) -> list[str]:
    """只修复能从明确业务规则唯一确定的字段，不猜测烹饪语义。"""

    steps = result.get("steps") if isinstance(result, dict) else None
    if not isinstance(steps, list):
        return []
    repairs: list[str] = []
    for index, step in enumerate(steps):
        if not isinstance(step, dict) or index >= len(source_steps):
            continue
        source_text = source_steps[index]["text"]
        combined_text = f"{source_text} {step.get('action', '')}"
        duration = step.get("duration_min")
        device_start = step.get("device_active_start_min")
        step.setdefault("additional_device_usages", [])

        # “90℃热水烫”等数值描述的是处理介质，不是某台设备的设定温度。
        # 保留这个工艺参数，但不为了通过校验而虚构净咖机或灶具。
        if (
            step.get("device") is None
            and ("热水" in combined_text or "水温" in combined_text)
            and step.get("temperature_settings")
        ):
            for setting in step["temperature_settings"]:
                if isinstance(setting, dict):
                    setting["zone"] = "处理用水"
            step["mode_raw"] = None
            step["mode"] = None
            step["mode_source"] = "unknown"
            step["heat_level_raw"] = None
            step["heat_level"] = None
            step["humidity"] = None
            step["device_active_start_min"] = 0
            step["device_active_min"] = 0
            step["device_active_source"] = "not_applicable"
            step.pop("temperature_c", None)
            step.pop("temperature_source", None)
            repairs.append(f"{step.get('step_id', index + 1)} 将热水温度标记为处理用水")

        # “取出水箱加水 + 开始烹饪”是蒸制设备的明确操作证据。
        if (
            step.get("device") is None
            and "水箱" in combined_text
            and _contains_any(combined_text, SCREEN_OPERATION_PHRASES)
        ):
            step["device"] = "蒸箱"
            step["device_raw"] = "设备" if "设备" in source_text else None
            step["device_source"] = "inferred"
            repairs.append(f"{step.get('step_id', index + 1)} 根据水箱操作识别为蒸箱")

        # 冷藏/冷冻温度是固定可用的保守默认值，不值得为缺失温度重调模型。
        if step.get("device") == "冰箱" and step.get("mode") in ("冷藏", "冷冻"):
            if not step.get("temperature_settings"):
                mode = step["mode"]
                step["temperature_settings"] = [{
                    "zone": "冷藏室" if mode == "冷藏" else "冷冻室",
                    "value_c": 4 if mode == "冷藏" else -18,
                    "source": "estimated",
                }]
                step.pop("temperature_c", None)
                step.pop("temperature_source", None)
                repairs.append(f"{step.get('step_id', index + 1)} 补充冰箱{mode}温度")

        stove_repair = _repair_stove_release(step, combined_text)
        if stove_repair:
            repairs.append(stove_repair)

        waiting_repair = _repair_long_unattended_human(step, combined_text)
        if waiting_repair:
            repairs.append(waiting_repair)

        # 纯手工且没有等待/设备自动运行阶段时，人工必须覆盖整个步骤。
        if (
            isinstance(duration, (int, float))
            and not isinstance(duration, bool)
            and _contains_any(combined_text, MANUAL_ACTION_PHRASES)
            and not _contains_any(combined_text, UNATTENDED_PHASE_PHRASES)
        ):
            expected_intervals = [{
                "start_min": 0,
                "duration_min": float(duration),
                "source": "estimated",
            }]
            if step.get("human_intervals") != expected_intervals:
                step["human_intervals"] = expected_intervals
                step["human_active_min"] = float(duration)
                step["human_active_source"] = "estimated"
                repairs.append(f"{step.get('step_id', index + 1)} 持续人工覆盖整个步骤")

        # 未写“离火”的灶具步骤必然占用到步骤结束，这是规则而不是模型猜测。
        if (
            step.get("device") == "灶具"
            and not _contains_any(combined_text, STOVE_RELEASE_PHRASES)
            and isinstance(duration, (int, float))
            and not isinstance(duration, bool)
            and isinstance(device_start, (int, float))
            and not isinstance(device_start, bool)
        ):
            expected = max(0.0, float(duration) - float(device_start))
            if step.get("device_active_min") != expected:
                step["device_active_min"] = expected
                step["device_active_source"] = "estimated"
                repairs.append(f"{step.get('step_id', index + 1)} 灶具占用补到步骤结束")

        if index == 0:
            continue
        previous = steps[index - 1]
        if not isinstance(previous, dict):
            continue
        previous_device = previous.get("device")
        text_without_water_tank = combined_text.replace("取出水箱", "")
        is_takeout = _contains_any(text_without_water_tank, REMOVE_PHRASES)

        # “烹饪结束后取出”沿用紧邻前一步的封闭设备，并直接依赖该步骤。
        if is_takeout and previous_device in ENCLOSED_DEVICES:
            if step.get("device") != previous_device:
                old_device = step.get("device")
                if old_device is not None:
                    extra = {
                        "device": old_device,
                        "mode": step.get("mode"),
                        "temperature_settings": step.get("temperature_settings", []),
                        "start_min": step.get("device_active_start_min", 0),
                        "duration_min": step.get("device_active_min", 0),
                        "action": f"使用{old_device}：{step.get('action', '')}",
                    }
                    if (
                        isinstance(extra["duration_min"], (int, float))
                        and extra["duration_min"] > 0
                        and extra not in step["additional_device_usages"]
                    ):
                        step["additional_device_usages"].append(extra)
                        repairs.append(
                            f"{step.get('step_id', index + 1)} 保留附加设备{old_device}"
                        )
                step["device"] = previous_device
                step["device_raw"] = (
                    previous_device if previous_device in source_text else None
                )
                step["device_source"] = (
                    "explicit" if previous_device in source_text else "inferred"
                )
                step["mode_raw"] = None
                step["mode"] = None
                step["mode_source"] = "unknown"
                step["temperature_c"] = None
                step["temperature_source"] = "unknown"
                step["temperature_settings"] = []
                step["heat_level_raw"] = None
                step["heat_level"] = None
                step["humidity"] = None
                step["device_active_start_min"] = 0
                first_human = next(iter(step.get("human_intervals") or []), {})
                takeout_min = first_human.get("duration_min", 1)
                if isinstance(duration, (int, float)) and not isinstance(duration, bool):
                    step["device_active_min"] = min(float(duration), float(takeout_min))
                repairs.append(f"{step.get('step_id', index + 1)} 取出步骤沿用{previous_device}")
            dependencies = step.get("depends_on")
            previous_id = previous.get("step_id")
            if isinstance(dependencies, list) and previous_id and previous_id not in dependencies:
                dependencies.append(previous_id)
                repairs.append(f"{step.get('step_id', index + 1)} 补充前置依赖{previous_id}")
            if isinstance(duration, (int, float)) and not isinstance(duration, bool):
                step["device_active_start_min"] = 0
                if not isinstance(step.get("device_active_min"), (int, float)) or step["device_active_min"] <= 0:
                    step["device_active_min"] = float(duration)
                step["device_active_source"] = "estimated"

        # 食材已经在同一封闭设备中时，后续运行或取出从步骤起点继续占用。
        elif previous_device in ENCLOSED_DEVICES and step.get("device") == previous_device:
            if step.get("device_active_start_min") != 0:
                step["device_active_start_min"] = 0
                repairs.append(f"{step.get('step_id', index + 1)} 连续设备占用起点修正为0")

        for usage in step["additional_device_usages"]:
            if usage.get("device") == "冰箱" and usage.get("mode") in ("冷藏", "冷冻"):
                if not usage.get("temperature_settings"):
                    mode = usage["mode"]
                    usage["temperature_settings"] = [{
                        "zone": "冷藏室" if mode == "冷藏" else "冷冻室",
                        "value_c": 4 if mode == "冷藏" else -18,
                        "source": "estimated",
                    }]
                    repairs.append(
                        f"{step.get('step_id', index + 1)} 补充附加冰箱{mode}温度"
                    )
    # “准备食材/准备所有食材”不是可以放在任意时刻的独立步骤。它必须
    # 成为所有后续并行分支的共同祖先；只给当前根步骤补直接依赖，后续
    # 步骤通过传递关系自然继承，避免制造不必要的串行依赖。
    for prep_index, preparation in enumerate(steps):
        if not isinstance(preparation, dict) or prep_index >= len(source_steps):
            continue
        if not _is_generic_preparation_step(
            preparation, source_steps[prep_index]["text"]
        ):
            continue
        preparation_id = preparation.get("step_id")
        if not preparation_id:
            continue
        for step in steps[prep_index + 1:]:
            if not isinstance(step, dict):
                continue
            dependencies = step.get("depends_on")
            if isinstance(dependencies, list) and not dependencies:
                dependencies.append(preparation_id)
                repairs.append(
                    f"{step.get('step_id')} 补充通用准备步骤依赖{preparation_id}"
                )

    ingredients = result.get("ingredients")
    if isinstance(ingredients, list):
        repairs.extend(_repair_material_dependencies(steps, source_steps, ingredients))
    return repairs


def _check_human_intervals_and_preheat(step: dict, duration: float, human: float) -> None:
    """校验多段人工区间和预热元数据之间的一致性。"""

    intervals = step["human_intervals"]
    if not isinstance(intervals, list):
        raise ValueError(f"{step['step_id']} 的 human_intervals 必须是数组")
    checked: list[tuple[float, float]] = []
    total = 0.0
    for item in intervals:
        if not isinstance(item, dict) or set(item) != HUMAN_INTERVAL_FIELDS:
            raise ValueError(f"{step['step_id']} 的人工区间字段不符合格式：{item}")
        start = _number(item["start_min"], f"{step['step_id']} 人工区间起点")
        active = _number(item["duration_min"], f"{step['step_id']} 人工区间时长", minimum=0.01)
        if item["source"] not in ("explicit", "estimated"):
            raise ValueError(f"{step['step_id']} 的人工区间来源无效")
        if start + active > duration + 1e-9:
            raise ValueError(f"{step['step_id']} 的人工区间超过步骤总时长")
        if checked and start < checked[-1][1] - 1e-9:
            raise ValueError(f"{step['step_id']} 的人工区间重叠或未按时间排序")
        checked.append((start, start + active))
        total += active
    if abs(total - human) > 1e-9:
        raise ValueError(f"{step['step_id']} 的 human_intervals 总时长必须等于 human_active_min")

    preheat = step["preheat"]
    if not isinstance(preheat, dict) or set(preheat) != PREHEAT_FIELDS:
        raise ValueError(f"{step['step_id']} 的 preheat 字段不符合格式")
    if not isinstance(preheat["required"], bool) or not isinstance(preheat["whole_device_exclusive"], bool):
        raise ValueError(f"{step['step_id']} 的预热布尔字段无效")
    preheat_min = _number(preheat["duration_min"], f"{step['step_id']} 预热时长")
    if preheat["placement"] not in ("none", "within_step", "before_step"):
        raise ValueError(f"{step['step_id']} 的预热位置无效")
    if preheat["required"]:
        if preheat_min <= 0 or preheat["source"] not in ("explicit", "estimated"):
            raise ValueError(f"{step['step_id']} 需要预热时必须填写正数时长和有效来源")
        if not preheat["whole_device_exclusive"] or preheat["placement"] == "none":
            raise ValueError(f"{step['step_id']} 预热必须整机独占并注明发生位置")
        if preheat["placement"] == "within_step" and preheat_min > duration + 1e-9:
            raise ValueError(f"{step['step_id']} 的步骤内预热时长超过步骤总时长")
    else:
        if preheat_min != 0 or preheat["source"] != "not_applicable":
            raise ValueError(f"{step['step_id']} 无预热时预热时长必须为0且来源为not_applicable")
        if preheat["whole_device_exclusive"] or preheat["placement"] != "none":
            raise ValueError(f"{step['step_id']} 无预热时不能设置整机独占或预热位置")


def _check_additional_device_usages(
    step: dict,
    duration: float,
    devices: dict[str, dict],
) -> None:
    """校验同一原始步骤内主设备之外的其它设备占用。"""

    usages = step["additional_device_usages"]
    if not isinstance(usages, list):
        raise ValueError(f"{step['step_id']} 的 additional_device_usages 必须是数组")
    for usage in usages:
        if not isinstance(usage, dict) or set(usage) != ADDITIONAL_DEVICE_USAGE_FIELDS:
            raise ValueError(f"{step['step_id']} 的附加设备占用字段不符合格式：{usage}")
        device = usage["device"]
        if device not in devices:
            raise ValueError(f"{step['step_id']} 的附加设备不在清单中：{device}")
        if not isinstance(usage["action"], str) or not usage["action"].strip():
            raise ValueError(f"{step['step_id']} 的附加设备动作不能为空")
        _check_nullable_text(usage["mode"], "附加设备模式")
        start = _number(usage["start_min"], "附加设备占用起点")
        active = _number(usage["duration_min"], "附加设备占用时长", minimum=0.01)
        if start + active > duration + 1e-9:
            raise ValueError(f"{step['step_id']} 的附加设备占用超过步骤总时长")
        settings = usage["temperature_settings"]
        if not isinstance(settings, list):
            raise ValueError(f"{step['step_id']} 的附加设备温度必须是数组")
        for setting in settings:
            if not isinstance(setting, dict) or set(setting) != TEMPERATURE_SETTING_FIELDS:
                raise ValueError(f"{step['step_id']} 的附加设备温度字段不符合格式")
            if setting["zone"] not in TEMPERATURE_ZONES:
                raise ValueError(f"{step['step_id']} 的附加设备温区无效")
            _number(setting["value_c"], "附加设备温度", minimum=-273.15)
            if setting["source"] not in ("explicit", "estimated"):
                raise ValueError(f"{step['step_id']} 的附加设备温度来源无效")

        first = settings[0] if settings else None
        pseudo_step = {
            "device": device,
            "mode": usage["mode"],
            "temperature_c": first["value_c"] if first else None,
            "temperature_settings": settings,
            "heat_level": None,
            "humidity": None,
            "device_active_min": active,
        }
        _check_device_parameters(pseudo_step, devices)


def _check_device_sessions(steps: list[dict], source_steps: list[dict]) -> None:
    """保证相邻的放入、运行、取出步骤能形成连续设备占用会话。"""
    for index, step in enumerate(steps):
        source_text = source_steps[index]["text"]
        combined_text = f"{source_text} {step['action']}"
        text_without_water_tank = combined_text.replace("取出水箱", "")
        is_load = _contains_any(combined_text, LOAD_PHRASES)
        is_remove = _contains_any(text_without_water_tank, REMOVE_PHRASES)
        is_run = _contains_any(combined_text, RUN_PHRASES)
        mentioned_device = _mentioned_enclosed_device(combined_text)

        all_step_devices = {
            step["device"],
            *(usage["device"] for usage in step["additional_device_usages"]),
        }
        if (is_load or is_remove) and mentioned_device and mentioned_device not in all_step_devices:
            raise ValueError(
                f"{step['step_id']} 明确操作{mentioned_device}，device 必须标记为同一设备"
            )

        if index == 0:
            continue
        previous = steps[index - 1]
        previous_device = previous["device"]

        # “烹饪结束后取出”可能不再重复写设备名，仍必须沿用上一设备。
        if is_remove and previous_device in ENCLOSED_DEVICES and step["device"] != previous_device:
            raise ValueError(
                f"{step['step_id']} 是取出步骤，device 必须沿用前一步的{previous_device}"
            )

        continues_previous_session = (
            previous_device in ENCLOSED_DEVICES
            and step["device"] == previous_device
            and (is_run or is_remove or previous["device"] == step["device"])
        )
        if not continues_previous_session:
            continue

        if previous["step_id"] not in step["depends_on"]:
            raise ValueError(
                f"{step['step_id']} 与前一步连续使用{previous_device}，必须直接依赖{previous['step_id']}"
            )
        if step["device_active_start_min"] != 0:
            raise ValueError(
                f"{step['step_id']} 开始时食材已在{previous_device}内，设备占用起点必须为0"
            )


def _check_device_parameters(step: dict, devices: dict[str, dict]) -> None:
    name, mode, temp = step["device"], step["mode"], step["temperature_c"]
    if name is None:
        process_temperatures = step["temperature_settings"]
        valid_process_temperature = (
            temp is not None
            and bool(process_temperatures)
            and all(
                item["zone"] in ("食材中心", "处理用水")
                for item in process_temperatures
            )
        )
        if (
            mode is not None
            or step["heat_level"] is not None
            or step["humidity"] is not None
            or (temp is not None and not valid_process_temperature)
            or (process_temperatures and not valid_process_temperature)
        ):
            raise ValueError("未识别设备时不能填写工作模式、温度或火力档位")
        return
    if name not in devices:
        raise ValueError(f"设备不在清单中：{name}")
    parameters = devices[name]["参数"]
    options = [option for param in parameters for option in param.get("可选值", [])]
    modes = {item if isinstance(item, str) else item["模式名"] for item in options}
    if mode is not None and mode not in modes:
        raise ValueError(f"{name} 不支持模式：{mode}")
    if name == "灶具":
        level = step["heat_level"]
        if type(level) is not int or not 1 <= level <= 7:
            raise ValueError("灶具工作步骤必须提供 1～7 档 heat_level；原文未写时也要合理估计")
    elif step["heat_level"] is not None:
        raise ValueError(f"{name} 不能使用灶具火力档位")
    if name == "烤箱" and mode == "加湿烤":
        if step["humidity"] not in ("低", "中", "高"):
            raise ValueError("加湿烤必须提供低/中/高湿度")
    elif step["humidity"] is not None:
        raise ValueError("只有加湿烤允许湿度字段")
    if name in ("烤箱", "蒸箱") and mode is not None:
        selected = next(item for item in options if isinstance(item, dict) and item["模式名"] == mode)
        low_time, high_time = (float(x) for x in re.findall(r"\d+(?:\.\d+)?", selected["烹饪时长"]))
        running_time = step["device_active_min"]
        if not low_time <= running_time <= high_time:
            raise ValueError(f"{name} {mode} 的运行时长 {running_time} 分钟超出 {low_time}～{high_time} 分钟")
    temperature_required = (
        (name in ("烤箱", "蒸箱") and mode is not None)
        or (name == "冰箱" and mode in ("冷藏", "冷冻"))
    )
    if temp is None and temperature_required:
        raise ValueError(f"{name} {mode} 正式运行时必须提供温度")
    if temp is None:
        return
    settings = step["temperature_settings"]
    if name == "灶具":
        # 灶具没有摄氏温度设定；这里的数值表示原文中的油温或食材温度。
        return
    if name in ("烤箱", "蒸箱"):
        candidate_modes = [item for item in options if isinstance(item, dict) and (mode is None or item["模式名"] == mode)]
        device_temperatures = [item["value_c"] for item in settings if item["zone"] in ("整体", "上火", "下火")]
        if not device_temperatures:
            raise ValueError(f"{name} 的温度必须标注为整体、上火或下火")
        for temperature in device_temperatures:
            if not any(
                (lambda bounds: bounds[0] <= temperature <= bounds[1])(
                    [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", item["烹饪温度"])]
                )
                for item in candidate_modes
            ):
                raise ValueError(f"{name} 的温度 {temperature}℃ 不符合设备参数")
        return
    if name == "冰箱" and mode in ("冷藏", "冷冻"):
        expected_zone = "冷藏室" if mode == "冷藏" else "冷冻室"
        low, high = (0, 10) if mode == "冷藏" else (-24, -15)
        for item in settings:
            if item["zone"] != expected_zone or type(item["value_c"]) is not int or not low <= item["value_c"] <= high:
                raise ValueError(f"冰箱{mode}温度设置无效：{item}")
        return
    if name == "净咖一体机":
        allowed = {int(x.rstrip("℃")) for x in parameters[0]["可选值"]}
        if all(item["zone"] == "出水" and item["value_c"] in allowed for item in settings):
            return
    raise ValueError(f"{name} 的温度设置不符合设备参数：{settings}")


def validate_and_wrap(recipe: dict, source_steps: list[dict], result: dict, devices: dict[str, dict]) -> dict:
    if not isinstance(result, dict):
        raise ValueError("模型返回的不是 JSON 对象")
    ingredients, steps = result.get("ingredients"), result.get("steps")
    if not isinstance(ingredients, list) or not ingredients:
        raise ValueError("模型未返回非空 ingredients 数组")
    if not isinstance(steps, list) or len(steps) != len(source_steps):
        raise ValueError(f"解析步骤数应与原始步骤数一致：预期 {len(source_steps)}，实际 {len(steps) if isinstance(steps, list) else '非数组'}")
    for item in ingredients:
        if not isinstance(item, dict) or set(item) != INGREDIENT_FIELDS:
            raise ValueError(f"食材字段不符合格式：{item}")
        for key in ("name", "amount_text", "source_text"):
            if not isinstance(item[key], str) or not item[key].strip():
                raise ValueError(f"食材 {key} 不能为空")
        if item["role"] not in ("主料", "辅料") or item["category"] not in ("荤菜", "素菜", "调味品"):
            raise ValueError(f"食材角色或类别无效：{item['name']}")
        _check_nullable_text(item["unit"], "食材单位")
        if item["quantity"] is not None:
            _number(item["quantity"], "食材用量", minimum=0)
        if item["source_text"] not in recipe["食材清单"]:
            raise ValueError(f"食材原文不在清单中：{item['source_text']}")
    seen = set()
    for index, step in enumerate(steps):
        if isinstance(step, dict):
            _normalise_derived_step_fields(step)
            _normalise_extended_step_fields(step, source_steps[index]["text"])
        if not isinstance(step, dict) or set(step) != STEP_FIELDS:
            raise ValueError(f"步骤字段不符合格式：{step}")
        step_id = step["step_id"]
        if step_id != f"s{index + 1}" or type(step["source_step_no"]) is not int or step["source_step_no"] != source_steps[index]["no"]:
            raise ValueError(f"步骤未与原始顺序一一对应：{step_id}")
        if not isinstance(step["action"], str) or not step["action"].strip():
            raise ValueError(f"步骤动作为空：{step_id}")
        deps = step["depends_on"]
        if not isinstance(deps, list) or any(not isinstance(dep, str) for dep in deps) or len(deps) != len(set(deps)) or any(dep not in seen for dep in deps):
            raise ValueError(f"依赖步骤不存在、重复或出现在后面：{step_id}")
        for key in ("device_raw", "device", "mode_raw", "mode", "heat_level_raw", "humidity"):
            _check_nullable_text(step[key], key)
        for key, allowed in (
            ("device_source", ("explicit", "normalized", "inferred", "unknown")),
            ("mode_source", ("explicit", "normalized", "estimated", "unknown")),
            ("temperature_source", ("explicit", "estimated", "unknown")),
            ("duration_source", ("explicit", "estimated")),
            ("human_active_source", ("explicit", "estimated")),
            ("device_active_source", ("explicit", "estimated", "not_applicable")),
        ):
            if step[key] not in allowed:
                raise ValueError(f"{step_id} 的 {key} 无效：{step[key]}")
        if step["device"] is None and step["device_source"] != "unknown":
            raise ValueError(f"{step_id} 未识别设备时 device_source 应为 unknown")
        if step["mode"] is None and step["mode_source"] != "unknown":
            raise ValueError(f"{step_id} 未识别模式时 mode_source 应为 unknown")
        if step["temperature_c"] is None and step["temperature_source"] != "unknown":
            raise ValueError(f"{step_id} 未识别温度时 temperature_source 应为 unknown")
        temperature_settings = step["temperature_settings"]
        if not isinstance(temperature_settings, list):
            raise ValueError(f"{step_id} 的 temperature_settings 必须是数组")
        for setting in temperature_settings:
            if not isinstance(setting, dict) or set(setting) != TEMPERATURE_SETTING_FIELDS:
                raise ValueError(f"{step_id} 的温度设置字段不符合格式：{setting}")
            if setting["zone"] not in TEMPERATURE_ZONES:
                raise ValueError(f"{step_id} 的温度区域无效：{setting['zone']}")
            _number(setting["value_c"], f"{step_id} 的温度", minimum=-273.15)
            if setting["source"] not in ("explicit", "estimated"):
                raise ValueError(f"{step_id} 的温度来源无效：{setting['source']}")
        if step["temperature_c"] is None:
            if temperature_settings:
                raise ValueError(f"{step_id} 没有主要温度时 temperature_settings 应为空")
        else:
            _number(step["temperature_c"], f"{step_id} 的主要温度", minimum=-273.15)
            if not any(
                setting["value_c"] == step["temperature_c"]
                and setting["source"] == step["temperature_source"]
                for setting in temperature_settings
            ):
                raise ValueError(f"{step_id} 的主要温度必须对应 temperature_settings 中的一项")
        if any(not isinstance(step[key], bool) for key in ("requires_human", "can_run_unattended")):
            raise ValueError(f"人工参与字段必须为布尔值：{step_id}")
        duration = _number(step["duration_min"], "步骤时长", minimum=0.01)
        human = _number(step["human_active_min"], "人工占用时长")
        if human > duration:
            raise ValueError(f"{step_id} 的人工占用时长超过步骤总时长")
        _check_human_intervals_and_preheat(step, duration, human)
        # 这两个字段由时长关系确定，程序统一计算，避免模型返回矛盾值。
        step["requires_human"] = human > 0
        step["can_run_unattended"] = human < duration
        device_start = _number(step["device_active_start_min"], "设备占用起点")
        device_active = _number(step["device_active_min"], "设备占用时长")
        if step["device"] is None:
            if device_start != 0 or device_active != 0 or step["device_active_source"] != "not_applicable":
                raise ValueError(f"{step_id} 没有设备时设备占用必须为 0 且来源为 not_applicable")
        else:
            if device_active <= 0 or step["device_active_source"] == "not_applicable":
                raise ValueError(f"{step_id} 使用设备时必须填写正数设备占用时长及有效来源")
            if device_start + device_active > duration:
                raise ValueError(f"{step_id} 的设备占用区间超过步骤总时长")
        _check_stove_occupancy(
            step,
            source_steps[index]["text"],
            duration,
            device_start,
            device_active,
        )
        _check_human_intervention(
            step,
            source_steps[index]["text"],
            duration,
            human,
            device_active,
        )
        _check_preheat_step(
            step,
            source_steps[index]["text"],
            human,
            device_start,
            device_active,
        )
        _check_device_parameters(step, devices)
        _check_additional_device_usages(step, duration, devices)
        step["source_text"] = source_steps[index]["text"]
        seen.add(step_id)
    _check_device_sessions(steps, source_steps)
    _check_semantic_dependencies(steps, source_steps, ingredients)
    return {
        "schema_version": SCHEMA_VERSION,
        "recipe_id": recipe["菜谱id"],
        "name": recipe["名称"],
        "ingredients_raw": recipe["食材清单"],
        "ingredients": ingredients,
        "source_steps": source_steps,
        "steps": steps,
    }


def _write_json_atomic(path: Path, data: dict) -> None:
    """校验成功后再原子替换文件，重解析失败时保留旧结果。"""

    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def migrate_existing_recipes(
    rows: list[dict],
    output_dir: Path,
    devices: dict[str, dict],
) -> int:
    """不调用模型，把已有 JSON 升级到当前字段和确定性业务规则。"""

    failures: list[dict[str, str]] = []
    existing_rows = [
        row for row in rows
        if (output_dir / f"{row['菜谱id']}.json").exists()
    ]
    print(f"本地迁移 {len(existing_rows)} 个已有菜谱，不调用 DeepSeek。")
    for index, row in enumerate(existing_rows, start=1):
        path = output_dir / f"{row['菜谱id']}.json"
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
            source_steps = split_steps(row["烹饪步骤"])
            result = {
                "ingredients": stored["ingredients"],
                "steps": stored["steps"],
            }
            for step_index, step in enumerate(result["steps"]):
                step.pop("source_text", None)
                if step_index < len(source_steps):
                    _normalise_extended_step_fields(
                        step, source_steps[step_index]["text"]
                    )
            repairs = _repair_deterministic_result(result, source_steps)
            wrapped = validate_and_wrap(row, source_steps, result, devices)
            _write_json_atomic(path, wrapped)
            suffix = f"：{'；'.join(repairs)}" if repairs else ""
            print(f"[{index}/{len(existing_rows)}] 完成：{row['名称']}{suffix}")
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            failures.append({
                "recipe_id": row["菜谱id"],
                "name": row["名称"],
                "error": str(exc),
            })
            print(
                f"[{index}/{len(existing_rows)}] 迁移失败：{row['名称']}：{exc}",
                file=sys.stderr,
            )

    failure_manifest = output_dir / "failed_recipes.json"
    _write_json_atomic(failure_manifest, failures)
    if failures:
        print(f"本地迁移有 {len(failures)} 道失败。", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="解析菜谱 CSV 为内部 JSON")
    parser.add_argument("--input", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--devices", type=Path, default=DEFAULT_DEVICES, help="设备参数清单 JSON")
    parser.add_argument("--sample", action="store_true", help="使用选定的 10 道代表性菜谱")
    parser.add_argument(
        "--start",
        type=int,
        default=1,
        help="从第几道开始处理（从1计数），用于中断后继续，默认1",
    )
    parser.add_argument("--limit", type=int, default=1, help="本次最多处理多少道；先用 1 测试")
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=DEFAULT_MAX_RESULT_ATTEMPTS,
        help="每道菜最多请求次数，默认3；0表示不限次数",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=5,
        help="长菜谱每次最多让模型输出多少个步骤，默认5",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="重新解析并覆盖已存在的同名 JSON；新结果校验成功后才替换旧文件",
    )
    parser.add_argument(
        "--retry-failures",
        action="store_true",
        help="只重试输出目录 failed_recipes.json 中记录的失败菜谱",
    )
    parser.add_argument(
        "--retry-missing",
        action="store_true",
        help="只解析输出目录中尚无对应JSON的菜谱",
    )
    parser.add_argument(
        "--migrate-existing",
        action="store_true",
        help="本地补齐并重新校验全部已有JSON，不调用DeepSeek",
    )
    parser.add_argument("--dry-run", action="store_true", help="只检查输入，不发起 API 请求")
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit 必须大于 0")
    if args.start < 1:
        parser.error("--start 必须大于 0")
    if args.max_attempts < 0:
        parser.error("--max-attempts 不能小于 0")
    if args.batch_size < 1:
        parser.error("--batch-size 必须大于 0")
    selection_modes = sum((
        args.retry_failures,
        args.retry_missing,
        args.migrate_existing,
    ))
    if selection_modes > 1:
        parser.error("--retry-failures、--retry-missing、--migrate-existing 不能同时使用")
    if args.migrate_existing and args.dry_run:
        parser.error("--migrate-existing 不能和 --dry-run 同时使用")

    rows = read_recipes(args.input)
    devices = read_devices(args.devices)
    args.output.mkdir(parents=True, exist_ok=True)
    candidates = select_recipes(rows, args.sample, len(SAMPLE_NAMES) if args.sample else len(rows))
    failure_manifest = args.output / "failed_recipes.json"
    if args.migrate_existing:
        return migrate_existing_recipes(candidates, args.output, devices)
    if args.retry_failures:
        if not failure_manifest.exists():
            raise SystemExit(f"失败清单不存在：{failure_manifest}")
        previous_failures = json.loads(failure_manifest.read_text(encoding="utf-8"))
        failed_ids = {
            str(item["recipe_id"])
            for item in previous_failures
            if isinstance(item, dict) and item.get("recipe_id")
        }
        selected = [row for row in candidates if row["菜谱id"] in failed_ids]
    elif args.retry_missing:
        selected = [
            row for row in candidates
            if not (args.output / f"{row['菜谱id']}.json").exists()
        ]
    else:
        selected = candidates[args.start - 1:args.start - 1 + args.limit]
    if not selected:
        if args.retry_failures or args.retry_missing:
            raise SystemExit("没有需要重试的菜谱")
        raise SystemExit(f"从第 {args.start} 道开始没有可处理的菜谱")
    print(f"已读取 {len(rows)} 道菜，本次选择 {len(selected)} 道：")
    for row in selected:
        print(f"  {row['名称']} ({row['菜谱id']})，原始步骤 {len(split_steps(row['烹饪步骤']))} 条")
    if args.dry_run:
        return 0

    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise SystemExit("请先设置环境变量 DEEPSEEK_API_KEY；不要把密钥写进脚本。")
    base_url = os.getenv("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL)
    model = os.getenv("DEEPSEEK_MODEL", "deepseek-flash")
    failures: list[dict[str, str]] = []
    for index, row in enumerate(selected, start=1):
        output_path = args.output / f"{row['菜谱id']}.json"
        if output_path.exists() and not args.overwrite:
            existing = json.loads(output_path.read_text(encoding="utf-8"))
            if existing.get("schema_version") != SCHEMA_VERSION:
                raise SystemExit(f"旧格式文件已存在：{output_path}；请指定新的 --output 目录，避免覆盖旧结果。")
            print(f"[{index}/{len(selected)}] 已存在，跳过：{row['名称']}")
            continue
        if output_path.exists():
            print(f"[{index}/{len(selected)}] 重新解析，校验成功后覆盖：{row['名称']}")
        source_steps = split_steps(row["烹饪步骤"])
        try:
            feedback = None
            attempt = 0
            while args.max_attempts == 0 or attempt < args.max_attempts:
                attempt += 1
                try:
                    result = call_deepseek_chunked(
                        row,
                        source_steps,
                        devices,
                        api_key,
                        base_url,
                        model,
                        feedback,
                        batch_size=args.batch_size,
                    )
                    repairs = _repair_deterministic_result(result, source_steps)
                    if repairs:
                        print(
                            f"[{index}/{len(selected)}] 本地自动修正：{'；'.join(repairs)}"
                        )
                    wrapped = validate_and_wrap(row, source_steps, result, devices)
                    break
                except (ValueError, KeyError, RuntimeError, error.URLError) as exc:
                    if "模型输出被截断" in str(exc):
                        feedback = (
                            "上次输出被长度限制截断。请立即重新输出完整 JSON；只输出 JSON，"
                            "不要解释或重复原文，action 使用简短句子，保留全部字段，"
                            "并确保最后以完整的 } 结束。"
                        )
                    else:
                        feedback = str(exc)
                    if args.max_attempts == 0:
                        attempt_text = f"第 {attempt} 次，继续重试"
                    else:
                        attempt_text = f"第 {attempt}/{args.max_attempts} 次"
                    print(
                        f"[{index}/{len(selected)}] 当前结果失败，{attempt_text}：{feedback}",
                        file=sys.stderr,
                    )
                    if args.max_attempts != 0 and attempt >= args.max_attempts:
                        raise
                    time.sleep(RETRY_WAIT_SECONDS)
            _write_json_atomic(output_path, wrapped)
        except (ValueError, KeyError, RuntimeError, error.URLError) as exc:
            print(f"[{index}/{len(selected)}] 失败：{row['名称']}：{exc}", file=sys.stderr)
            failures.append({
                "recipe_id": row["菜谱id"],
                "name": row["名称"],
                "error": str(exc),
            })
            continue
        print(f"[{index}/{len(selected)}] 完成：{row['名称']} → {output_path.name}")
    _write_json_atomic(failure_manifest, failures)
    if failures:
        print(f"本次有 {len(failures)} 道菜失败，其余菜谱已继续处理：", file=sys.stderr)
        for item in failures:
            print(f"  {item['name']}：{item['error']}", file=sys.stderr)
        print(f"失败清单已保存：{failure_manifest}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
