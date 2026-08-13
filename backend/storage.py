from __future__ import annotations

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
    "kv_cache_usage_pct": "kv_cache_usage_pct",
    "prompt_tok_s": "prompt_tok_s",
    "generation_tok_s": "generation_tok_s",
    "request_s": "request_s",
    "error_s": "error_s",
    "ttft_avg_s": "ttft_avg_s",
    "e2e_avg_s": "e2e_avg_s",
    "cache_hit_ratio_pct": "cache_hit_ratio_pct",
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


def _positive_float(value: Any) -> float | None:
    parsed = _safe_float(value)
    return parsed if parsed is not None and parsed > 0 else None


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
        self.active_alerts: dict[str, int] = {}
        self._init_pragmas()
        self._init_schema()
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
                    running REAL,
                    waiting REAL,
                    kv_cache_usage_pct REAL,
                    prompt_tok_s REAL,
                    generation_tok_s REAL,
                    request_s REAL,
                    error_s REAL,
                    ttft_avg_s REAL,
                    e2e_avg_s REAL,
                    cache_hit_ratio_pct REAL,
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
                CREATE INDEX IF NOT EXISTS idx_alerts_base_key ON alerts(base_key);
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

            cur.execute(
                """
                INSERT INTO vllm_metrics (
                    ts, running, waiting, kv_cache_usage_pct, prompt_tok_s,
                    generation_tok_s, request_s, error_s, ttft_avg_s,
                    e2e_avg_s, cache_hit_ratio_pct, health
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    _safe_float(vllm.get("running")),
                    _safe_float(vllm.get("waiting")),
                    _safe_float(vllm.get("kv_cache_usage_pct")),
                    _positive_float(vllm.get("prompt_tok_s")),
                    _positive_float(vllm.get("generation_tok_s")),
                    _safe_float(vllm.get("request_s")),
                    _safe_float(vllm.get("error_s")),
                    _safe_float(vllm.get("ttft_avg_s")),
                    _safe_float(vllm.get("e2e_avg_s")),
                    _safe_float(vllm.get("cache_hit_ratio_pct")),
                    str(vllm.get("health") or "error"),
                ),
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
        self._load_active_alerts()
        return total

    def _metric_values(self, table: str, column: str, cutoff: float, where_sql: str = "", params: tuple[Any, ...] = ()) -> list[float]:
        query = f"SELECT {column} AS value FROM {table} WHERE ts >= ?"
        args: list[Any] = [cutoff]
        if where_sql:
            query += f" AND {where_sql}"
            args.extend(params)
        query += f" AND {column} IS NOT NULL ORDER BY ts"
        with self.lock:
            rows = self.conn.execute(query, tuple(args)).fetchall()
        return [float(row["value"]) for row in rows if row["value"] is not None]

    def stats(self, window_seconds: int) -> dict[str, Any]:
        cutoff = time.time() - window_seconds
        with self.lock:
            node_ids = [row["node_id"] for row in self.conn.execute(
                "SELECT DISTINCT node_id FROM node_metrics WHERE ts >= ? ORDER BY node_id",
                (cutoff,),
            ).fetchall()]
        nodes: dict[str, Any] = {}
        for node_id in node_ids:
            node_stats: dict[str, Any] = {}
            for metric, column in NODE_METRIC_COLUMNS.items():
                values = self._metric_values("node_metrics", column, cutoff, "node_id = ?", (node_id,))
                node_stats[metric] = describe(values)
            nodes[node_id] = node_stats

        vllm: dict[str, Any] = {}
        for metric, column in VLLM_METRIC_COLUMNS.items():
            values = self._metric_values("vllm_metrics", column, cutoff)
            vllm[metric] = describe(values)
        return {
            "window_seconds": window_seconds,
            "generated_at": time.time(),
            "nodes": nodes,
            "vllm": vllm,
        }

    def series(
        self,
        *,
        kind: str,
        metric: str,
        window_seconds: int,
        bucket_seconds: int = 0,
        node_id: str | None = None,
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

        positive_only = kind == "vllm" and metric in {"prompt_tok_s", "generation_tok_s"}

        with self.lock:
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
                rows = self.conn.execute(query, tuple(args)).fetchall()
            else:
                query = f"SELECT ts, {column} AS value FROM {table} WHERE ts >= ?"
                args = [cutoff]
                if where_sql:
                    query += f" AND {where_sql}"
                    args.extend(params)
                if positive_only:
                    query += f" AND {column} > 0"
                query += f" AND {column} IS NOT NULL ORDER BY ts"
                rows = self.conn.execute(query, tuple(args)).fetchall()

        timestamps = [float(row[0]) for row in rows]
        values = [float(row[1]) for row in rows]
        return {
            "kind": kind,
            "metric": metric,
            "node_id": node_id,
            "window_seconds": window_seconds,
            "bucket_seconds": bucket_seconds,
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
            "cache_hit_ratio_pct": "prompt_tok_s > 0 AND cache_hit_ratio_pct IS NOT NULL",
        }
        samples: dict[str, dict[str, float]] = {}
        with self.lock:
            for column, condition in conditions.items():
                row = self.conn.execute(
                    f"SELECT ts, {column} AS value FROM vllm_metrics "
                    f"WHERE ts >= ? AND {condition} ORDER BY ts DESC LIMIT 1",
                    (cutoff,),
                ).fetchone()
                if row is not None:
                    samples[column] = {"sampled_at": float(row["ts"]), "value": float(row["value"])}
        return samples

    def alerts(self, window_seconds: int) -> dict[str, Any]:
        cutoff = time.time() - window_seconds
        with self.lock:
            rows = self.conn.execute(
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

    def analysis_rows(self, window_seconds: int) -> dict[str, Any]:
        cutoff = time.time() - window_seconds
        node_columns = ", ".join(["ts", "node_id", *NODE_METRIC_COLUMNS.values()])
        vllm_columns = ", ".join(["ts", *VLLM_METRIC_COLUMNS.values()])
        with self.lock:
            node_rows = self.conn.execute(
                f"SELECT {node_columns} FROM node_metrics WHERE ts >= ? ORDER BY ts",
                (cutoff,),
            ).fetchall()
            vllm_rows = self.conn.execute(
                f"SELECT {vllm_columns} FROM vllm_metrics WHERE ts >= ? ORDER BY ts",
                (cutoff,),
            ).fetchall()
        return {
            "window_seconds": window_seconds,
            "generated_at": time.time(),
            "nodes": [dict(row) for row in node_rows],
            "vllm": [dict(row) for row in vllm_rows],
        }
