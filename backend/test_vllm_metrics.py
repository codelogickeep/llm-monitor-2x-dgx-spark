from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import AlertDebouncer, build_vllm_snapshot  # noqa: E402


def metrics(
    *,
    prompt: float = 1000,
    cached: float = 900,
    generation: float = 500,
    requests: float = 4,
    ttft_sum: float = 8,
    ttft_count: float = 4,
    e2e_sum: float = 40,
    e2e_count: float = 4,
    prefill_time: float = 2,
    decode_time: float = 10,
    prefill_tokens: float = 800,
    request_generation_tokens: float = 500,
    draft_tokens: float = 600,
    accepted_tokens: float = 480,
    running: float = 0,
) -> str:
    return f"""
vllm:prompt_tokens_total{{model_name="model"}} {prompt}
vllm:prompt_tokens_cached_total{{model_name="model"}} {cached}
vllm:generation_tokens_total{{model_name="model"}} {generation}
vllm:request_success_total{{finished_reason="stop",model_name="model"}} {requests}
vllm:request_success_total{{finished_reason="error",model_name="model"}} 0
vllm:request_success_total{{finished_reason="abort",model_name="model"}} 0
vllm:num_preemptions_total{{model_name="model"}} 0
vllm:spec_decode_num_draft_tokens_total{{model_name="model"}} {draft_tokens}
vllm:spec_decode_num_accepted_tokens_total{{model_name="model"}} {accepted_tokens}
vllm:time_to_first_token_seconds_sum{{model_name="model"}} {ttft_sum}
vllm:time_to_first_token_seconds_count{{model_name="model"}} {ttft_count}
vllm:e2e_request_latency_seconds_sum{{model_name="model"}} {e2e_sum}
vllm:e2e_request_latency_seconds_count{{model_name="model"}} {e2e_count}
vllm:request_queue_time_seconds_sum{{model_name="model"}} 1
vllm:request_queue_time_seconds_count{{model_name="model"}} {requests}
vllm:request_prefill_time_seconds_sum{{model_name="model"}} {prefill_time}
vllm:request_prefill_time_seconds_count{{model_name="model"}} {requests}
vllm:request_decode_time_seconds_sum{{model_name="model"}} {decode_time}
vllm:request_decode_time_seconds_count{{model_name="model"}} {requests}
vllm:inter_token_latency_seconds_sum{{model_name="model"}} 5
vllm:inter_token_latency_seconds_count{{model_name="model"}} 500
vllm:request_prompt_tokens_sum{{model_name="model"}} {prompt}
vllm:request_prompt_tokens_count{{model_name="model"}} {requests}
vllm:request_generation_tokens_sum{{model_name="model"}} {request_generation_tokens}
vllm:request_generation_tokens_count{{model_name="model"}} {requests}
vllm:request_prefill_kv_computed_tokens_sum{{model_name="model"}} {prefill_tokens}
vllm:request_prefill_kv_computed_tokens_count{{model_name="model"}} {requests}
vllm:num_requests_running{{model_name="model"}} {running}
vllm:num_requests_waiting{{model_name="model"}} 0
vllm:num_requests_waiting_by_reason{{model_name="model",reason="capacity"}} 0
vllm:num_requests_waiting_by_reason{{model_name="model",reason="deferred"}} 0
vllm:kv_cache_usage_perc{{model_name="model"}} 0
"""


