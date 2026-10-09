"""用 DeepSeek 和菜谱证据补全设备调度配置。

输入是比赛提供的简化设备参数 JSON；输出保留全部原始设备参数，并增加
人工数量、资源类别、设备实例、容量、实例选择规则和并行条件。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import time
from pathlib import Path
from urllib import error, request


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = PROJECT_ROOT / "para" / "设备参数清单参考1.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "para" / "设备参数清单生成.json"
DEFAULT_RECIPES = Path(
    r"D:\study2\汇报\方太人工智能专项赛\方太人工智能专项赛\recipes_100.csv"
)
DEFAULT_BASE_URL = "https://api.deepseek.com"

SYSTEM_PROMPT = """你是厨房设备配置补全器。只输出一个完整合法的 JSON 对象，不要解释。
必须完整保留输入中的设备名称、参数、模式、温度范围和时长范围，只补充调度字段。
顶层增加人力资源，人数固定为1。
灶具固定为左灶眼、右灶眼，每个灶眼容量1；未指定时选最早空闲灶眼。
蒸箱和烤箱根据菜谱证据中的最高层号生成独立层位，每层容量1；原文指定层位时必须使用指定层位，否则选最早空闲层位。不同菜并行必须模式和温度相同；烤箱加湿烤还要求湿度相同。
烟机和冰箱是后台资源，不阻塞其他任务且容量不限。烟机忽略强弱档；冰箱拆成冷藏区、冷冻区、解冻区，按模式和温度选择分区，并记录放入和取出。
洗碗机和净咖一体机各只有一个实例，容量1。
每台设备增加资源类别、调度约束和一句简洁的推断说明。资源类别只能是：互斥制作资源、分层制作资源、后台资源。
"""


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def read_recipe_evidence(path: Path) -> list[str]:
    """只摘取与设备和层位有关的步骤，避免把100道菜全文发送给模型。"""

    evidence: list[str] = []
    pattern = re.compile(
        r"灶|蒸箱|烤箱|蒸烤|烹饪机|冰箱|烟机|洗碗机|净咖|"
        r"第\s*[一二三四五六七八九1-9]\s*层|上层|中层|中间层|下层"
    )
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            steps = re.split(r"；(?=第\s*\d+\s*步)", row.get("烹饪步骤", ""))
            for step in steps:
                if pattern.search(step):
                    evidence.append(f"{row.get('名称', '')}：{step.strip()}")
    return evidence


def infer_layer_count(evidence: list[str]) -> int:
    """层数使用菜谱中明确出现的最高设备层号；至少保留一层。"""

    chinese = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    highest = 1
    for text in evidence:
        for raw in re.findall(r"第\s*([一二三四五六七八九1-9])\s*层", text):
            highest = max(highest, int(raw) if raw.isdigit() else chinese[raw])
        if all(word in text for word in ("上层", "中层", "下层")):
            highest = max(highest, 3)
    return highest


def call_deepseek(source: dict, evidence: list[str], api_key: str, base_url: str, model: str) -> dict:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps({
                "device_catalog": source,
                "recipe_device_evidence": evidence,
                "observed_layer_count": infer_layer_count(evidence),
            }, ensure_ascii=False)},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.1,
        "max_tokens": 12000,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    while True:
        req = request.Request(
            base_url.rstrip("/") + "/chat/completions",
            data=body,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=120) as response:
                result = json.load(response)
            if result["choices"][0].get("finish_reason") == "length":
                raise ValueError("模型输出被截断")
            return json.loads(result["choices"][0]["message"]["content"])
        except (ValueError, KeyError, json.JSONDecodeError, error.URLError, error.HTTPError) as exc:
            print(f"设备配置生成失败，将在3秒后重试：{exc}")
            time.sleep(3)


def fixed_constraint(name: str, layer_count: int) -> tuple[str, dict]:
    """竞赛场景中的确定规则由程序兜底，避免模型随机改变。"""

    if name == "灶具":
        return "互斥制作资源", {
            "资源实例": ["左灶眼", "右灶眼"],
            "单实例最大同时菜数": 1,
            "实例选择规则": "原文指定灶眼时必须使用指定灶眼，否则选择最早空闲灶眼",
        }
    if name in ("蒸箱", "烤箱"):
        constraint = {
            "资源实例": [f"{name}第{index}层" for index in range(1, layer_count + 1)],
            "单实例最大同时菜数": 1,
            "实例选择规则": "原文指定层位时必须使用指定层位，否则选择最早空闲层位",
            "容量来源": f"菜谱中最高出现第{layer_count}层，推断至少{layer_count}层",
            "并行条件": {"模式": "相同", "温度": "相同"},
        }
        if name == "烤箱":
            constraint["模式附加并行条件"] = {"加湿烤": ["湿度相同"]}
        return "分层制作资源", constraint
    if name == "烟机":
        return "后台资源", {
            "资源实例": ["烟机"], "是否阻塞排程": False,
            "容量限制": None, "会话结束条件": "关闭烟机",
        }
    if name == "冰箱":
        return "后台资源", {
            "资源实例": ["冰箱冷藏区", "冰箱冷冻区", "冰箱解冻区"],
            "是否阻塞排程": False, "容量限制": None,
            "分区规则": {"冷藏": "冰箱冷藏区", "冷冻": "冰箱冷冻区", "解冻": "冰箱解冻区"},
        }
    return "互斥制作资源", {
        "资源实例": [name], "单实例最大同时菜数": 1,
    }


def finalize(source: dict, model_result: dict, layer_count: int) -> dict:
    """保留原始参数，合并模型说明，并用确定规则覆盖关键调度字段。"""

    proposed = {
        item.get("设备名称"): item
        for item in model_result.get("设备清单", [])
        if isinstance(item, dict)
    }
    devices: list[dict] = []
    for original in source["设备清单"]:
        name = original["设备名称"]
        category, constraint = fixed_constraint(name, layer_count)
        item = dict(original)
        item.update({
            "资源类别": category,
            "调度约束": constraint,
        })
        explanation = proposed.get(name, {}).get("推断说明")
        if isinstance(explanation, str) and explanation.strip():
            item["推断说明"] = explanation.strip()
        devices.append(item)
    return {"人力资源": {"人数": 1}, "设备清单": devices}


def main() -> int:
    parser = argparse.ArgumentParser(description="根据菜谱补全设备调度配置")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--recipes", type=Path, default=DEFAULT_RECIPES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise SystemExit("请先设置 DEEPSEEK_API_KEY")
    source = read_json(args.input)
    evidence = read_recipe_evidence(args.recipes)
    layer_count = infer_layer_count(evidence)
    result = call_deepseek(
        source,
        evidence,
        api_key,
        os.getenv("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL),
        os.getenv("DEEPSEEK_MODEL", "deepseek-flash"),
    )
    completed = finalize(source, result, layer_count)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(completed, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"设备配置已生成：{args.output}")
    print(f"根据菜谱推断蒸箱/烤箱至少 {layer_count} 层")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
