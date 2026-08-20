#!/usr/bin/env python3
"""Monitor a two-node DGX Spark vLLM deployment from a lightweight host."""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from analysis import analyze_windows
from storage import MonitorStore, parse_duration_seconds


APP_ROOT = Path(__file__).resolve().parent
STATIC_DIR = APP_ROOT / "static"
PROBE_SCRIPT = Path(os.environ.get("DGX_MONITOR_PROBE", APP_ROOT.parent / "remote" / "dgx_probe.py"))
DB_PATH = Path(os.environ.get("DGX_MONITOR_DB", APP_ROOT.parent / "data" / "monitor.sqlite3"))

POLL_INTERVAL_S = float(os.environ.get("DGX_MONITOR_POLL_INTERVAL", "2.5"))
HISTORY_SECONDS = int(os.environ.get("DGX_MONITOR_HISTORY_SECONDS", "7200"))
HISTORY_MAXLEN = max(120, int(HISTORY_SECONDS / POLL_INTERVAL_S))
RECENT_VLLM_TTL_S = float(os.environ.get("DGX_MONITOR_RECENT_VLLM_TTL", "900"))
DEPLOYMENT_ID = os.environ.get("DGX_MONITOR_DEPLOYMENT_ID", "default").strip() or "default"
ALERT_WARNING_S = float(os.environ.get("DGX_MONITOR_ALERT_WARNING_SECONDS", "300"))
ALERT_CRITICAL_S = float(os.environ.get("DGX_MONITOR_ALERT_CRITICAL_SECONDS", "120"))
ALERT_RECOVERY_S = float(os.environ.get("DGX_MONITOR_ALERT_RECOVERY_SECONDS", "300"))

def csv_env(name: str, default: str) -> list[str]:
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


SSH_USER = os.environ.get("DGX_MONITOR_SSH_USER", "ubuntu")
NODES = [
    {
        "id": os.environ.get("DGX_MONITOR_NODE_1_ID", "node-1"),
        "name": os.environ.get("DGX_MONITOR_NODE_1_NAME", "dgx-spark-1"),
        "host": os.environ.get("DGX_MONITOR_NODE_1_HOST", "dgx-spark-1.local"),
        "ifaces": csv_env("DGX_MONITOR_NODE_1_IFACES", "eth0"),
        "roce_ifaces": csv_env("DGX_MONITOR_NODE_1_ROCE_IFACES", "eth0"),
    },
    {
        "id": os.environ.get("DGX_MONITOR_NODE_2_ID", "node-2"),
        "name": os.environ.get("DGX_MONITOR_NODE_2_NAME", "dgx-spark-2"),
        "host": os.environ.get("DGX_MONITOR_NODE_2_HOST", "dgx-spark-2.local"),
        "ifaces": csv_env("DGX_MONITOR_NODE_2_IFACES", "eth0"),
        "roce_ifaces": csv_env("DGX_MONITOR_NODE_2_ROCE_IFACES", "eth0"),
    },
]

VLLM_METRICS_URL = os.environ.get("DGX_MONITOR_VLLM_METRICS_URL", "http://dgx-spark-1.local:8000/metrics")
VLLM_MODELS_URL = os.environ.get("DGX_MONITOR_VLLM_MODELS_URL", "http://dgx-spark-1.local:8000/v1/models")


PROM_LINE_RE = re.compile(r"^([^#\s{]+)(?:\{([^}]*)\})?\s+([-+0-9.eE]+)")
LABEL_RE = re.compile(r'(\w+)="([^"]*)"')


def now() -> float:
    return time.time()


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        value = float(value)
        if math.isnan(value) or math.isinf(value):
            return default
        return value
    except (TypeError, ValueError):
        return default


def optional_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


def run_http_text(url: str, timeout: float = 4.0) -> str:
    request = urllib.request.Request(url, headers={"Accept": "text/plain, application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


async def run_ssh_probe(node: dict[str, Any]) -> dict[str, Any]:
    script = PROBE_SCRIPT.read_text()
    command = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=6",
        "-o",
        "StrictHostKeyChecking=accept-new",
        f"{SSH_USER}@{node['host']}",
        "python3",
        "-",
        "--ifaces",
        ",".join(node["ifaces"]),
    ]
    started = time.perf_counter()
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(script.encode()), timeout=15)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise RuntimeError("ssh probe timeout")

    latency_ms = (time.perf_counter() - started) * 1000.0
    if process.returncode != 0:
        raise RuntimeError((stderr or stdout).decode("utf-8", errors="replace")[-500:])
    payload = json.loads(stdout.decode("utf-8"))
    payload["probe_latency_ms"] = round(latency_ms, 1)
    return payload


