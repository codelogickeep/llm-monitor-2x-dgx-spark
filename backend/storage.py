from __future__ import annotations

import json
import math
import sqlite3
import statistics
import threading
import time
from pathlib import Path
from typing import Any


NODE_METRIC_COLUMNS = {
    "cpu_used_pct": "cpu_used_pct",
    "cpu_soc_temp_max_c": "cpu_soc_temp_max_c",
    "mem_used_pct": "mem_used_pct",
    "mem_available_mib": "mem_available_mib",
    "swap_used_pct": "swap_used_pct",
    "swap_in_pages_s": "swap_in_pages_s",
    "swap_out_pages_s": "swap_out_pages_s",
    "memory_psi_some_avg10": "memory_psi_some_avg10",
    "memory_psi_full_avg10": "memory_psi_full_avg10",
    "gpu_util_avg_pct": "gpu_util_avg_pct",
    "gpu_temp_max_c": "gpu_temp_max_c",
    "gpu_sm_clock_avg_mhz": "gpu_sm_clock_avg_mhz",
    "gpu_sm_clock_pct": "gpu_sm_clock_pct",
    "gpu_throttle_active": "gpu_throttle_active",
    "gpu_pstate_numeric": "gpu_pstate_numeric",
    "power_total_w": "power_total_w",
    "nvme_temp_max_c": "nvme_temp_max_c",
    "nic_temp_max_c": "nic_temp_max_c",
    "disk_used_pct": "disk_used_pct",
    "roce_rx_mbps": "roce_rx_mbps",
    "roce_tx_mbps": "roce_tx_mbps",
    "roce_error_delta": "roce_error_delta",
    "roce_link_speed_min_mbps": "roce_link_speed_min_mbps",
    "roce_link_up": "roce_link_up",
    "probe_latency_ms": "probe_latency_ms",
}

VLLM_METRIC_COLUMNS = {
    "running": "running",
    "waiting": "waiting",
    "waiting_capacity": "waiting_capacity",
    "waiting_deferred": "waiting_deferred",
    "kv_cache_usage_pct": "kv_cache_usage_pct",
    "prompt_tok_s": "prompt_tok_s",
    "cached_prompt_tok_s": "cached_prompt_tok_s",
    "uncached_prompt_tok_s": "uncached_prompt_tok_s",
    "generation_tok_s": "generation_tok_s",
    "request_s": "request_s",
    "error_s": "error_s",
    "ttft_avg_s": "ttft_avg_s",
    "e2e_avg_s": "e2e_avg_s",
    "queue_avg_s": "queue_avg_s",
    "prefill_avg_s": "prefill_avg_s",
    "decode_avg_s": "decode_avg_s",
    "itl_avg_s": "itl_avg_s",
    "cache_hit_ratio_pct": "cache_hit_ratio_pct",
    "request_prompt_tokens_avg": "request_prompt_tokens_avg",
    "request_generation_tokens_avg": "request_generation_tokens_avg",
    "prefill_efficiency_tok_s": "prefill_efficiency_tok_s",
    "decode_efficiency_tok_s": "decode_efficiency_tok_s",
    "mtp_acceptance_pct": "mtp_acceptance_pct",
    "preemption_delta": "preemption_delta",
}

VLLM_EVENT_METRICS = {
    "prompt_tok_s",
    "cached_prompt_tok_s",
    "uncached_prompt_tok_s",
    "generation_tok_s",
    "request_s",
    "error_s",
    "ttft_avg_s",
    "e2e_avg_s",
    "queue_avg_s",
    "prefill_avg_s",
    "decode_avg_s",
    "itl_avg_s",
    "cache_hit_ratio_pct",
    "request_prompt_tokens_avg",
    "request_generation_tokens_avg",
    "prefill_efficiency_tok_s",
    "decode_efficiency_tok_s",
    "mtp_acceptance_pct",
}

VLLM_POSITIVE_ACTIVITY_METRICS = {
    "prompt_tok_s",
    "cached_prompt_tok_s",
    "uncached_prompt_tok_s",
    "generation_tok_s",
    "request_s",
    "error_s",
}

VLLM_ANALYSIS_COLUMNS = {
    **VLLM_METRIC_COLUMNS,
    "interval_s": "interval_s",
    "counter_reset": "counter_reset",
    "prompt_tokens_delta": "prompt_tokens_delta",
    "cached_prompt_tokens_delta": "cached_prompt_tokens_delta",
    "uncached_prompt_tokens_delta": "uncached_prompt_tokens_delta",
    "generation_tokens_delta": "generation_tokens_delta",
    "request_completed_delta": "request_completed_delta",
    "request_error_delta": "request_error_delta",
    "request_abort_delta": "request_abort_delta",
    "mtp_draft_tokens_delta": "mtp_draft_tokens_delta",
    "mtp_accepted_tokens_delta": "mtp_accepted_tokens_delta",
}


