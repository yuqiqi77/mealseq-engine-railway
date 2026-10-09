"""设备配置补全的离线测试，不调用 DeepSeek。"""

from __future__ import annotations

import unittest

from enrich_devices import DEFAULT_INPUT, finalize, infer_layer_count, read_json


class DeviceEnrichmentTests(unittest.TestCase):
    def test_highest_explicit_device_layer_becomes_capacity(self) -> None:
        evidence = ["菜A：放入第1层", "菜B：烤盘放入机器第4层"]
        self.assertEqual(infer_layer_count(evidence), 4)

    def test_fixed_rules_preserve_original_parameters(self) -> None:
        source = read_json(DEFAULT_INPUT)
        completed = finalize(source, {"设备清单": []}, layer_count=4)
        devices = {item["设备名称"]: item for item in completed["设备清单"]}
        original = {item["设备名称"]: item for item in source["设备清单"]}

        self.assertEqual(completed["人力资源"]["人数"], 1)
        self.assertEqual(devices["烤箱"]["参数"], original["烤箱"]["参数"])
        self.assertEqual(len(devices["烤箱"]["调度约束"]["资源实例"]), 4)
        self.assertEqual(devices["灶具"]["调度约束"]["资源实例"], ["左灶眼", "右灶眼"])
        self.assertFalse(devices["冰箱"]["调度约束"]["是否阻塞排程"])
        self.assertEqual(devices["洗碗机"]["调度约束"]["单实例最大同时菜数"], 1)


if __name__ == "__main__":
    unittest.main()
