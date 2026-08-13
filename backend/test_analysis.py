from __future__ import annotations

import time
import unittest

from analysis import analyze_windows


def build_dataset(
    window_seconds: int,
    *,
    memory_pct: float = 94.0,
    memory_psi: float = 0.0,
    swap_out_pages_s: float = 0.0,
    gpu_util: float = 0.0,
    roce_error_at_last_sample: float = 0.0,
) -> dict:
    now = time.time()
    rows = []
    vllm = []
    samples = int(window_seconds / 15) + 1
    for index in range(samples):
        ts = now - window_seconds + index * 15
        for node_id in ["be2c", "aa43"]:
            rows.append(
                {
                    "ts": ts,
                    "node_id": node_id,
                    "cpu_soc_temp_max_c": 54.0,
                    "gpu_temp_max_c": 65.0,
                    "nvme_temp_max_c": 53.0,
                    "nic_temp_max_c": 57.0,
                    "mem_used_pct": memory_pct,
                    "mem_available_mib": 7000.0,
                    "swap_used_pct": 18.0,
                    "swap_in_pages_s": 0.0,
                    "swap_out_pages_s": swap_out_pages_s,
                    "memory_psi_some_avg10": memory_psi,
                    "memory_psi_full_avg10": 0.0,
                    "gpu_util_avg_pct": gpu_util,
                    "gpu_sm_clock_pct": 80.1,
                    "gpu_throttle_active": 0.0,
                    "gpu_pstate_numeric": 0.0,
                    "power_total_w": 40.0,
                    "roce_link_up": 1.0,
                    "roce_link_speed_min_mbps": 200000.0,
                    "roce_error_delta": roce_error_at_last_sample if index == samples - 1 else 0.0,
                    "roce_rx_mbps": 100.0,
                    "roce_tx_mbps": 100.0,
                }
            )
        vllm.append(
            {
                "ts": ts,
                "running": 0.0,
                "waiting": 0.0,
                "generation_tok_s": 0.0,
                "prompt_tok_s": 0.0,
                "request_s": 0.0,
                "error_s": 0.0,
            }
        )
    return {"window_seconds": window_seconds, "nodes": rows, "vllm": vllm}


class AnalysisTests(unittest.TestCase):
    def analyze(self, short: dict) -> dict:
        datasets = {
            900: short,
            3600: build_dataset(3600),
            86400: build_dataset(86400),
        }
        return analyze_windows(datasets, 2.5)["windows"][0]

    def item(self, analysis: dict, item_id: str) -> dict:
        return next(item for item in analysis["items"] if item["id"] == item_id)

    def test_high_unified_memory_without_pressure_is_ok(self) -> None:
        result = self.analyze(build_dataset(900, memory_pct=99.0))
        self.assertEqual(self.item(result, "memory")["severity"], "ok")

    def test_sustained_memory_pressure_warns(self) -> None:
        result = self.analyze(build_dataset(900, memory_psi=20.0))
        self.assertEqual(self.item(result, "memory")["severity"], "warning")

    def test_single_roce_error_spike_does_not_warn(self) -> None:
        result = self.analyze(build_dataset(900, roce_error_at_last_sample=29.0))
        self.assertEqual(self.item(result, "roce")["severity"], "ok")

    def test_idle_gpu_does_not_claim_throttling(self) -> None:
        result = self.analyze(build_dataset(900, gpu_util=0.0))
        self.assertEqual(self.item(result, "throttling")["severity"], "idle")

    def test_migration_counter_spike_is_ignored(self) -> None:
        dataset = build_dataset(900)
        dataset["nodes"][0]["swap_in_pages_s"] = 4_955_307_000.0
        dataset["nodes"][0]["swap_out_pages_s"] = 9_120_599_000.0
        result = self.analyze(dataset)
        self.assertEqual(self.item(result, "memory")["severity"], "ok")


if __name__ == "__main__":
    unittest.main()
