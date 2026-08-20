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
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
