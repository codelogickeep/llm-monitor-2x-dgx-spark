from __future__ import annotations

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import build_vllm_snapshot  # noqa: E402


METRICS = """
vllm:prompt_tokens_total{model_name="model"} 1000
vllm:prompt_tokens_cached_total{model_name="model"} 900
vllm:generation_tokens_total{model_name="model"} 500
vllm:request_success_total{finished_reason="stop",model_name="model"} 4
vllm:time_to_first_token_seconds_sum{model_name="model"} 8
vllm:time_to_first_token_seconds_count{model_name="model"} 4
vllm:e2e_request_latency_seconds_sum{model_name="model"} 40
vllm:e2e_request_latency_seconds_count{model_name="model"} 4
vllm:num_requests_running{model_name="model"} 0
vllm:num_requests_waiting{model_name="model"} 0
vllm:kv_cache_usage_perc{model_name="model"} 0
"""


class VllmMetricTests(unittest.TestCase):
    def test_first_sample_does_not_report_cumulative_latency_as_recent(self) -> None:
        snapshot = build_vllm_snapshot(METRICS, None)

        self.assertIsNone(snapshot["ttft_avg_s"])
        self.assertIsNone(snapshot["e2e_avg_s"])
        self.assertEqual(snapshot["prompt_tok_s"], 0.0)
        self.assertEqual(snapshot["generation_tok_s"], 0.0)
        self.assertEqual(snapshot["request_s"], 0.0)


if __name__ == "__main__":
    unittest.main()