def labels_key(labels: dict[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(labels.items()))


def parse_prometheus(text: str) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    for line in text.splitlines():
        match = PROM_LINE_RE.match(line.strip())
        if not match:
            continue
        name, labels_raw, value_raw = match.groups()
        labels = dict(LABEL_RE.findall(labels_raw or ""))
        value = optional_float(value_raw)
        if value is None:
            continue
        metric = metrics.setdefault(name, {})
        metric[labels_key(labels)] = {"labels": labels, "value": value}
    return metrics


def metric_sum(metrics: dict[str, Any], name: str, **labels: str) -> float | None:
    entries = metrics.get(name) or {}
    total = 0.0
    matched = False
    for entry in entries.values():
        if all(entry["labels"].get(key) == value for key, value in labels.items()):
            value = optional_float(entry["value"])
            if value is not None:
                total += value
                matched = True
    return total if matched else None


def metric_max(metrics: dict[str, Any], name: str, **labels: str) -> float | None:
    entries = metrics.get(name) or {}
    values = [
        value
        for entry in entries.values()
        if all(entry["labels"].get(key) == expected for key, expected in labels.items())
        and (value := optional_float(entry["value"])) is not None
    ]
    return max(values) if values else None


def counter_delta(
    previous: dict[str, Any] | None,
    current: dict[str, Any],
    key: str,
) -> tuple[float | None, bool]:
    """Return a counter delta and whether a reset was observed."""
    current_value = optional_float(current.get(key))
    previous_value = optional_float((previous or {}).get(key))
    if current_value is None or previous_value is None:
        return None, False
    delta = current_value - previous_value
    if delta < 0:
        return None, True
    return delta, False


def rate_from_delta(delta: float | None, elapsed_s: float | None) -> float | None:
    if delta is None or elapsed_s is None or elapsed_s <= 0:
        return None
    return delta / max(0.001, elapsed_s)


def interval_mean(
    deltas: dict[str, float | None],
    sum_key: str,
    count_key: str,
) -> float | None:
    count = deltas.get(count_key)
    total = deltas.get(sum_key)
    if count is None or total is None or count <= 0 or total < 0:
        return None
    return total / count


def counter_rate(previous: dict[str, Any] | None, current: dict[str, Any], key: str, elapsed_s: float) -> float:
    """Compatibility helper for node counters, where a missing delta is displayed as zero."""
    delta, _ = counter_delta(previous, current, key)
    return rate_from_delta(delta, elapsed_s) or 0.0


def enrich_recent_vllm_metrics(snapshot: dict[str, Any], recent: dict[str, dict[str, float]]) -> None:
    """Keep short-lived request metrics visible without changing stored raw samples."""
    ts = safe_float(snapshot.get("ts"), now())
    candidates: dict[str, float | None] = {
        "prompt_tok_s": snapshot.get("prompt_tok_s") if safe_float(snapshot.get("prompt_tok_s")) > 0 else None,
        "request_s": snapshot.get("request_s") if safe_float(snapshot.get("request_s")) > 0 else None,
        "ttft_avg_s": snapshot.get("ttft_avg_s"),
        "e2e_avg_s": snapshot.get("e2e_avg_s"),
        "queue_avg_s": snapshot.get("queue_avg_s"),
        "prefill_efficiency_tok_s": snapshot.get("prefill_efficiency_tok_s"),
        "decode_efficiency_tok_s": snapshot.get("decode_efficiency_tok_s"),
        "mtp_acceptance_pct": snapshot.get("mtp_acceptance_pct"),
        "cache_hit_ratio_pct": (
            snapshot.get("cache_hit_ratio_pct") if safe_float(snapshot.get("prompt_tok_s")) > 0 else None
        ),
    }
    for key, value in candidates.items():
        if value is not None:
            recent[key] = {"value": safe_float(value), "sampled_at": ts}

    snapshot["recent"] = {
        key: {
            **sample,
            "age_s": round(max(0.0, ts - safe_float(sample.get("sampled_at"), ts)), 1),
        }
        for key, sample in recent.items()
        if ts - safe_float(sample.get("sampled_at"), ts) <= RECENT_VLLM_TTL_S
    }


def enrich_interfaces(
    node_id: str,
    current: list[dict[str, Any]],
    previous: dict[str, Any] | None,
    elapsed_s: float,
    roce_ifaces: list[str],
) -> list[dict[str, Any]]:
    previous_by_name = {item["name"]: item for item in (previous or {}).get("interfaces", [])}
    enriched = []
    for item in current:
        prev = previous_by_name.get(item.get("name"))
        rx_bps = counter_rate(prev, item, "rx_bytes", elapsed_s)
        tx_bps = counter_rate(prev, item, "tx_bytes", elapsed_s)
        rx_err = max(0, safe_float(item.get("rx_errors")) - safe_float(prev.get("rx_errors"))) if prev else 0
        tx_err = max(0, safe_float(item.get("tx_errors")) - safe_float(prev.get("tx_errors"))) if prev else 0
        rx_drop = max(0, safe_float(item.get("rx_dropped")) - safe_float(prev.get("rx_dropped"))) if prev else 0
        tx_drop = max(0, safe_float(item.get("tx_dropped")) - safe_float(prev.get("tx_dropped"))) if prev else 0
        xmit_wait = max(0, safe_float(item.get("xmit_wait")) - safe_float(prev.get("xmit_wait"))) if prev else 0
        speed_mbps = safe_float(item.get("speed_mbps"))
        status = "ok"
        if not item.get("exists") or item.get("operstate") != "up":
            status = "critical"
        elif (item.get("is_roce") or item["name"] in roce_ifaces) and speed_mbps and speed_mbps < 200000:
            status = "warning"
        elif rx_err + tx_err + rx_drop + tx_drop > 0:
            status = "warning"
        enriched.append(
            {
                **item,
                "node_id": node_id,
                "is_roce": bool(item.get("is_roce")) or item["name"] in roce_ifaces,
                "rx_mbps": round(rx_bps * 8 / 1_000_000, 2),
                "tx_mbps": round(tx_bps * 8 / 1_000_000, 2),
                "error_delta": int(rx_err + tx_err),
                "drop_delta": int(rx_drop + tx_drop),
                "xmit_wait_delta": int(xmit_wait),
                "health": status,
            }
        )
    return enriched


def summarize_node(node: dict[str, Any]) -> dict[str, Any]:
    gpus = [gpu for gpu in node.get("gpu", []) if gpu.get("status") == "ok"]
    ifaces = node.get("interfaces", [])
    temps = [safe_float(gpu.get("temperature_c")) for gpu in gpus if gpu.get("temperature_c") is not None]
    utils = [safe_float(gpu.get("gpu_util_pct")) for gpu in gpus if gpu.get("gpu_util_pct") is not None]
    powers = [safe_float(gpu.get("power_w")) for gpu in gpus if gpu.get("power_w") is not None]
    clocks = [safe_float(gpu.get("sm_clock_mhz")) for gpu in gpus if gpu.get("sm_clock_mhz") is not None]
    clock_ratios = [
        safe_float(gpu.get("sm_clock_mhz")) / safe_float(gpu.get("sm_clock_max_mhz")) * 100.0
        for gpu in gpus
        if safe_float(gpu.get("sm_clock_max_mhz")) > 0
    ]
    pstates = []
    for gpu in gpus:
        match = re.fullmatch(r"P(\d+)", str(gpu.get("pstate") or ""))
        if match:
            pstates.append(int(match.group(1)))
    throttle_active = any(
        str(gpu.get("clock_throttle_reasons") or "0") not in {"0", "0x0000000000000000"}
        for gpu in gpus
    )
    roce = [iface for iface in ifaces if iface.get("is_roce")]
    memory = node.get("memory") or {}
    pressure = memory.get("pressure") or {}
    thermals = node.get("thermals") or {}
    warnings = []
    for temp in temps:
        if temp >= 86:
            warnings.append("gpu_temp_critical")
        elif temp >= 82:
            warnings.append("gpu_temp_warning")
    for iface in roce:
        if iface.get("health") != "ok":
            warnings.append(f"roce_{iface.get('name')}_{iface.get('health')}")
    if safe_float(pressure.get("some_avg10")) >= 10:
        warnings.append("memory_pressure_warning")
    if safe_float(thermals.get("cpu_soc_temp_max_c")) >= 85:
        warnings.append("cpu_soc_temp_warning")
    if safe_float(thermals.get("nvme_temp_max_c")) >= 70:
        warnings.append("nvme_temp_warning")
    if safe_float(thermals.get("nic_temp_max_c")) >= 80:
        warnings.append("nic_temp_warning")

    return {
        "gpu_count": len(gpus),
        "cpu_used_pct": node.get("cpu_used_pct"),
        "mem_used_pct": memory.get("used_pct"),
        "gpu_temp_max_c": round(max(temps), 1) if temps else None,
        "gpu_util_avg_pct": round(sum(utils) / len(utils), 1) if utils else None,
        "gpu_sm_clock_avg_mhz": round(sum(clocks) / len(clocks), 1) if clocks else None,
        "gpu_sm_clock_pct": round(sum(clock_ratios) / len(clock_ratios), 1) if clock_ratios else None,
        "gpu_throttle_active": 1 if throttle_active else 0,
        "gpu_pstate_numeric": max(pstates) if pstates else None,
        "power_total_w": round(sum(powers), 1) if powers else None,
        "cpu_soc_temp_max_c": thermals.get("cpu_soc_temp_max_c"),
        "nvme_temp_max_c": thermals.get("nvme_temp_max_c"),
        "nic_temp_max_c": thermals.get("nic_temp_max_c"),
        "roce_rx_mbps": round(sum(safe_float(item.get("rx_mbps")) for item in roce), 2),
        "roce_tx_mbps": round(sum(safe_float(item.get("tx_mbps")) for item in roce), 2),
        "roce_error_delta": sum(int(item.get("error_delta", 0)) + int(item.get("drop_delta", 0)) for item in roce),
        "roce_link_speed_min_mbps": min(
            [safe_float(item.get("speed_mbps")) for item in roce if safe_float(item.get("speed_mbps")) > 0],
            default=None,
        ),
        "roce_link_up": 1 if roce and all(item.get("operstate") == "up" for item in roce) else 0,
        "warnings": sorted(set(warnings)),
        "health": "critical" if any("critical" in item for item in warnings) else ("warning" if warnings else "ok"),
    }


class MonitorState:
    def __init__(self) -> None:
        self.started_at = now()
        self.lock = asyncio.Lock()
        self.current: dict[str, Any] = {
            "status": "starting",
            "ts": now(),
            "nodes": {},
            "vllm": {},
            "alerts": [],
            "model": {},
        }
        self.history: deque[dict[str, Any]] = deque(maxlen=HISTORY_MAXLEN)
        self.previous_nodes: dict[str, dict[str, Any]] = {}
        self.previous_vllm: dict[str, Any] | None = None
        self.recent_vllm: dict[str, dict[str, float]] = {}

    async def snapshot(self) -> dict[str, Any]:
        async with self.lock:
            return json.loads(json.dumps(self.current))

    async def history_payload(self, limit: int = 240) -> dict[str, Any]:
        async with self.lock:
            return {"points": list(self.history)[-limit:]}

    async def update(self, snapshot: dict[str, Any]) -> None:
        async with self.lock:
            self.current = snapshot
            self.history.append(snapshot)


class AlertDebouncer:
    """Require sustained threshold violations and stable recovery before changing alerts."""

    def __init__(self) -> None:
        self.states: dict[str, dict[str, Any]] = {}

    def seed(self, alerts: list[dict[str, Any]], ts: float) -> None:
        for alert in alerts:
            key = str(alert.get("signature") or "")
            if not key:
                continue
            self.states[key] = {
                "emitted": dict(alert),
                "candidate": dict(alert),
                "candidate_token": str(alert.get("level")),
                "candidate_since": ts,
            }

    def _delay(self, key: str, alert: dict[str, Any] | None) -> float:
        if alert is None:
            return ALERT_RECOVERY_S
        if key.startswith("roce:") or key.endswith(":offline") or key in {
            "vllm:metrics:error",
            "vllm:preemption",
            "model:unavailable",
        }:
            return 0.0
        return ALERT_CRITICAL_S if alert.get("level") == "critical" else ALERT_WARNING_S

    def update(self, alerts: list[dict[str, Any]], ts: float) -> list[dict[str, Any]]:
        current = {str(alert["signature"]): dict(alert) for alert in alerts if alert.get("signature")}
        for key in set(self.states).union(current):
            raw = current.get(key)
            token = str(raw.get("level")) if raw is not None else "clear"
            state = self.states.setdefault(
                key,
                {
                    "emitted": None,
                    "candidate": raw,
                    "candidate_token": token,
                    "candidate_since": ts,
                },
            )
            if state["candidate_token"] != token:
                state["candidate"] = raw
                state["candidate_token"] = token
                state["candidate_since"] = ts
            elif raw is not None:
                state["candidate"] = raw

            emitted = state.get("emitted")
            if emitted is not None and raw is not None and emitted.get("level") == raw.get("level"):
                state["emitted"] = raw
                continue

            if ts - float(state["candidate_since"]) < self._delay(key, raw):
                continue
            state["emitted"] = dict(raw) if raw is not None else None
            if raw is None:
                self.states.pop(key, None)

        result = [state["emitted"] for state in self.states.values() if state.get("emitted") is not None]
        return sorted(result, key=lambda item: (item.get("level") != "critical", str(item.get("scope"))))


STATE = MonitorState()
STORE = MonitorStore(DB_PATH)
ALERT_FILTER = AlertDebouncer()
NEXT_CLEANUP_TS = 0.0
app = FastAPI(title="LLM Monitor 2X-DGX-Spark", version="2026.08.20")


def build_vllm_snapshot(metrics_text: str, previous: dict[str, Any] | None) -> dict[str, Any]:
    ts = now()
    metrics = parse_prometheus(metrics_text)
    counters = {
        "prompt_tokens_total": metric_sum(metrics, "vllm:prompt_tokens_total"),
        "prompt_tokens_cached_total": metric_sum(metrics, "vllm:prompt_tokens_cached_total"),
        "prompt_local_compute_total": metric_sum(
            metrics,
            "vllm:prompt_tokens_by_source_total",
            source="local_compute",
        ),
        "generation_tokens_total": metric_sum(metrics, "vllm:generation_tokens_total"),
        "request_success_total": metric_sum(metrics, "vllm:request_success_total"),
        "request_error_total": metric_sum(metrics, "vllm:request_success_total", finished_reason="error"),
        "request_abort_total": metric_sum(metrics, "vllm:request_success_total", finished_reason="abort"),
        "num_preemptions_total": metric_sum(metrics, "vllm:num_preemptions_total"),
        "mtp_draft_tokens_total": metric_sum(metrics, "vllm:spec_decode_num_draft_tokens_total"),
        "mtp_accepted_tokens_total": metric_sum(metrics, "vllm:spec_decode_num_accepted_tokens_total"),
        "ttft_sum": metric_sum(metrics, "vllm:time_to_first_token_seconds_sum"),
        "ttft_count": metric_sum(metrics, "vllm:time_to_first_token_seconds_count"),
        "e2e_sum": metric_sum(metrics, "vllm:e2e_request_latency_seconds_sum"),
        "e2e_count": metric_sum(metrics, "vllm:e2e_request_latency_seconds_count"),
        "queue_sum": metric_sum(metrics, "vllm:request_queue_time_seconds_sum"),
        "queue_count": metric_sum(metrics, "vllm:request_queue_time_seconds_count"),
        "prefill_time_sum": metric_sum(metrics, "vllm:request_prefill_time_seconds_sum"),
        "prefill_time_count": metric_sum(metrics, "vllm:request_prefill_time_seconds_count"),
        "decode_time_sum": metric_sum(metrics, "vllm:request_decode_time_seconds_sum"),
        "decode_time_count": metric_sum(metrics, "vllm:request_decode_time_seconds_count"),
        "itl_sum": metric_sum(metrics, "vllm:inter_token_latency_seconds_sum"),
        "itl_count": metric_sum(metrics, "vllm:inter_token_latency_seconds_count"),
        "request_prompt_tokens_sum": metric_sum(metrics, "vllm:request_prompt_tokens_sum"),
        "request_prompt_tokens_count": metric_sum(metrics, "vllm:request_prompt_tokens_count"),
        "request_generation_tokens_sum": metric_sum(metrics, "vllm:request_generation_tokens_sum"),
        "request_generation_tokens_count": metric_sum(metrics, "vllm:request_generation_tokens_count"),
        "prefill_kv_tokens_sum": metric_sum(metrics, "vllm:request_prefill_kv_computed_tokens_sum"),
        "prefill_kv_tokens_count": metric_sum(metrics, "vllm:request_prefill_kv_computed_tokens_count"),
    }
    previous_ts = optional_float((previous or {}).get("ts"))
    elapsed_s = ts - previous_ts if previous_ts is not None and ts > previous_ts else None
    prev_counters = (previous or {}).get("counters") if previous else None
    deltas: dict[str, float | None] = {}
    reset_keys: list[str] = []
    for key in counters:
        delta, reset = counter_delta(prev_counters, counters, key)
        deltas[key] = delta
        if reset:
            reset_keys.append(key)

    waiting = metric_sum(metrics, "vllm:num_requests_waiting")
    running = metric_sum(metrics, "vllm:num_requests_running")
    kv_cache = metric_max(metrics, "vllm:kv_cache_usage_perc")
    waiting_capacity = metric_sum(metrics, "vllm:num_requests_waiting_by_reason", reason="capacity")
    waiting_deferred = metric_sum(metrics, "vllm:num_requests_waiting_by_reason", reason="deferred")
    core_keys = {
        "prompt_tokens_total",
        "generation_tokens_total",
        "request_success_total",
        "ttft_sum",
        "ttft_count",
        "e2e_sum",
        "e2e_count",
    }
    missing_metrics = sorted(key for key, value in counters.items() if value is None)
    core_missing = core_keys.intersection(missing_metrics)
    for key, value in {"running": running, "waiting": waiting, "kv_cache_usage_perc": kv_cache}.items():
        if value is None:
            missing_metrics.append(key)
            core_missing.add(key)
    missing_metrics.sort()
    if core_missing:
        sample_state = "metrics_missing"
    elif prev_counters is None or elapsed_s is None:
        sample_state = "warmup"
    elif reset_keys:
        sample_state = "counter_reset"
    elif any(deltas[key] is None for key in core_keys):
        sample_state = "warmup"
    else:
        sample_state = "ok"

    if sample_state != "ok":
        deltas = {key: None for key in counters}

    prompt_delta = deltas["prompt_tokens_total"]
    cached_prompt_delta = deltas["prompt_tokens_cached_total"]
    generation_delta = deltas["generation_tokens_total"]
    request_completed_delta = deltas["request_success_total"]
    request_error_delta = deltas["request_error_total"]
    request_abort_delta = deltas["request_abort_total"]
    preemption_raw = deltas["num_preemptions_total"]
    preemption_delta = int(preemption_raw) if preemption_raw is not None else None

    uncached_prompt_delta = deltas["prompt_local_compute_total"]
    if uncached_prompt_delta is None and prompt_delta is not None and cached_prompt_delta is not None:
        uncached_prompt_delta = max(0.0, prompt_delta - cached_prompt_delta)

    prompt_rate = rate_from_delta(prompt_delta, elapsed_s)
    cached_rate = rate_from_delta(cached_prompt_delta, elapsed_s)
    uncached_rate = rate_from_delta(uncached_prompt_delta, elapsed_s)
    generation_rate = rate_from_delta(generation_delta, elapsed_s)
    request_rate = rate_from_delta(request_completed_delta, elapsed_s)
    error_delta = (
        request_error_delta + request_abort_delta
        if request_error_delta is not None and request_abort_delta is not None
        else None
    )
    error_rate = rate_from_delta(error_delta, elapsed_s)
    cache_hit_ratio = (
        cached_prompt_delta / prompt_delta
        if cached_prompt_delta is not None and prompt_delta is not None and prompt_delta > 0
        else None
    )

    ttft_avg_s = interval_mean(deltas, "ttft_sum", "ttft_count")
    e2e_avg_s = interval_mean(deltas, "e2e_sum", "e2e_count")
    queue_avg_s = interval_mean(deltas, "queue_sum", "queue_count")
    prefill_avg_s = interval_mean(deltas, "prefill_time_sum", "prefill_time_count")
    decode_avg_s = interval_mean(deltas, "decode_time_sum", "decode_time_count")
    itl_avg_s = interval_mean(deltas, "itl_sum", "itl_count")
    request_prompt_tokens_avg = interval_mean(deltas, "request_prompt_tokens_sum", "request_prompt_tokens_count")
    request_generation_tokens_avg = interval_mean(
        deltas,
        "request_generation_tokens_sum",
        "request_generation_tokens_count",
    )
    prefill_efficiency = None
    if (deltas["prefill_time_sum"] or 0) > 0 and deltas["prefill_kv_tokens_sum"] is not None:
        prefill_efficiency = deltas["prefill_kv_tokens_sum"] / deltas["prefill_time_sum"]
    decode_efficiency = None
    if (deltas["decode_time_sum"] or 0) > 0 and deltas["request_generation_tokens_sum"] is not None:
        decode_efficiency = deltas["request_generation_tokens_sum"] / deltas["decode_time_sum"]
    mtp_acceptance = None
    if (deltas["mtp_draft_tokens_total"] or 0) > 0 and deltas["mtp_accepted_tokens_total"] is not None:
        mtp_acceptance = deltas["mtp_accepted_tokens_total"] / deltas["mtp_draft_tokens_total"]

    if sample_state == "ok":
        if (request_completed_delta or 0) > 0:
            event_state = "completion_event"
        elif any((value or 0) > 0 for value in [running, waiting, prompt_delta, generation_delta]):
            event_state = "active"
        else:
            event_state = "idle"
    else:
        event_state = "unknown"

    health = "ok"
    warnings = []
    if sample_state == "metrics_missing":
        warnings.append("metrics_missing")
        health = "warning"
    elif sample_state == "counter_reset":
        warnings.append("counter_reset")
        health = "warning"
    if (waiting or 0) > 0:
        warnings.append("requests_waiting")
        health = "warning"
    if (kv_cache or 0) >= 0.9:
        warnings.append("kv_cache_critical")
        health = "critical"
    elif (kv_cache or 0) >= 0.8 and health != "critical":
        warnings.append("kv_cache_warning")
        health = "warning"
    if (preemption_delta or 0) > 0:
        warnings.append("preemption_delta")
        health = "critical"

    def rounded(value: float | None, digits: int = 2) -> float | None:
        return round(value, digits) if value is not None else None

    return {
        "ts": ts,
        "status": "ok",
        "sample_state": sample_state,
        "event_state": event_state,
        "deployment_id": DEPLOYMENT_ID,
        "interval_s": rounded(elapsed_s, 3),
        "counter_reset": bool(reset_keys),
        "reset_metrics": reset_keys,
        "missing_metrics": missing_metrics,
        "health": health,
        "warnings": warnings,
        "running": running,
        "waiting": waiting,
        "waiting_capacity": waiting_capacity,
        "waiting_deferred": waiting_deferred,
        "kv_cache_usage_pct": rounded(kv_cache * 100.0 if kv_cache is not None else None),
        "prompt_tokens_delta": rounded(prompt_delta, 0),
        "cached_prompt_tokens_delta": rounded(cached_prompt_delta, 0),
        "uncached_prompt_tokens_delta": rounded(uncached_prompt_delta, 0),
        "generation_tokens_delta": rounded(generation_delta, 0),
        "request_completed_delta": rounded(request_completed_delta, 0),
        "request_error_delta": rounded(request_error_delta, 0),
        "request_abort_delta": rounded(request_abort_delta, 0),
        "prompt_tok_s": rounded(prompt_rate),
        "cached_prompt_tok_s": rounded(cached_rate),
        "uncached_prompt_tok_s": rounded(uncached_rate),
        "cache_hit_ratio_pct": rounded(cache_hit_ratio * 100.0 if cache_hit_ratio is not None else None),
        "generation_tok_s": rounded(generation_rate),
        "request_s": rounded(request_rate, 3),
        "error_s": rounded(error_rate, 3),
        "preemption_delta": preemption_delta,
        "mtp_draft_tokens_delta": rounded(deltas["mtp_draft_tokens_total"], 0),
        "mtp_accepted_tokens_delta": rounded(deltas["mtp_accepted_tokens_total"], 0),
        "mtp_acceptance_pct": rounded(mtp_acceptance * 100.0 if mtp_acceptance is not None else None),
        "ttft_avg_s": rounded(ttft_avg_s, 3),
        "e2e_avg_s": rounded(e2e_avg_s, 3),
        "queue_avg_s": rounded(queue_avg_s, 3),
        "prefill_avg_s": rounded(prefill_avg_s, 3),
        "decode_avg_s": rounded(decode_avg_s, 3),
        "itl_avg_s": rounded(itl_avg_s, 4),
        "request_prompt_tokens_avg": rounded(request_prompt_tokens_avg, 1),
        "request_generation_tokens_avg": rounded(request_generation_tokens_avg, 1),
        "prefill_efficiency_tok_s": rounded(prefill_efficiency),
        "decode_efficiency_tok_s": rounded(decode_efficiency),
        "counters": counters,
    }


async def collect_model_info() -> dict[str, Any]:
    try:
        text = await asyncio.to_thread(run_http_text, VLLM_MODELS_URL, 4.0)
        data = json.loads(text)
        model = (data.get("data") or [{}])[0]
        return {
            "status": "ok",
            "id": model.get("id"),
            "root": model.get("root"),
            "max_model_len": model.get("max_model_len"),
        }
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": str(exc)[-300:]}


async def collect_once() -> dict[str, Any]:
    ts = now()
    node_results = await asyncio.gather(
        *[run_ssh_probe(node) for node in NODES],
        return_exceptions=True,
    )
    nodes: dict[str, Any] = {}
    for config, result in zip(NODES, node_results, strict=True):
        previous = STATE.previous_nodes.get(config["id"])
        elapsed_s = ts - safe_float((previous or {}).get("ts"), ts)
        if isinstance(result, Exception):
            node_payload = {
                "id": config["id"],
                "name": config["name"],
                "host": config["host"],
                "ts": ts,
                "status": "error",
                "health": "critical",
                "error": str(result)[-500:],
            }
        else:
            interfaces = enrich_interfaces(
                config["id"],
                result.get("interfaces", []),
                previous,
                elapsed_s,
                config["roce_ifaces"],
            )
            node_payload = {
                **result,
                "id": config["id"],
                "name": config["name"],
                "host": config["host"],
                "status": "ok",
                "interfaces": interfaces,
            }
            previous_cpu = (previous or {}).get("cpu") or {}
            current_cpu = result.get("cpu") or {}
            current_memory = result.get("memory") or {}
            total_delta = safe_float(current_cpu.get("total")) - safe_float(previous_cpu.get("total"))
            idle_delta = safe_float(current_cpu.get("idle")) - safe_float(previous_cpu.get("idle"))
            node_payload["cpu_used_pct"] = (
                round(max(0.0, min(100.0, (1.0 - idle_delta / total_delta) * 100.0)), 1)
                if total_delta > 0
                else None
            )
            node_payload["mem_used_pct"] = current_memory.get("used_pct")
            previous_memory = (previous or {}).get("memory") or {}
            for direction in ["in", "out"]:
                key = f"swap_{direction}_pages_total"
                previous_value = previous_memory.get(key)
                if previous_value is None:
                    current_memory[f"swap_{direction}_pages_s"] = 0.0
                else:
                    delta = safe_float(current_memory.get(key)) - safe_float(previous_value)
                    current_memory[f"swap_{direction}_pages_s"] = round(max(0.0, delta) / max(0.001, elapsed_s), 3)
            node_payload["summary"] = summarize_node(node_payload)
            node_payload["health"] = node_payload["summary"]["health"]
        nodes[config["id"]] = node_payload
        STATE.previous_nodes[config["id"]] = node_payload

    try:
        metrics_text = await asyncio.to_thread(run_http_text, VLLM_METRICS_URL, 4.0)
        vllm = build_vllm_snapshot(metrics_text, STATE.previous_vllm)
        enrich_recent_vllm_metrics(vllm, STATE.recent_vllm)
    except Exception as exc:  # noqa: BLE001
        vllm = {
            "ts": ts,
            "status": "error",
            "sample_state": "collection_error",
            "event_state": "unknown",
            "deployment_id": DEPLOYMENT_ID,
            "health": "critical",
            "error": str(exc)[-500:],
        }
    STATE.previous_vllm = vllm

    model = await collect_model_info()
    raw_alerts = build_alerts(nodes, vllm, model)
    alerts = ALERT_FILTER.update(raw_alerts, ts)
    overall = "ok"
    if any(item["level"] == "critical" for item in alerts):
        overall = "critical"
    elif any(item["level"] == "warning" for item in alerts):
        overall = "warning"

    return {
        "status": overall,
        "ts": ts,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)),
        "poll_interval_s": POLL_INTERVAL_S,
        "nodes": nodes,
        "vllm": vllm,
        "model": model,
        "alerts": alerts,
    }