def parse_duration_seconds(value: str | None, default_seconds: int) -> int:
    if not value:
        return default_seconds
    value = value.strip().lower()
    if value == "raw":
        return 0
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    try:
        if value[-1] in multipliers:
            return int(float(value[:-1]) * multipliers[value[-1]])
        return int(float(value))
    except ValueError:
        return default_seconds


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        value = float(value)
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    except (TypeError, ValueError):
        return None


def describe(values: list[float]) -> dict[str, float | int | None]:
    clean = [float(v) for v in values if v is not None]
    if not clean:
        return {
            "count": 0,
            "min": None,
            "max": None,
            "avg": None,
            "p95": None,
            "p99": None,
            "stddev": None,
        }
    clean.sort()
    count = len(clean)
    avg = statistics.fmean(clean)
    stddev = statistics.pstdev(clean) if count > 1 else 0.0

    def pct(p: float) -> float:
        idx = min(count - 1, max(0, int(round((count - 1) * p))))
        return clean[idx]

    return {
        "count": count,
        "min": clean[0],
        "max": clean[-1],
        "avg": avg,
        "p95": pct(0.95),
        "p99": pct(0.99),
        "stddev": stddev,
    }


class MonitorStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self.read_lock = threading.Lock()
        self.active_alerts: dict[str, int] = {}
        self._init_pragmas()
        self._init_schema()
        self.read_conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30.0)
        self.read_conn.row_factory = sqlite3.Row
        self.read_conn.execute("PRAGMA query_only=ON;")
        self.read_conn.execute("PRAGMA busy_timeout=30000;")
        self._load_active_alerts()

    def _init_pragmas(self) -> None:
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL;")
            self.conn.execute("PRAGMA synchronous=NORMAL;")
            self.conn.execute("PRAGMA temp_store=MEMORY;")
            self.conn.execute("PRAGMA cache_size=-20000;")

    def _init_schema(self) -> None:
        with self.lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS node_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    node_id TEXT NOT NULL,
                    node_name TEXT NOT NULL,
                    cpu_used_pct REAL,
                    mem_used_pct REAL,
                    gpu_util_avg_pct REAL,
                    gpu_temp_max_c REAL,
                    power_total_w REAL,
                    roce_rx_mbps REAL,
                    roce_tx_mbps REAL,
                    probe_latency_ms REAL,
                    health TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_node_metrics_node_ts ON node_metrics(node_id, ts);
                CREATE INDEX IF NOT EXISTS idx_node_metrics_ts ON node_metrics(ts);

                CREATE TABLE IF NOT EXISTS vllm_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    deployment_id TEXT NOT NULL DEFAULT 'default',
                    sample_state TEXT,
                    event_state TEXT,
                    interval_s REAL,
                    counter_reset INTEGER NOT NULL DEFAULT 0,
                    missing_metrics TEXT,
                    running REAL,
                    waiting REAL,
                    waiting_capacity REAL,
                    waiting_deferred REAL,
                    kv_cache_usage_pct REAL,
                    prompt_tokens_delta REAL,
                    cached_prompt_tokens_delta REAL,
                    uncached_prompt_tokens_delta REAL,
                    generation_tokens_delta REAL,
                    request_completed_delta REAL,
                    request_error_delta REAL,
                    request_abort_delta REAL,
                    prompt_tok_s REAL,
                    cached_prompt_tok_s REAL,
                    uncached_prompt_tok_s REAL,
                    generation_tok_s REAL,
                    request_s REAL,
                    error_s REAL,
                    preemption_delta REAL,
                    mtp_draft_tokens_delta REAL,
                    mtp_accepted_tokens_delta REAL,
                    mtp_acceptance_pct REAL,
                    ttft_avg_s REAL,
                    e2e_avg_s REAL,
                    queue_avg_s REAL,
                    prefill_avg_s REAL,
                    decode_avg_s REAL,
                    itl_avg_s REAL,
                    cache_hit_ratio_pct REAL,
                    request_prompt_tokens_avg REAL,
                    request_generation_tokens_avg REAL,
                    prefill_efficiency_tok_s REAL,
                    decode_efficiency_tok_s REAL,
                    health TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_vllm_metrics_ts ON vllm_metrics(ts);

                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    first_ts REAL NOT NULL,
                    last_ts REAL NOT NULL,
                    resolved_ts REAL,
                    level TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    message TEXT NOT NULL,
                    base_key TEXT NOT NULL DEFAULT '',
                    signature TEXT NOT NULL UNIQUE,
                    health TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_alerts_first_ts ON alerts(first_ts);
                CREATE INDEX IF NOT EXISTS idx_alerts_last_ts ON alerts(last_ts);
                CREATE INDEX IF NOT EXISTS idx_alerts_resolved_ts ON alerts(resolved_ts);
                """
            )
            node_columns = {
                row["name"]
                for row in self.conn.execute("PRAGMA table_info(node_metrics)").fetchall()
            }
            node_migrations = {
                "cpu_soc_temp_max_c": "REAL",
                "mem_available_mib": "REAL",
                "swap_used_pct": "REAL",
                "swap_in_pages_s": "REAL",
                "swap_out_pages_s": "REAL",
                "memory_psi_some_avg10": "REAL",
                "memory_psi_full_avg10": "REAL",
                "gpu_sm_clock_avg_mhz": "REAL",
                "gpu_sm_clock_pct": "REAL",
                "gpu_throttle_active": "REAL",
                "gpu_pstate_numeric": "REAL",
                "nvme_temp_max_c": "REAL",
                "nic_temp_max_c": "REAL",
                "disk_used_pct": "REAL",
                "roce_error_delta": "REAL",
                "roce_link_speed_min_mbps": "REAL",
                "roce_link_up": "REAL",
            }
            for column, column_type in node_migrations.items():
                if column not in node_columns:
                    self.conn.execute(f"ALTER TABLE node_metrics ADD COLUMN {column} {column_type}")
            vllm_columns = {
                row["name"]
                for row in self.conn.execute("PRAGMA table_info(vllm_metrics)").fetchall()
            }
            vllm_migrations = {
                "deployment_id": "TEXT NOT NULL DEFAULT 'legacy'",
                "sample_state": "TEXT",
                "event_state": "TEXT",
                "interval_s": "REAL",
                "counter_reset": "INTEGER NOT NULL DEFAULT 0",
                "missing_metrics": "TEXT",
                "waiting_capacity": "REAL",
                "waiting_deferred": "REAL",
                "prompt_tokens_delta": "REAL",
                "cached_prompt_tokens_delta": "REAL",
                "uncached_prompt_tokens_delta": "REAL",
                "generation_tokens_delta": "REAL",
                "request_completed_delta": "REAL",
                "request_error_delta": "REAL",
                "request_abort_delta": "REAL",
                "cached_prompt_tok_s": "REAL",
                "uncached_prompt_tok_s": "REAL",
                "preemption_delta": "REAL",
                "mtp_draft_tokens_delta": "REAL",
                "mtp_accepted_tokens_delta": "REAL",
                "mtp_acceptance_pct": "REAL",
                "queue_avg_s": "REAL",
                "prefill_avg_s": "REAL",
                "decode_avg_s": "REAL",
                "itl_avg_s": "REAL",
                "request_prompt_tokens_avg": "REAL",
                "request_generation_tokens_avg": "REAL",
                "prefill_efficiency_tok_s": "REAL",
                "decode_efficiency_tok_s": "REAL",
            }
            for column, column_type in vllm_migrations.items():
                if column not in vllm_columns:
                    self.conn.execute(f"ALTER TABLE vllm_metrics ADD COLUMN {column} {column_type}")
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_vllm_metrics_state_ts "
                "ON vllm_metrics(sample_state, event_state, ts)"
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_vllm_metrics_deployment_ts "
                "ON vllm_metrics(deployment_id, ts)"
            )
            columns = {
                row["name"]
                for row in self.conn.execute("PRAGMA table_info(alerts)").fetchall()
            }
            if "base_key" not in columns:
                self.conn.execute("ALTER TABLE alerts ADD COLUMN base_key TEXT NOT NULL DEFAULT ''")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_base_key ON alerts(base_key)")
            self.conn.commit()

    def _load_active_alerts(self) -> None:
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, base_key, scope, level, message FROM alerts WHERE resolved_ts IS NULL"
            ).fetchall()
        self.active_alerts = {
            (row["base_key"] or f"{row['scope']}|{row['level']}|{row['message']}"): int(row["id"])
            for row in rows
        }

    def close(self) -> None:
        with self.read_lock:
            self.read_conn.close()
        with self.lock:
            self.conn.close()

    def insert_snapshot(self, snapshot: dict[str, Any]) -> None:
        ts = float(snapshot.get("ts") or time.time())
        nodes = snapshot.get("nodes") or {}
        vllm = snapshot.get("vllm") or {}
        alerts = snapshot.get("alerts") or []

        with self.lock:
            cur = self.conn.cursor()
            for node_id, node in nodes.items():
                summary = node.get("summary") or {}
                memory = node.get("memory") or {}
                pressure = memory.get("pressure") or {}
                disk_root = node.get("disk_root") or {}
                cur.execute(
                    """
                    INSERT INTO node_metrics (
                        ts, node_id, node_name, cpu_used_pct, cpu_soc_temp_max_c,
                        mem_used_pct, mem_available_mib, swap_used_pct,
                        swap_in_pages_s, swap_out_pages_s,
                        memory_psi_some_avg10, memory_psi_full_avg10,
                        gpu_util_avg_pct, gpu_temp_max_c, gpu_sm_clock_avg_mhz,
                        gpu_sm_clock_pct, gpu_throttle_active, gpu_pstate_numeric,
                        power_total_w, nvme_temp_max_c,
                        nic_temp_max_c, disk_used_pct, roce_rx_mbps, roce_tx_mbps,
                        roce_error_delta, roce_link_speed_min_mbps,
                        roce_link_up, probe_latency_ms, health
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ts,
                        node_id,
                        node.get("name") or node.get("host") or node_id,
                        _safe_float(summary.get("cpu_used_pct")),
                        _safe_float(summary.get("cpu_soc_temp_max_c")),
                        _safe_float(memory.get("used_pct")),
                        _safe_float(memory.get("available_mib")),
                        _safe_float(memory.get("swap_used_pct")),
                        _safe_float(memory.get("swap_in_pages_s")),
                        _safe_float(memory.get("swap_out_pages_s")),
                        _safe_float(pressure.get("some_avg10")),
                        _safe_float(pressure.get("full_avg10")),
                        _safe_float(summary.get("gpu_util_avg_pct")),
                        _safe_float(summary.get("gpu_temp_max_c")),
                        _safe_float(summary.get("gpu_sm_clock_avg_mhz")),
                        _safe_float(summary.get("gpu_sm_clock_pct")),
                        _safe_float(summary.get("gpu_throttle_active")),
                        _safe_float(summary.get("gpu_pstate_numeric")),
                        _safe_float(summary.get("power_total_w")),
                        _safe_float(summary.get("nvme_temp_max_c")),
                        _safe_float(summary.get("nic_temp_max_c")),
                        _safe_float(disk_root.get("used_pct")),
                        _safe_float(summary.get("roce_rx_mbps")),
                        _safe_float(summary.get("roce_tx_mbps")),
                        _safe_float(summary.get("roce_error_delta")),
                        _safe_float(summary.get("roce_link_speed_min_mbps")),
                        _safe_float(summary.get("roce_link_up")),
                        _safe_float(node.get("probe_latency_ms")),
                        str(node.get("health") or summary.get("health") or "error"),
                    ),
                )

            sample_state = str(
                vllm.get("sample_state")
                or ("collection_error" if vllm.get("status", "ok") != "ok" else "ok")
            )
            event_state = str(
                vllm.get("event_state")
                or (
                    "active"
                    if any((_safe_float(vllm.get(key)) or 0) > 0 for key in ["running", "waiting", "prompt_tok_s", "generation_tok_s"])
                    else "idle"
                )
            )
            vllm_values: dict[str, Any] = {
                "ts": ts,
                "deployment_id": str(vllm.get("deployment_id") or "default"),
                "sample_state": sample_state,
                "event_state": event_state,
                "interval_s": _safe_float(vllm.get("interval_s")),
                "counter_reset": 1 if vllm.get("counter_reset") else 0,
                "missing_metrics": json.dumps(vllm.get("missing_metrics") or [], separators=(",", ":")),
                "running": _safe_float(vllm.get("running")),
                "waiting": _safe_float(vllm.get("waiting")),
                "waiting_capacity": _safe_float(vllm.get("waiting_capacity")),
                "waiting_deferred": _safe_float(vllm.get("waiting_deferred")),
                "kv_cache_usage_pct": _safe_float(vllm.get("kv_cache_usage_pct")),
                "prompt_tokens_delta": _safe_float(vllm.get("prompt_tokens_delta")),
                "cached_prompt_tokens_delta": _safe_float(vllm.get("cached_prompt_tokens_delta")),
                "uncached_prompt_tokens_delta": _safe_float(vllm.get("uncached_prompt_tokens_delta")),
                "generation_tokens_delta": _safe_float(vllm.get("generation_tokens_delta")),
                "request_completed_delta": _safe_float(vllm.get("request_completed_delta")),
                "request_error_delta": _safe_float(vllm.get("request_error_delta")),
                "request_abort_delta": _safe_float(vllm.get("request_abort_delta")),
                "prompt_tok_s": _safe_float(vllm.get("prompt_tok_s")),
                "cached_prompt_tok_s": _safe_float(vllm.get("cached_prompt_tok_s")),
                "uncached_prompt_tok_s": _safe_float(vllm.get("uncached_prompt_tok_s")),
                "generation_tok_s": _safe_float(vllm.get("generation_tok_s")),
                "request_s": _safe_float(vllm.get("request_s")),
                "error_s": _safe_float(vllm.get("error_s")),
                "preemption_delta": _safe_float(vllm.get("preemption_delta")),
                "mtp_draft_tokens_delta": _safe_float(vllm.get("mtp_draft_tokens_delta")),
                "mtp_accepted_tokens_delta": _safe_float(vllm.get("mtp_accepted_tokens_delta")),
                "mtp_acceptance_pct": _safe_float(vllm.get("mtp_acceptance_pct")),
                "ttft_avg_s": _safe_float(vllm.get("ttft_avg_s")),
                "e2e_avg_s": _safe_float(vllm.get("e2e_avg_s")),
                "queue_avg_s": _safe_float(vllm.get("queue_avg_s")),
                "prefill_avg_s": _safe_float(vllm.get("prefill_avg_s")),
                "decode_avg_s": _safe_float(vllm.get("decode_avg_s")),
                "itl_avg_s": _safe_float(vllm.get("itl_avg_s")),
                "cache_hit_ratio_pct": _safe_float(vllm.get("cache_hit_ratio_pct")),
                "request_prompt_tokens_avg": _safe_float(vllm.get("request_prompt_tokens_avg")),
                "request_generation_tokens_avg": _safe_float(vllm.get("request_generation_tokens_avg")),
                "prefill_efficiency_tok_s": _safe_float(vllm.get("prefill_efficiency_tok_s")),
                "decode_efficiency_tok_s": _safe_float(vllm.get("decode_efficiency_tok_s")),
                "health": str(vllm.get("health") or "error"),
            }
            columns = list(vllm_values)
            placeholders = ", ".join("?" for _ in columns)
            cur.execute(
                f"INSERT INTO vllm_metrics ({', '.join(columns)}) VALUES ({placeholders})",
                tuple(vllm_values[column] for column in columns),
            )

            self._sync_alerts(cur, alerts, ts)
            self.conn.commit()

    def _sync_alerts(self, cur: sqlite3.Cursor, alerts: list[dict[str, Any]], ts: float) -> None:
        current: dict[str, dict[str, Any]] = {}
        for alert in alerts:
            base_key = str(alert.get("signature") or f"{alert.get('scope')}|{alert.get('level')}|{alert.get('message')}")
            current[base_key] = alert
            existing_id = self.active_alerts.get(base_key)
            if existing_id is None:
                occurrence_signature = f"{base_key}:{int(ts * 1000)}"
                cur.execute(
                    """
                    INSERT INTO alerts (
                        first_ts, last_ts, resolved_ts, level, scope, message, base_key, signature, health
                    ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ts,
                        ts,
                        str(alert.get("level") or "warning"),
                        str(alert.get("scope") or "unknown"),
                        str(alert.get("message") or ""),
                        base_key,
                        occurrence_signature,
                        str(alert.get("level") or "warning"),
                    ),
                )
                self.active_alerts[base_key] = int(cur.lastrowid)
            else:
                cur.execute(
                    "UPDATE alerts SET last_ts=?, level=?, scope=?, message=?, base_key=?, health=? WHERE id=?",
                    (
                        ts,
                        str(alert.get("level") or "warning"),
                        str(alert.get("scope") or "unknown"),
                        str(alert.get("message") or ""),
                        base_key,
                        str(alert.get("level") or "warning"),
                        existing_id,
                    ),
                )

        for base_key, row_id in list(self.active_alerts.items()):
            if base_key in current:
                continue
            cur.execute(
                "UPDATE alerts SET last_ts=?, resolved_ts=? WHERE id=? AND resolved_ts IS NULL",
                (ts, ts, row_id),
            )
            self.active_alerts.pop(base_key, None)

    def cleanup_retention(self, days: int = 60) -> int:
        cutoff = time.time() - days * 86400
        with self.lock:
            total = 0
            for table, column in [
                ("node_metrics", "ts"),
                ("vllm_metrics", "ts"),
                ("alerts", "COALESCE(resolved_ts, last_ts, first_ts)"),
            ]:
                cur = self.conn.execute(f"DELETE FROM {table} WHERE {column} < ?", (cutoff,))
                total += cur.rowcount if cur.rowcount is not None else 0
            self.conn.commit()
            self.conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            self.conn.execute("PRAGMA optimize")
        self._load_active_alerts()
        return total

    @staticmethod
    def _vllm_stats_expression(metric: str, column: str) -> str:
        if metric == "cache_hit_ratio_pct":
            return f"CASE WHEN prompt_tokens_delta > 0 OR prompt_tok_s > 0 THEN {column} END"
        if metric == "mtp_acceptance_pct":
            return f"CASE WHEN mtp_draft_tokens_delta > 0 THEN {column} END"
        if metric in VLLM_POSITIVE_ACTIVITY_METRICS:
            return f"CASE WHEN {column} > 0 THEN {column} END"
        return column

    def _table_stats(
        self,
        table: str,
        metrics: dict[str, str],
        cutoff: float,
        *,
        where_sql: str = "",
        params: tuple[Any, ...] = (),
        vllm: bool = False,
        max_percentile_samples: int = 10_000,
    ) -> dict[str, Any]:
        expressions = {
            metric: self._vllm_stats_expression(metric, column) if vllm else column
            for metric, column in metrics.items()
        }
        aggregate_columns = []
        for metric, expression in expressions.items():
            aggregate_columns.extend(
                [
                    f'COUNT({expression}) AS "{metric}__count"',
                    f'MIN({expression}) AS "{metric}__min"',
                    f'MAX({expression}) AS "{metric}__max"',
                    f'AVG({expression}) AS "{metric}__avg"',
                    f'AVG(({expression}) * ({expression})) AS "{metric}__mean_square"',
                ]
            )
        base_where = "ts >= ?"
        args: list[Any] = [cutoff]
        if where_sql:
            base_where += f" AND {where_sql}"
            args.extend(params)
        aggregate_query = f"SELECT {', '.join(aggregate_columns)} FROM {table} WHERE {base_where}"

        with self.read_lock:
            aggregate = self.read_conn.execute(aggregate_query, tuple(args)).fetchone()
            groups = [list(metrics)]
            if vllm:
                groups = [
                    [metric for metric in metrics if metric not in VLLM_EVENT_METRICS],
                    [metric for metric in metrics if metric in VLLM_EVENT_METRICS],
                ]
            sampled_values: dict[str, list[float]] = {metric: [] for metric in metrics}
            sample_strides: dict[str, int] = {metric: 1 for metric in metrics}
            for group in groups:
                if not group:
                    continue
                largest_count = max(int(aggregate[f"{metric}__count"] or 0) for metric in group)
                stride = max(1, math.ceil(largest_count / max_percentile_samples))
                sample_columns = ", ".join(
                    f'{expressions[metric]} AS "{metric}"' for metric in group
                )
                sample_query = f"SELECT {sample_columns} FROM {table} WHERE {base_where}"
                sample_args = list(args)
                if vllm and all(metric in VLLM_EVENT_METRICS for metric in group):
                    event_filter = " OR ".join(f"({expressions[metric]}) IS NOT NULL" for metric in group)
                    sample_query += f" AND ({event_filter})"
                if stride > 1:
                    sample_query += " AND id % ? = 0"
                    sample_args.append(stride)
                sample_query += " ORDER BY ts"
                sample_rows = self.read_conn.execute(sample_query, tuple(sample_args)).fetchall()
                for metric in group:
                    sampled_values[metric] = [
                        float(row[metric]) for row in sample_rows if row[metric] is not None
                    ]
                    sample_strides[metric] = stride

        result: dict[str, Any] = {}
        for metric in metrics:
            sampled = sampled_values[metric]
            summary = describe(sampled)
            count = int(aggregate[f"{metric}__count"] or 0)
            average = _safe_float(aggregate[f"{metric}__avg"])
            mean_square = _safe_float(aggregate[f"{metric}__mean_square"])
            summary.update(
                {
                    "count": count,
                    "min": _safe_float(aggregate[f"{metric}__min"]),
                    "max": _safe_float(aggregate[f"{metric}__max"]),
                    "avg": average,
                    "stddev": (
                        math.sqrt(max(0.0, mean_square - average * average))
                        if average is not None and mean_square is not None
                        else None
                    ),
                    "percentiles_approximate": bool(count and sample_strides[metric] > 1),
                    "percentile_samples": len(sampled),
                }
            )
            result[metric] = summary
        return result

    def stats(self, window_seconds: int) -> dict[str, Any]:
        cutoff = time.time() - window_seconds
        with self.read_lock:
            node_ids = [row["node_id"] for row in self.read_conn.execute(
                "SELECT DISTINCT node_id FROM node_metrics WHERE ts >= ? ORDER BY node_id",
                (cutoff,),
            ).fetchall()]
        nodes: dict[str, Any] = {}
        for node_id in node_ids:
            nodes[node_id] = self._table_stats(
                "node_metrics",
                NODE_METRIC_COLUMNS,
                cutoff,
                where_sql="node_id = ?",
                params=(node_id,),
            )

        vllm = self._table_stats("vllm_metrics", VLLM_METRIC_COLUMNS, cutoff, vllm=True)
        return {
            "window_seconds": window_seconds,
            "generated_at": time.time(),
            "nodes": nodes,
            "vllm": vllm,
            "inference_sampling": self.inference_sampling(window_seconds),
        }

    def inference_sampling(self, window_seconds: int) -> dict[str, Any]:
        cutoff = time.time() - window_seconds
        with self.read_lock:
            row = self.read_conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE
                        WHEN sample_state IN ('ok', 'warmup', 'counter_reset') THEN 1
                        WHEN sample_state IS NULL AND health != 'error' THEN 1
                        ELSE 0
                    END) AS collected,
                    SUM(CASE
                        WHEN event_state IN ('active', 'completion_event') THEN 1
                        WHEN event_state IS NULL AND (
                            COALESCE(running, 0) > 0 OR COALESCE(waiting, 0) > 0 OR
                            COALESCE(prompt_tok_s, 0) > 0 OR COALESCE(generation_tok_s, 0) > 0
                        ) THEN 1
                        ELSE 0
                    END) AS active,
                    SUM(CASE
                        WHEN event_state = 'completion_event' THEN 1
                        WHEN event_state IS NULL AND (
                            COALESCE(prompt_tok_s, 0) > 0 OR COALESCE(generation_tok_s, 0) > 0 OR
                            COALESCE(request_s, 0) > 0 OR ttft_avg_s IS NOT NULL
                        ) THEN 1
                        ELSE 0
                    END) AS event_samples
                FROM vllm_metrics
                WHERE ts >= ?
                """,
                (cutoff,),
            ).fetchone()
            states = self.read_conn.execute(
                """
                SELECT COALESCE(sample_state, 'legacy') AS state, COUNT(*) AS count
                FROM vllm_metrics WHERE ts >= ? GROUP BY state ORDER BY state
                """,
                (cutoff,),
            ).fetchall()
        total = int(row["total"] or 0)
        collected = int(row["collected"] or 0)
        active = int(row["active"] or 0)
        return {
            "total_samples": total,
            "collected_samples": collected,
            "collection_coverage_pct": round(collected / total * 100.0, 1) if total else 0.0,
            "active_samples": active,
            "activity_ratio_pct": round(active / collected * 100.0, 1) if collected else 0.0,
            "event_samples": int(row["event_samples"] or 0),
            "states": {str(item["state"]): int(item["count"]) for item in states},
        }

    def series(
        self,
        *,
        kind: str,
        metric: str,
        window_seconds: int,
        bucket_seconds: int = 0,
        node_id: str | None = None,
        max_points: int = 5000,
    ) -> dict[str, Any]:
        cutoff = time.time() - window_seconds
        if kind == "node":
            column = NODE_METRIC_COLUMNS[metric]
            table = "node_metrics"
            where_sql = "node_id = ?"
            params: tuple[Any, ...] = (node_id or "node-1",)
        elif kind == "vllm":
            column = VLLM_METRIC_COLUMNS[metric]
            table = "vllm_metrics"
            where_sql = ""
            params = ()
        else:
            raise ValueError(f"unknown series kind: {kind}")

        positive_only = kind == "vllm" and metric in VLLM_POSITIVE_ACTIVITY_METRICS

        requested_bucket_seconds = bucket_seconds
        max_points = max(100, min(20_000, int(max_points)))
        minimum_bucket = max(1, math.ceil(window_seconds / max_points))
        if bucket_seconds > 0 and math.ceil(window_seconds / bucket_seconds) > max_points:
            bucket_seconds = minimum_bucket

        with self.read_lock:
            if bucket_seconds == 0:
                count_query = f"SELECT COUNT(*) FROM {table} WHERE ts >= ?"
                count_args: list[Any] = [cutoff]
                if where_sql:
                    count_query += f" AND {where_sql}"
                    count_args.extend(params)
                if positive_only:
                    count_query += f" AND {column} > 0"
                count_query += f" AND {column} IS NOT NULL"
                point_count = int(self.read_conn.execute(count_query, tuple(count_args)).fetchone()[0])
                if point_count > max_points:
                    bucket_seconds = minimum_bucket
            if bucket_seconds and bucket_seconds > 0:
                query = (
                    f"SELECT CAST(ts / ? AS INTEGER) * ? AS bucket_ts, "
                    f"SUM({column}) / COUNT({column}) AS value "
                    f"FROM {table} WHERE ts >= ?"
                )
                args: list[Any] = [bucket_seconds, bucket_seconds, cutoff]
                if where_sql:
                    query += f" AND {where_sql}"
                    args.extend(params)
                if positive_only:
                    query += f" AND {column} > 0"
                query += f" AND {column} IS NOT NULL GROUP BY bucket_ts ORDER BY bucket_ts"
                rows = self.read_conn.execute(query, tuple(args)).fetchall()
            else:
                query = f"SELECT ts, {column} AS value FROM {table} WHERE ts >= ?"
                args = [cutoff]
                if where_sql:
                    query += f" AND {where_sql}"
                    args.extend(params)
                if positive_only:
                    query += f" AND {column} > 0"
                query += f" AND {column} IS NOT NULL ORDER BY ts"
                rows = self.read_conn.execute(query, tuple(args)).fetchall()

        timestamps = [float(row[0]) for row in rows]
        values = [float(row[1]) for row in rows]
        return {
            "kind": kind,
            "metric": metric,
            "node_id": node_id,
            "window_seconds": window_seconds,
            "bucket_seconds": bucket_seconds,
            "requested_bucket_seconds": requested_bucket_seconds,
            "downsampled": bucket_seconds != requested_bucket_seconds,
            "timestamps": timestamps,
            "values": values,
        }

    def recent_vllm_samples(self, max_age_seconds: int = 900) -> dict[str, dict[str, float]]:
        cutoff = time.time() - max_age_seconds
        conditions = {
            "prompt_tok_s": "prompt_tok_s > 0",
            "request_s": "request_s > 0",
            "ttft_avg_s": "ttft_avg_s IS NOT NULL",
            "e2e_avg_s": "e2e_avg_s IS NOT NULL",
            "queue_avg_s": "queue_avg_s IS NOT NULL",
            "prefill_efficiency_tok_s": "prefill_efficiency_tok_s IS NOT NULL",
            "decode_efficiency_tok_s": "decode_efficiency_tok_s IS NOT NULL",
            "mtp_acceptance_pct": "mtp_acceptance_pct IS NOT NULL",
            "cache_hit_ratio_pct": "prompt_tok_s > 0 AND cache_hit_ratio_pct IS NOT NULL",
        }
        samples: dict[str, dict[str, float]] = {}
        with self.read_lock:
            for column, condition in conditions.items():
                row = self.read_conn.execute(
                    f"SELECT ts, {column} AS value FROM vllm_metrics "
                    f"WHERE ts >= ? AND {condition} ORDER BY ts DESC LIMIT 1",
                    (cutoff,),
                ).fetchone()
                if row is not None:
                    samples[column] = {"sampled_at": float(row["ts"]), "value": float(row["value"])}
        return samples

    def alerts(self, window_seconds: int) -> dict[str, Any]:
        cutoff = time.time() - window_seconds
        with self.read_lock:
            rows = self.read_conn.execute(
                """
                SELECT id, first_ts, last_ts, resolved_ts, level, scope, message, base_key, signature, health
                FROM alerts
                WHERE first_ts >= ? OR last_ts >= ? OR resolved_ts >= ?
                ORDER BY last_ts DESC
                """,
                (cutoff, cutoff, cutoff),
            ).fetchall()
        items = []
        for row in rows:
            items.append(
                {
                    "id": int(row["id"]),
                    "first_ts": float(row["first_ts"]),
                    "last_ts": float(row["last_ts"]),
                    "resolved_ts": float(row["resolved_ts"]) if row["resolved_ts"] is not None else None,
                    "level": row["level"],
                    "scope": row["scope"],
                    "message": row["message"],
                    "base_key": row["base_key"],
                    "signature": row["signature"],
                    "health": row["health"],
                    "status": "active" if row["resolved_ts"] is None else "resolved",
                }
            )
        return {"window_seconds": window_seconds, "items": items}

    def active_alert_payloads(self) -> list[dict[str, Any]]:
        with self.read_lock:
            rows = self.read_conn.execute(
                """
                SELECT level, scope, message, base_key
                FROM alerts WHERE resolved_ts IS NULL ORDER BY last_ts
                """
            ).fetchall()
        return [
            {
                "level": str(row["level"]),
                "scope": str(row["scope"]),
                "message": str(row["message"]),
                "signature": str(row["base_key"]),
            }
            for row in rows
        ]

    def analysis_rows(self, window_seconds: int) -> dict[str, Any]:
        cutoff = time.time() - window_seconds
        node_columns = ", ".join(["ts", "node_id", *NODE_METRIC_COLUMNS.values()])
        vllm_columns = ", ".join(
            ["ts", "deployment_id", "sample_state", "event_state", *VLLM_ANALYSIS_COLUMNS.values()]
        )
        with self.read_lock:
            node_rows = self.read_conn.execute(
                f"SELECT {node_columns} FROM node_metrics WHERE ts >= ? ORDER BY ts",
                (cutoff,),
            ).fetchall()
            vllm_rows = self.read_conn.execute(
                f"SELECT {vllm_columns} FROM vllm_metrics WHERE ts >= ? ORDER BY ts",
                (cutoff,),
            ).fetchall()
        return {
            "window_seconds": window_seconds,
            "generated_at": time.time(),
            "nodes": [dict(row) for row in node_rows],
            "vllm": [dict(row) for row in vllm_rows],
        }

    def database_health(self) -> dict[str, Any]:
        with self.read_lock:
            page_count = int(self.read_conn.execute("PRAGMA page_count").fetchone()[0])
            page_size = int(self.read_conn.execute("PRAGMA page_size").fetchone()[0])
            freelist = int(self.read_conn.execute("PRAGMA freelist_count").fetchone()[0])
            integrity = str(self.read_conn.execute("PRAGMA quick_check").fetchone()[0])
            rows = {
                table: int(self.read_conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in ["node_metrics", "vllm_metrics", "alerts"]
            }
        wal_path = Path(f"{self.path}-wal")
        return {
            "status": "ok" if integrity == "ok" else "error",
            "size_bytes": page_count * page_size,
            "free_bytes": freelist * page_size,
            "wal_size_bytes": wal_path.stat().st_size if wal_path.exists() else 0,
            "rows": rows,
        }
