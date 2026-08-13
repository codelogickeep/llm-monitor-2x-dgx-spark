from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from storage import MonitorStore


class StorageSeriesTests(unittest.TestCase):
    def test_zero_throughput_is_not_stored_or_averaged(self) -> None:
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

                self.assertIsNone(zero_sample["prompt_tok_s"])
                self.assertIsNone(zero_sample["generation_tok_s"])

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
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