def build_alerts(nodes: dict[str, Any], vllm: dict[str, Any], model: dict[str, Any]) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    for node in nodes.values():
        if node.get("status") != "ok":
            alerts.append(
                {
                    "level": "critical",
                    "scope": node.get("name"),
                    "message": node.get("error", "node offline"),
                    "signature": f"node:{node.get('id')}:offline",
                }
            )
            continue
        summary = node.get("summary") or {}
        temp = summary.get("gpu_temp_max_c")
        memory = node.get("memory") or {}
        pressure = memory.get("pressure") or {}
        if temp is not None and temp >= 86:
            alerts.append(
                {
                    "level": "critical",
                    "scope": node["name"],
                    "message": f"GPU 温度 {temp}C",
                    "signature": f"node:{node['id']}:gpu_temp",
                }
            )
        elif temp is not None and temp >= 82:
            alerts.append(
                {
                    "level": "warning",
                    "scope": node["name"],
                    "message": f"GPU 温度 {temp}C",
                    "signature": f"node:{node['id']}:gpu_temp",
                }
            )
        cpu_temp = summary.get("cpu_soc_temp_max_c")
        nvme_temp = summary.get("nvme_temp_max_c")
        nic_temp = summary.get("nic_temp_max_c")
        if cpu_temp is not None and cpu_temp >= 95:
            alerts.append(
                {
                    "level": "critical",
                    "scope": node["name"],
                    "message": f"CPU/SoC 温度 {cpu_temp}C",
                    "signature": f"node:{node['id']}:cpu_temp",
                }
            )
        elif cpu_temp is not None and cpu_temp >= 85:
            alerts.append(
                {
                    "level": "warning",
                    "scope": node["name"],
                    "message": f"CPU/SoC 温度 {cpu_temp}C",
                    "signature": f"node:{node['id']}:cpu_temp",
                }
            )
        if nvme_temp is not None and nvme_temp >= 70:
            alerts.append(
                {
                    "level": "critical" if nvme_temp >= 80 else "warning",
                    "scope": node["name"],
                    "message": f"NVMe 温度 {nvme_temp}C",
                    "signature": f"node:{node['id']}:nvme_temp",
                }
            )
        if nic_temp is not None and nic_temp >= 80:
            alerts.append(
                {
                    "level": "critical" if nic_temp >= 90 else "warning",
                    "scope": node["name"],
                    "message": f"200G 网卡温度 {nic_temp}C",
                    "signature": f"node:{node['id']}:nic_temp",
                }
            )
        psi = safe_float(pressure.get("some_avg10"))
        swap_out = safe_float(memory.get("swap_out_pages_s"))
        if psi >= 10 or swap_out >= 256:
            alerts.append(
                {
                    "level": "critical" if psi >= 50 or swap_out >= 2560 else "warning",
                    "scope": node["name"],
                    "message": f"内存压力 PSI={psi:.1f}，换出={swap_out:.1f} 页/秒",
                    "signature": f"node:{node['id']}:memory_pressure",
                }
            )
        for iface in node.get("interfaces", []):
            if iface.get("is_roce") and iface.get("health") != "ok":
                alerts.append(
                    {
                        "level": "warning" if iface.get("operstate") == "up" else "critical",
                        "scope": f"{node['name']}:{iface['name']}",
                        "message": f"RoCE {iface.get('operstate')} {iface.get('speed_mbps')} Mbps 错误/丢弃={iface.get('error_delta', 0) + iface.get('drop_delta', 0)}",
                        "signature": f"roce:{node['id']}:{iface['name']}",
                    }
                )
    if vllm.get("status") != "ok":
        alerts.append(
            {
                "level": "critical",
                "scope": "vLLM",
                "message": vllm.get("error", "metrics unavailable"),
                "signature": "vllm:metrics:error",
            }
        )
    else:
        if vllm.get("sample_state") == "metrics_missing":
            alerts.append(
                {
                    "level": "warning",
                    "scope": "vLLM",
                    "message": f"核心指标缺失：{', '.join(vllm.get('missing_metrics') or [])}",
                    "signature": "vllm:metrics:missing",
                }
            )
        if safe_float(vllm.get("waiting")) > 0:
            alerts.append(
                {
                    "level": "warning",
                    "scope": "vLLM",
                    "message": f"等待请求 {vllm.get('waiting')}",
                    "signature": "vllm:waiting",
                }
            )
        kv_cache = safe_float(vllm.get("kv_cache_usage_pct"))
        if kv_cache >= 80:
            alerts.append(
                {
                    "level": "critical" if kv_cache >= 90 else "warning",
                    "scope": "vLLM",
                    "message": f"KV 缓存 {vllm.get('kv_cache_usage_pct')}%",
                    "signature": "vllm:kv_cache",
                }
            )
        if int(vllm.get("preemption_delta") or 0) > 0:
            alerts.append(
                {
                    "level": "critical",
                    "scope": "vLLM",
                    "message": "发生抢占",
                    "signature": "vllm:preemption",
                }
            )
    if model.get("status") != "ok":
        alerts.append(
            {
                "level": "critical",
                "scope": "model",
                "message": model.get("error", "model unavailable"),
                "signature": "model:unavailable",
            }
        )
    return alerts