class VllmMetricTests(unittest.TestCase):
    def test_first_sample_is_warmup_not_zero_activity(self) -> None:
        with patch("app.now", return_value=100.0):
            snapshot = build_vllm_snapshot(metrics(), None)

        self.assertEqual(snapshot["sample_state"], "warmup")
        self.assertEqual(snapshot["event_state"], "unknown")
        self.assertIsNone(snapshot["ttft_avg_s"])
        self.assertIsNone(snapshot["prompt_tok_s"])
        self.assertIsNone(snapshot["generation_tok_s"])

    def test_valid_idle_sample_preserves_zero_deltas(self) -> None:
        with patch("app.now", return_value=100.0):
            previous = build_vllm_snapshot(metrics(), None)
        with patch("app.now", return_value=102.5):
            snapshot = build_vllm_snapshot(metrics(), previous)

        self.assertEqual(snapshot["sample_state"], "ok")
        self.assertEqual(snapshot["event_state"], "idle")
        self.assertEqual(snapshot["prompt_tokens_delta"], 0.0)
        self.assertEqual(snapshot["prompt_tok_s"], 0.0)
        self.assertEqual(snapshot["generation_tok_s"], 0.0)
        self.assertIsNone(snapshot["ttft_avg_s"])

    def test_completion_uses_native_efficiency_histograms(self) -> None:
        with patch("app.now", return_value=100.0):
            previous = build_vllm_snapshot(metrics(), None)
        current_metrics = metrics(
            prompt=1250,
            cached=1050,
            generation=600,
            requests=5,
            ttft_sum=10,
            ttft_count=5,
            e2e_sum=50,
            e2e_count=5,
            prefill_time=2.5,
            decode_time=12,
            prefill_tokens=900,
            request_generation_tokens=600,
            draft_tokens=700,
            accepted_tokens=560,
        )
        with patch("app.now", return_value=102.5):
            snapshot = build_vllm_snapshot(current_metrics, previous)

        self.assertEqual(snapshot["event_state"], "completion_event")
        self.assertEqual(snapshot["prompt_tok_s"], 100.0)
        self.assertEqual(snapshot["generation_tok_s"], 40.0)
        self.assertEqual(snapshot["prefill_efficiency_tok_s"], 200.0)
        self.assertEqual(snapshot["decode_efficiency_tok_s"], 50.0)
        self.assertEqual(snapshot["mtp_acceptance_pct"], 80.0)

    def test_counter_reset_is_not_reported_as_zero(self) -> None:
        with patch("app.now", return_value=100.0):
            previous = build_vllm_snapshot(metrics(), None)
        with patch("app.now", return_value=102.5):
            snapshot = build_vllm_snapshot(metrics(prompt=10, cached=5, generation=2, requests=0), previous)

        self.assertEqual(snapshot["sample_state"], "counter_reset")
        self.assertTrue(snapshot["counter_reset"])
        self.assertIsNone(snapshot["prompt_tok_s"])
        self.assertIsNone(snapshot["generation_tokens_delta"])

    def test_missing_core_metric_is_explicit(self) -> None:
        broken = "\n".join(
            line for line in metrics().splitlines() if not line.startswith("vllm:generation_tokens_total")
        )
        with patch("app.now", return_value=100.0):
            snapshot = build_vllm_snapshot(broken, None)

        self.assertEqual(snapshot["sample_state"], "metrics_missing")
        self.assertIn("generation_tokens_total", snapshot["missing_metrics"])

        with patch("app.now", return_value=102.5):
            recovered = build_vllm_snapshot(metrics(), snapshot)
        self.assertEqual(recovered["sample_state"], "warmup")
        self.assertIsNone(recovered["generation_tok_s"])


class AlertDebouncerTests(unittest.TestCase):
    def test_warning_requires_sustained_violation_and_recovery(self) -> None:
        debouncer = AlertDebouncer()
        warning = {"level": "warning", "scope": "node", "message": "hot", "signature": "node:1:gpu_temp"}

        self.assertEqual(debouncer.update([warning], 0), [])
        self.assertEqual(debouncer.update([warning], 299), [])
        self.assertEqual(len(debouncer.update([warning], 300)), 1)
        self.assertEqual(len(debouncer.update([], 301)), 1)
        self.assertEqual(len(debouncer.update([], 600)), 1)
        self.assertEqual(debouncer.update([], 601), [])

    def test_offline_alert_is_immediate(self) -> None:
        debouncer = AlertDebouncer()
        offline = {"level": "critical", "scope": "node", "message": "offline", "signature": "node:1:offline"}
        self.assertEqual(len(debouncer.update([offline], 0)), 1)


if __name__ == "__main__":
    unittest.main()
