from __future__ import annotations

import tempfile
import time
import unittest
import sqlite3
from pathlib import Path

from storage import MonitorStore


class StorageSeriesTests(unittest.TestCase):
    def test_zero_throughput_is_stored_but_not_averaged_as_activity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MonitorStore(Path(directory) / "monitor.sqlite3")
            bucket_start = int((time.time() - 1800) / 900) * 900
            try:
                store.insert_snapshot(
                    {
                        "ts": bucket_start + 10,
                        "vllm": {
                            "prompt_tok_s": 0.0,
                            "generation_tok_s": 0.0,
                            "health": "ok",
                        },
                    }
                )
                store.insert_snapshot(
                    {
                        "ts": bucket_start + 20,
                        "vllm": {
                            "prompt_tok_s": 1200.0,
                            "generation_tok_s": 60.0,
                            "queue_avg_s": 0.0,
                            "health": "ok",
                        },
                    }
                )
                store.insert_snapshot(
                    {
                        "ts": bucket_start + 30,
                        "vllm": {
                            "prompt_tok_s": 600.0,
                            "generation_tok_s": 30.0,
                            "health": "ok",
                        },
                    }
                )

                with store.lock:
                    store.conn.execute(
                        """
                        INSERT INTO vllm_metrics (
                            ts, prompt_tok_s, generation_tok_s, health
                        ) VALUES (?, 0, 0, 'ok')
                        """,
                        (bucket_start + 5,),
                    )
                    store.conn.commit()
                    zero_sample = store.conn.execute(
                        "SELECT prompt_tok_s, generation_tok_s FROM vllm_metrics WHERE ts = ?",
                        (bucket_start + 10,),
                    ).fetchone()

                self.assertEqual(zero_sample["prompt_tok_s"], 0.0)
                self.assertEqual(zero_sample["generation_tok_s"], 0.0)

                raw_samples = store.series(
                    kind="vllm",
                    metric="generation_tok_s",
                    window_seconds=3600,
                    bucket_seconds=0,
                )
                sample_average = store.series(
                    kind="vllm",
                    metric="generation_tok_s",
                    window_seconds=3600,
                    bucket_seconds=900,
                )

                self.assertEqual(raw_samples["values"], [60.0, 30.0])
                self.assertEqual(sample_average["values"], [45.0])
                sampling = store.inference_sampling(3600)
                self.assertEqual(sampling["collected_samples"], 4)
                self.assertEqual(sampling["active_samples"], 2)
                stats = store.stats(3600)
                self.assertEqual(stats["vllm"]["queue_avg_s"]["count"], 1)
                self.assertEqual(stats["vllm"]["queue_avg_s"]["avg"], 0.0)
            finally:
                store.close()

    def test_legacy_database_is_migrated_without_rewriting_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite3"
            conn = sqlite3.connect(path)
            conn.executescript(
                """
                CREATE TABLE node_metrics (
                    id INTEGER PRIMARY KEY, ts REAL NOT NULL, node_id TEXT NOT NULL,
                    node_name TEXT NOT NULL, cpu_used_pct REAL, mem_used_pct REAL,
                    gpu_util_avg_pct REAL, gpu_temp_max_c REAL, power_total_w REAL,
                    roce_rx_mbps REAL, roce_tx_mbps REAL, probe_latency_ms REAL,
                    health TEXT NOT NULL
                );
                CREATE TABLE vllm_metrics (
                    id INTEGER PRIMARY KEY, ts REAL NOT NULL, running REAL, waiting REAL,
                    kv_cache_usage_pct REAL, prompt_tok_s REAL, generation_tok_s REAL,
                    request_s REAL, error_s REAL, ttft_avg_s REAL, e2e_avg_s REAL,
                    cache_hit_ratio_pct REAL, health TEXT NOT NULL
                );
                CREATE TABLE alerts (
                    id INTEGER PRIMARY KEY, first_ts REAL NOT NULL, last_ts REAL NOT NULL,
                    resolved_ts REAL, level TEXT NOT NULL, scope TEXT NOT NULL,
                    message TEXT NOT NULL, signature TEXT NOT NULL UNIQUE, health TEXT NOT NULL
                );
                INSERT INTO vllm_metrics (
                    ts, prompt_tok_s, generation_tok_s, health
                ) VALUES (1, NULL, 60, 'ok');
                """
            )
            conn.commit()
            conn.close()

            store = MonitorStore(path)
            try:
                with store.lock:
                    columns = {
                        row["name"] for row in store.conn.execute("PRAGMA table_info(vllm_metrics)")
                    }
                    row = store.conn.execute(
                        "SELECT prompt_tok_s, generation_tok_s, deployment_id, sample_state "
                        "FROM vllm_metrics WHERE ts = 1"
                    ).fetchone()
                self.assertIn("prefill_efficiency_tok_s", columns)
                self.assertIsNone(row["prompt_tok_s"])
                self.assertEqual(row["generation_tok_s"], 60.0)
                self.assertEqual(row["deployment_id"], "legacy")
                self.assertIsNone(row["sample_state"])
                node_columns = {
                    item["name"] for item in store.conn.execute("PRAGMA table_info(node_metrics)")
                }
                self.assertIn("deployment_id", node_columns)
            finally:
                store.close()

    def test_deployment_id_isolated_for_node_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MonitorStore(Path(directory) / "monitor.sqlite3")
            now = time.time()
            try:
                for offset, deployment_id, gpu_util in [
                    (20, "deepseek-v4-flash-prod", 20.0),
                    (10, "qwen3.8-flash-next-nvfp4-prod", 80.0),
                ]:
                    store.insert_snapshot(
                        {
                            "ts": now - offset,
                            "vllm": {"deployment_id": deployment_id, "health": "ok"},
                            "nodes": {
                                "node-1": {
                                    "name": "spark-1",
                                    "summary": {
                                        "gpu_util_avg_pct": gpu_util,
                                        "cpu_used_pct": gpu_util / 2,
                                    },
                                    "health": "ok",
                                }
                            },
                        }
                    )

                qwen_stats = store.stats(
                    3600,
                    deployment_id="qwen3.8-flash-next-nvfp4-prod",
                )
                self.assertEqual(
                    qwen_stats["nodes"]["node-1"]["gpu_util_avg_pct"]["avg"],
                    80.0,
                )
                qwen_series = store.series(
                    kind="node",
                    metric="gpu_util_avg_pct",
                    node_id="node-1",
                    window_seconds=3600,
                    deployment_id="qwen3.8-flash-next-nvfp4-prod",
                )
                self.assertEqual(qwen_series["values"], [80.0])
                self.assertEqual(qwen_series["deployment_id"], "qwen3.8-flash-next-nvfp4-prod")
                analysis = store.analysis_rows(
                    3600,
                    deployment_id="qwen3.8-flash-next-nvfp4-prod",
                )
                self.assertEqual({row["deployment_id"] for row in analysis["nodes"]}, {"qwen3.8-flash-next-nvfp4-prod"})
            finally:
                store.close()

    def test_deployment_id_isolated_across_vllm_queries_and_alerts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MonitorStore(Path(directory) / "monitor.sqlite3")
            now = time.time()
            try:
                for offset, deployment_id, generation_rate in [
                    (20, "deepseek-v4-flash-prod", 10.0),
                    (10, "qwen3.8-flash-next-nvfp4-prod", 100.0),
                ]:
                    store.insert_snapshot(
                        {
                            "ts": now - offset,
                            "vllm": {
                                "deployment_id": deployment_id,
                                "prompt_tok_s": generation_rate * 10,
                                "generation_tok_s": generation_rate,
                                "health": "ok",
                            },
                            "alerts": [
                                {
                                    "level": "warning",
                                    "scope": "vllm",
                                    "message": f"{deployment_id} warning",
                                    "signature": "vllm:test",
                                }
                            ],
                        }
                    )

                qwen_series = store.series(
                    kind="vllm",
                    metric="generation_tok_s",
                    window_seconds=3600,
                    deployment_id="qwen3.8-flash-next-nvfp4-prod",
                )
                self.assertEqual(qwen_series["values"], [100.0])
                deepseek_stats = store.stats(
                    3600,
                    deployment_id="deepseek-v4-flash-prod",
                )
                self.assertEqual(deepseek_stats["vllm"]["generation_tok_s"]["avg"], 10.0)
                self.assertEqual(
                    deepseek_stats["inference_sampling"]["total_samples"],
                    1,
                )
                recent = store.recent_vllm_samples(
                    3600,
                    deployment_id="qwen3.8-flash-next-nvfp4-prod",
                )
                self.assertEqual(recent["generation_tok_s"]["value"], 100.0)
                analysis = store.analysis_rows(
                    3600,
                    deployment_id="qwen3.8-flash-next-nvfp4-prod",
                )
                self.assertEqual(
                    {row["deployment_id"] for row in analysis["vllm"]},
                    {"qwen3.8-flash-next-nvfp4-prod"},
                )
                alerts = store.alerts(
                    3600,
                    deployment_id="qwen3.8-flash-next-nvfp4-prod",
                )
                self.assertEqual(len(alerts["items"]), 1)
                self.assertEqual(
                    alerts["items"][0]["deployment_id"],
                    "qwen3.8-flash-next-nvfp4-prod",
                )
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