def next_cleanup_ts(from_ts: float | None = None) -> float:
    current = datetime.fromtimestamp(from_ts or now())
    target = current.replace(hour=3, minute=0, second=0, microsecond=0)
    if current >= target:
        target += timedelta(days=1)
    return target.timestamp()


async def poll_loop() -> None:
    global NEXT_CLEANUP_TS
    while True:
        try:
            snapshot = await collect_once()
        except Exception as exc:  # noqa: BLE001
            snapshot = {
                "status": "critical",
                "ts": now(),
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "nodes": {},
                "vllm": {},
                "model": {},
                "alerts": [{"level": "critical", "scope": "collector", "message": str(exc)[-500:]}],
            }
        await STATE.update(snapshot)
        try:
            await asyncio.to_thread(STORE.insert_snapshot, snapshot)
        except Exception as exc:  # noqa: BLE001
            print(f"sqlite insert failed: {exc}")
        current_ts = now()
        if current_ts >= NEXT_CLEANUP_TS:
            try:
                await asyncio.to_thread(STORE.cleanup_retention, 60)
            except Exception as exc:  # noqa: BLE001
                print(f"sqlite cleanup failed: {exc}")
            NEXT_CLEANUP_TS = next_cleanup_ts(current_ts)
        await asyncio.sleep(POLL_INTERVAL_S)


@app.on_event("startup")
async def startup() -> None:
    global NEXT_CLEANUP_TS
    await asyncio.to_thread(STORE.cleanup_retention, 60)
    STATE.recent_vllm = await asyncio.to_thread(STORE.recent_vllm_samples, int(RECENT_VLLM_TTL_S))
    ALERT_FILTER.seed(await asyncio.to_thread(STORE.active_alert_payloads), now())
    NEXT_CLEANUP_TS = next_cleanup_ts()
    asyncio.create_task(poll_loop())


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.head("/")
async def index_head() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
async def health() -> dict[str, Any]:
    snapshot = await STATE.snapshot()
    return {
        "status": snapshot.get("status"),
        "uptime_s": round(now() - STATE.started_at, 1),
        "updated_at": snapshot.get("updated_at"),
        "database": await asyncio.to_thread(STORE.database_health),
    }


@app.get("/api/snapshot")
async def snapshot() -> JSONResponse:
    return JSONResponse(await STATE.snapshot())


@app.get("/api/history")
async def history(
    kind: str | None = None,
    metric: str | None = None,
    node: str | None = None,
    window: str = "24h",
    bucket: str = "raw",
    limit: int = 240,
) -> JSONResponse:
    if kind and metric:
        return JSONResponse(
            STORE.series(
                kind=kind,
                metric=metric,
                window_seconds=parse_duration_seconds(window, 86400),
                bucket_seconds=parse_duration_seconds(bucket, 0),
                node_id=node,
            )
        )
    payload = await STATE.history_payload(max(1, min(2000, limit)))
    return JSONResponse({"points": [slim_history_point(point) for point in payload["points"]]})


@app.get("/api/stats")
async def stats(window: str = "24h") -> JSONResponse:
    return JSONResponse(STORE.stats(parse_duration_seconds(window, 86400)))


@app.get("/api/trends")
async def trends(
    kind: str,
    metric: str,
    node: str | None = None,
    window: str = "7d",
    bucket: str = "1h",
) -> JSONResponse:
    return JSONResponse(
        STORE.series(
            kind=kind,
            metric=metric,
            window_seconds=parse_duration_seconds(window, 7 * 86400),
            bucket_seconds=parse_duration_seconds(bucket, 0),
            node_id=node,
        )
    )


@app.get("/api/alerts")
async def alerts(window: str = "24h") -> JSONResponse:
    return JSONResponse(STORE.alerts(parse_duration_seconds(window, 86400)))


@app.get("/api/analysis")
async def analysis() -> JSONResponse:
    datasets = await asyncio.gather(
        *[asyncio.to_thread(STORE.analysis_rows, window) for window in [900, 3600, 86400]]
    )
    return JSONResponse(
        analyze_windows(
            {dataset["window_seconds"]: dataset for dataset in datasets},
            POLL_INTERVAL_S,
        )
    )


def slim_history_point(point: dict[str, Any]) -> dict[str, Any]:
    vllm = point.get("vllm") or {}
    return {
        "ts": point.get("ts"),
        "updated_at": point.get("updated_at"),
        "status": point.get("status"),
        "vllm": {
            key: vllm.get(key)
            for key in [
                "sample_state",
                "event_state",
                "prompt_tok_s",
                "cached_prompt_tok_s",
                "uncached_prompt_tok_s",
                "generation_tok_s",
                "prefill_efficiency_tok_s",
                "decode_efficiency_tok_s",
                "mtp_acceptance_pct",
                "kv_cache_usage_pct",
                "running",
                "waiting",
                "ttft_avg_s",
                "e2e_avg_s",
            ]
        },
        "nodes": {
            node_id: {
                "health": node.get("health"),
                "summary": node.get("summary"),
            }
            for node_id, node in (point.get("nodes") or {}).items()
        },
    }


@app.websocket("/ws")
async def websocket(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        while True:
            await websocket.send_json(await STATE.snapshot())
            await asyncio.sleep(POLL_INTERVAL_S)
    except WebSocketDisconnect:
        return


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
