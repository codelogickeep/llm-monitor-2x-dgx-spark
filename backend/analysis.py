from __future__ import annotations

import math
import statistics
import time
from collections import defaultdict
from typing import Any, Callable


SEVERITY_ORDER = {"ok": 0, "idle": 0, "insufficient": 1, "warning": 2, "critical": 3}
WINDOW_LABELS = {900: "最近 15 分钟", 3600: "最近 1 小时", 86400: "最近 24 小时"}


def _number(value: Any) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _values(rows: list[dict[str, Any]], key: str) -> list[float]:
    return [value for row in rows if (value := _number(row.get(key))) is not None]


def _mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round((len(ordered) - 1) * 0.95))]


def _fmt(value: float | None, digits: int = 1) -> str:
    return "--" if value is None else f"{value:.{digits}f}"


def _coverage(rows: list[dict[str, Any]], keys: list[str], window_seconds: int, poll_interval_s: float) -> float:
    usable = [row for row in rows if all(_number(row.get(key)) is not None for key in keys)]
    if not usable:
        return 0.0
    timestamps = [_number(row.get("ts")) for row in usable]
    clean_ts = [value for value in timestamps if value is not None]
    bucket_seconds = max(15.0, poll_interval_s * 4.0)
    buckets = {int(value // bucket_seconds) for value in clean_ts}
    bucket_ratio = len(buckets) / max(1.0, window_seconds / bucket_seconds)
    span_ratio = ((max(clean_ts) - min(clean_ts) + poll_interval_s) / window_seconds) if clean_ts else 0.0
    return round(max(0.0, min(1.0, bucket_ratio, span_ratio)), 3)


def _longest_duration(
    rows: list[dict[str, Any]],
    predicate: Callable[[dict[str, Any]], bool],
    poll_interval_s: float,
) -> float:
    longest = 0.0
    current = 0.0
    previous_ts: float | None = None
    timestamps = [_number(row.get("ts")) for row in rows]
    clean_ts = [value for value in timestamps if value is not None]
    observed_gaps = [
        right - left
        for left, right in zip(clean_ts, clean_ts[1:], strict=False)
        if 0 < right - left <= 300
    ]
    expected_gap = statistics.median(observed_gaps) if observed_gaps else poll_interval_s
    for row in rows:
        ts = _number(row.get("ts"))
        if ts is None or not predicate(row):
            current = 0.0
            previous_ts = ts
            continue
        gap = expected_gap if previous_ts is None else max(0.0, ts - previous_ts)
        current = expected_gap if gap > expected_gap * 3 else current + gap
        longest = max(longest, current)
        previous_ts = ts
    return longest


def _item(
    item_id: str,
    title: str,
    severity: str,
    conclusion: str,
    evidence: str,
    coverage: float,
) -> dict[str, Any]:
    return {
        "id": item_id,
        "title": title,
        "severity": severity,
        "conclusion": conclusion,
        "evidence": evidence,
        "coverage": round(coverage * 100.0, 1),
        "provisional": coverage < 0.8,
    }


def _thermal_analysis(
    nodes: dict[str, list[dict[str, Any]]],
    window_seconds: int,
    poll_interval_s: float,
) -> dict[str, Any]:
    sensors = {
        "cpu_soc_temp_max_c": ("CPU/SoC", 85.0, 95.0),
        "gpu_temp_max_c": ("GPU", 82.0, 86.0),
        "nvme_temp_max_c": ("NVMe", 70.0, 80.0),
        "nic_temp_max_c": ("200G 网卡", 80.0, 90.0),
    }
    coverage = min(
        (_coverage(rows, list(sensors), window_seconds, poll_interval_s) for rows in nodes.values()),
        default=0.0,
    )
    worst_severity = "ok"
    worst_label = ""
    worst_temp: float | None = None
    worst_duration = 0.0
    evidence_parts = []
    for node_id, rows in nodes.items():
        for key, (label, warning, critical) in sensors.items():
            values = _values(rows, key)
            if not values:
                continue
            maximum = max(values)
            p95 = _p95(values)
            critical_duration = _longest_duration(rows, lambda row, k=key, t=critical: (_number(row.get(k)) or -999) >= t, poll_interval_s)
            warning_duration = _longest_duration(rows, lambda row, k=key, t=warning: (_number(row.get(k)) or -999) >= t, poll_interval_s)
            severity = "critical" if critical_duration >= 120 else "warning" if warning_duration >= 300 else "ok"
            severity_order = SEVERITY_ORDER[severity]
            worst_order = SEVERITY_ORDER[worst_severity]
            if severity_order > worst_order or (severity_order == worst_order and (worst_temp is None or maximum > worst_temp)):
                worst_severity = severity
                worst_label = f"{node_id} {label}"
                worst_temp = maximum
                worst_duration = max(critical_duration, warning_duration)
            evidence_parts.append(f"{node_id} {label} P95 {_fmt(p95)}°C/峰值 {_fmt(maximum)}°C")
    severity = worst_severity if coverage >= 0.8 else "insufficient"
    if worst_temp is None:
        conclusion = "温度传感器数据尚不可用"
    elif worst_severity == "critical":
        conclusion = f"{worst_label} 持续高温，存在降频风险"
    elif worst_severity == "warning":
        conclusion = f"{worst_label} 温度偏高，建议检查散热"
    else:
        conclusion = f"热状态正常，最高 {worst_temp:.1f}°C"
    if coverage < 0.8:
        conclusion = f"样本积累中；当前观察：{conclusion}"
    return _item("thermal", "热状态", severity, conclusion, "；".join(evidence_parts) or "暂无传感器样本", coverage)


def _memory_analysis(
    nodes: dict[str, list[dict[str, Any]]],
    window_seconds: int,
    poll_interval_s: float,
) -> dict[str, Any]:
    keys = ["mem_available_mib", "swap_used_pct", "swap_in_pages_s", "swap_out_pages_s", "memory_psi_some_avg10"]
    coverage = min((_coverage(rows, keys, window_seconds, poll_interval_s) for rows in nodes.values()), default=0.0)
    severity = "ok"
    worst_node = ""
    max_psi = 0.0
    max_swap = 0.0
    max_swap_io = 0.0
    min_available: float | None = None
    evidence_parts = []
    for node_id, rows in nodes.items():
        psi = max(_values(rows, "memory_psi_some_avg10"), default=0.0)
        swap = max(_values(rows, "swap_used_pct"), default=0.0)
        swap_io_values = [
            (_number(row.get("swap_in_pages_s")) or 0) + (_number(row.get("swap_out_pages_s")) or 0)
            for row in rows
            if (_number(row.get("swap_in_pages_s")) or 0) <= 1_000_000
            and (_number(row.get("swap_out_pages_s")) or 0) <= 1_000_000
        ]
        swap_io = max(swap_io_values, default=0.0)
        available_values = _values(rows, "mem_available_mib")
        available = min(available_values) if available_values else None
        critical_duration = _longest_duration(
            rows,
            lambda row: (_number(row.get("memory_psi_some_avg10")) or 0) >= 50
            or ((_number(row.get("swap_out_pages_s")) or 0) >= 2560 and (_number(row.get("swap_used_pct")) or 0) >= 80),
            poll_interval_s,
        )
        warning_duration = _longest_duration(
            rows,
            lambda row: (_number(row.get("memory_psi_some_avg10")) or 0) >= 10
            or ((_number(row.get("swap_out_pages_s")) or 0) >= 256 and (_number(row.get("swap_used_pct")) or 0) >= 50),
            poll_interval_s,
        )
        node_severity = "critical" if critical_duration >= 300 else "warning" if warning_duration >= 300 else "ok"
        if SEVERITY_ORDER[node_severity] > SEVERITY_ORDER[severity]:
            severity = node_severity
            worst_node = node_id
        max_psi = max(max_psi, psi)
        max_swap = max(max_swap, swap)
        max_swap_io = max(max_swap_io, swap_io)
        if available is not None:
            min_available = available if min_available is None else min(min_available, available)
        evidence_parts.append(
            f"{node_id} PSI {psi:.2f}，Swap {swap:.1f}%，可用 {_fmt((available or 0) / 1024)} GiB"
        )
    formal_severity = severity if coverage >= 0.8 else "insufficient"
    if severity == "critical":
        conclusion = f"{worst_node} 内存压力严重，推理可能发生换页阻塞"
    elif severity == "warning":
        conclusion = f"{worst_node} 出现持续内存压力或活跃换页"
    else:
        conclusion = "统一内存占用较高但无持续压力或活跃换页"
    if coverage < 0.8:
        conclusion = f"样本积累中；当前观察：{conclusion}"
    evidence = f"峰值 PSI {max_psi:.2f}，Swap 峰值 {max_swap:.1f}%，换页峰值 {max_swap_io:.1f} 页/秒；" + "；".join(evidence_parts)
    return _item("memory", "内存压力", formal_severity, conclusion, evidence, coverage)


def _throttling_analysis(
    nodes: dict[str, list[dict[str, Any]]],
    window_seconds: int,
    poll_interval_s: float,
) -> dict[str, Any]:
    keys = ["gpu_util_avg_pct", "gpu_sm_clock_pct", "gpu_throttle_active", "gpu_pstate_numeric"]
    coverage = min((_coverage(rows, keys, window_seconds, poll_interval_s) for rows in nodes.values()), default=0.0)
    active_rows = [
        row
        for rows in nodes.values()
        for row in rows
        if (_number(row.get("gpu_util_avg_pct")) or 0) >= 10
        and _number(row.get("gpu_sm_clock_pct")) is not None
    ]
    if not active_rows:
        conclusion = "时间窗内没有持续 GPU 负载，降频判断处于待机状态"
        return _item("throttling", "GPU 降频", "idle" if coverage >= 0.8 else "insufficient", conclusion, "仅在 GPU 利用率不低于 10% 时评估", coverage)
    severity = "ok"
    evidence_parts = []
    for node_id, rows in nodes.items():
        active = [
            row
            for row in rows
            if (_number(row.get("gpu_util_avg_pct")) or 0) >= 10
            and _number(row.get("gpu_sm_clock_pct")) is not None
        ]
        if not active:
            continue
        critical_duration = _longest_duration(
            active,
            lambda row: (_number(row.get("gpu_throttle_active")) or 0) > 0
            or (_number(row.get("gpu_sm_clock_pct")) or 100) < 60,
            poll_interval_s,
        )
        warning_duration = _longest_duration(
            active,
            lambda row: (_number(row.get("gpu_throttle_active")) or 0) > 0
            or (_number(row.get("gpu_sm_clock_pct")) or 100) < 80
            or (_number(row.get("gpu_pstate_numeric")) or 0) > 0,
            poll_interval_s,
        )
        node_severity = "critical" if critical_duration >= 120 else "warning" if warning_duration >= 300 else "ok"
        if SEVERITY_ORDER[node_severity] > SEVERITY_ORDER[severity]:
            severity = node_severity
        evidence_parts.append(
            f"{node_id} 活跃 SM 时钟中位数 {_fmt(_median(_values(active, 'gpu_sm_clock_pct')))}%，P-State P{_fmt(_p95(_values(active, 'gpu_pstate_numeric')), 0)}"
        )
    formal_severity = severity if coverage >= 0.8 else "insufficient"
    conclusion = "没有发现持续降频迹象"
    if severity == "warning":
        conclusion = "检测到疑似持续降频，需要结合温度和功耗检查"
    elif severity == "critical":
        conclusion = "检测到严重降频，推理性能可能受限"
    if coverage < 0.8:
        conclusion = f"样本积累中；当前观察：{conclusion}"
    return _item("throttling", "GPU 降频", formal_severity, conclusion, "；".join(evidence_parts), coverage)


def _active_bins(vllm_rows: list[dict[str, Any]]) -> set[int]:
    bins: set[int] = set()
    for row in vllm_rows:
        active = (_number(row.get("running")) or 0) > 0 or (_number(row.get("prompt_tok_s")) or 0) > 0 or (_number(row.get("generation_tok_s")) or 0) > 0
        ts = _number(row.get("ts"))
        if not active or ts is None:
            continue
        bucket = int(ts // 10)
        bins.update({bucket - 1, bucket, bucket + 1})
    return bins


def _balance_analysis(
    nodes: dict[str, list[dict[str, Any]]],
    vllm_rows: list[dict[str, Any]],
    window_seconds: int,
    poll_interval_s: float,
) -> dict[str, Any]:
    coverage = min((_coverage(rows, ["gpu_util_avg_pct", "power_total_w"], window_seconds, poll_interval_s) for rows in nodes.values()), default=0.0)
    bins = _active_bins(vllm_rows)
    active_by_node: dict[str, list[dict[str, Any]]] = {}
    for node_id, rows in nodes.items():
        active_by_node[node_id] = [row for row in rows if int((_number(row.get("ts")) or 0) // 10) in bins]
    if len(active_by_node) < 2 or any(len(rows) < 3 for rows in active_by_node.values()):
        return _item("balance", "双机负载", "idle" if coverage >= 0.8 else "insufficient", "时间窗内双机活跃负载样本不足，暂不判断失衡", "仅在推理请求运行期间比较两台 GPU", coverage)
    node_ids = sorted(active_by_node)[:2]
    util = {node_id: _mean(_values(active_by_node[node_id], "gpu_util_avg_pct")) or 0.0 for node_id in node_ids}
    power = {node_id: _mean(_values(active_by_node[node_id], "power_total_w")) or 0.0 for node_id in node_ids}
    util_diff = abs(util[node_ids[0]] - util[node_ids[1]])
    power_diff = abs(power[node_ids[0]] - power[node_ids[1]])
    active_duration = min(len(active_by_node[node_id]) for node_id in node_ids) * poll_interval_s
    severity = "ok"
    if active_duration >= 900 and util_diff > 50:
        severity = "critical"
    elif active_duration >= 900 and util_diff > 20:
        severity = "warning"
    formal_severity = severity if coverage >= 0.8 else "insufficient"
    conclusion = f"双机负载均衡，GPU 利用率差 {util_diff:.1f}%"
    if severity == "warning":
        conclusion = f"双机负载不均衡，GPU 利用率差 {util_diff:.1f}%"
    elif severity == "critical":
        conclusion = f"双机负载严重失衡，GPU 利用率差 {util_diff:.1f}%"
    if active_duration < 900:
        conclusion = f"活跃负载仅覆盖 {active_duration / 60:.1f} 分钟，先记录差值不告警"
        formal_severity = "idle" if coverage >= 0.8 else "insufficient"
    if coverage < 0.8:
        conclusion = f"样本积累中；当前观察：{conclusion}"
    evidence = f"{node_ids[0]} 平均 {util[node_ids[0]]:.1f}%/{power[node_ids[0]]:.1f}W，{node_ids[1]} 平均 {util[node_ids[1]]:.1f}%/{power[node_ids[1]]:.1f}W，功耗差 {power_diff:.1f}W"
    return _item("balance", "双机负载", formal_severity, conclusion, evidence, coverage)


def _roce_analysis(
    nodes: dict[str, list[dict[str, Any]]],
    window_seconds: int,
    poll_interval_s: float,
) -> dict[str, Any]:
    keys = ["roce_link_up", "roce_link_speed_min_mbps", "roce_error_delta", "roce_rx_mbps", "roce_tx_mbps"]
    coverage = min((_coverage(rows, keys, window_seconds, poll_interval_s) for rows in nodes.values()), default=0.0)
    severity = "ok"
    evidence_parts = []
    for node_id, rows in nodes.items():
        down_duration = _longest_duration(
            rows,
            lambda row: _number(row.get("roce_link_up")) is not None
            and (_number(row.get("roce_link_up")) or 0) < 1,
            poll_interval_s,
        )
        slow_duration = _longest_duration(
            rows,
            lambda row: _number(row.get("roce_link_speed_min_mbps")) is not None
            and (_number(row.get("roce_link_speed_min_mbps")) or 0) < 200000,
            poll_interval_s,
        )
        errors = sum(_values(rows, "roce_error_delta"))
        error_duration = _longest_duration(
            rows,
            lambda row: (_number(row.get("roce_error_delta")) or 0) > 0,
            poll_interval_s,
        )
        node_severity = (
            "critical"
            if down_duration >= 60 or (errors > 100 and error_duration >= 300)
            else "warning"
            if slow_duration >= 300 or error_duration >= 300
            else "ok"
        )
        if SEVERITY_ORDER[node_severity] > SEVERITY_ORDER[severity]:
            severity = node_severity
        speed = min(_values(rows, "roce_link_speed_min_mbps"), default=0.0) / 1000.0
        throughput = max(
            [(_number(row.get("roce_rx_mbps")) or 0) + (_number(row.get("roce_tx_mbps")) or 0) for row in rows],
            default=0.0,
        )
        evidence_parts.append(f"{node_id} {speed:.0f} Gb/s，错误/丢弃 {errors:.0f}，峰值流量 {throughput:.1f} Mb/s")
    formal_severity = severity if coverage >= 0.8 else "insufficient"
    conclusion = "200G RoCE 链路正常，未发现持续错误"
    if severity == "warning":
        conclusion = "RoCE 出现降速或错误，需要检查链路计数器"
    elif severity == "critical":
        conclusion = "RoCE 链路故障，可能直接限制双机推理"
    if coverage < 0.8:
        conclusion = f"样本积累中；当前观察：{conclusion}"
    return _item("roce", "高速互联", formal_severity, conclusion, "；".join(evidence_parts), coverage)


def _inference_analysis(
    rows: list[dict[str, Any]],
    baseline_rows: list[dict[str, Any]],
    window_seconds: int,
    poll_interval_s: float,
) -> dict[str, Any]:
    coverage = _coverage(rows, ["running", "generation_tok_s", "prompt_tok_s", "error_s"], window_seconds, poll_interval_s)
    active = [row for row in rows if (_number(row.get("running")) or 0) > 0 or (_number(row.get("generation_tok_s")) or 0) > 0 or (_number(row.get("prompt_tok_s")) or 0) > 0]
    if not active:
        return _item("inference", "推理性能", "idle" if coverage >= 0.8 else "insufficient", "时间窗内没有推理请求，服务处于待机状态", "空闲时的 0 tok/s 不计为性能下降", coverage)
    generation = _median([value for value in _values(active, "generation_tok_s") if value > 0])
    prompt = _median([value for value in _values(active, "prompt_tok_s") if value > 0])
    ttft = _median(_values(active, "ttft_avg_s"))
    waiting_duration = _longest_duration(active, lambda row: (_number(row.get("waiting")) or 0) > 0, poll_interval_s)
    requests = sum(_values(active, "request_s"))
    errors = sum(_values(active, "error_s"))
    error_ratio = errors / max(0.0001, requests + errors)
    baseline_active = [row for row in baseline_rows if (_number(row.get("generation_tok_s")) or 0) > 0]
    baseline_generation = _median(_values(baseline_active, "generation_tok_s"))
    baseline_ttft = _median(_values(baseline_active, "ttft_avg_s"))
    severity = "ok"
    reasons = []
    if error_ratio > 0.05:
        severity = "critical"
        reasons.append(f"错误率 {error_ratio * 100:.1f}%")
    elif error_ratio > 0.01:
        severity = "warning"
        reasons.append(f"错误率 {error_ratio * 100:.1f}%")
    if waiting_duration >= 300 and SEVERITY_ORDER[severity] < SEVERITY_ORDER["warning"]:
        severity = "warning"
        reasons.append("请求持续排队")
    if baseline_generation and generation is not None and len(baseline_active) >= 10:
        ratio = generation / max(0.001, baseline_generation)
        if ratio < 0.4:
            severity = "critical"
            reasons.append(f"吞吐仅为 24 小时基线的 {ratio * 100:.0f}%")
        elif ratio < 0.7 and SEVERITY_ORDER[severity] < SEVERITY_ORDER["warning"]:
            severity = "warning"
            reasons.append(f"吞吐为 24 小时基线的 {ratio * 100:.0f}%")
    if baseline_ttft and ttft and len(_values(baseline_active, "ttft_avg_s")) >= 5:
        if ttft > baseline_ttft * 4 and ttft - baseline_ttft > 5:
            severity = "critical"
            reasons.append("TTFT 显著高于历史基线")
        elif ttft > baseline_ttft * 2 and ttft - baseline_ttft > 2 and SEVERITY_ORDER[severity] < SEVERITY_ORDER["warning"]:
            severity = "warning"
            reasons.append("TTFT 高于历史基线")
    formal_severity = severity if coverage >= 0.8 else "insufficient"
    conclusion = "推理性能稳定" if not reasons else "，".join(reasons)
    if coverage < 0.8:
        conclusion = f"样本积累中；当前观察：{conclusion}"
    evidence = f"活跃中位数：生成 {_fmt(generation)} tok/s，提示 {_fmt(prompt)} tok/s，TTFT {_fmt(ttft, 2)}s；24 小时生成基线 {_fmt(baseline_generation)} tok/s"
    return _item("inference", "推理性能", formal_severity, conclusion, evidence, coverage)


def analyze_windows(
    datasets: dict[int, dict[str, Any]],
    poll_interval_s: float,
) -> dict[str, Any]:
    baseline_rows = datasets.get(86400, {}).get("vllm") or []
    windows = []
    for window_seconds in [900, 3600, 86400]:
        dataset = datasets[window_seconds]
        grouped_nodes: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in dataset.get("nodes") or []:
            grouped_nodes[str(row.get("node_id"))].append(row)
        vllm_rows = dataset.get("vllm") or []
        items = [
            _thermal_analysis(grouped_nodes, window_seconds, poll_interval_s),
            _memory_analysis(grouped_nodes, window_seconds, poll_interval_s),
            _throttling_analysis(grouped_nodes, window_seconds, poll_interval_s),
            _balance_analysis(grouped_nodes, vllm_rows, window_seconds, poll_interval_s),
            _roce_analysis(grouped_nodes, window_seconds, poll_interval_s),
            _inference_analysis(vllm_rows, baseline_rows, window_seconds, poll_interval_s),
        ]
        coverage = min((item["coverage"] for item in items), default=0.0)
        formal = [item for item in items if item["severity"] not in {"insufficient", "idle"}]
        overall = max(formal, key=lambda item: SEVERITY_ORDER[item["severity"]])["severity"] if formal else "insufficient"
        if coverage < 80:
            overall = "insufficient"
            summary = f"新指标正在积累样本，当前覆盖率 {coverage:.1f}%"
        elif overall == "critical":
            summary = "发现严重风险，需要立即检查异常项"
        elif overall == "warning":
            summary = "发现需要关注的持续异常"
        else:
            summary = "时间窗内核心硬件与推理服务运行正常"
        windows.append(
            {
                "window": f"{window_seconds}s",
                "window_seconds": window_seconds,
                "label": WINDOW_LABELS[window_seconds],
                "status": overall,
                "coverage": round(coverage, 1),
                "summary": summary,
                "items": items,
            }
        )
    return {"generated_at": time.time(), "windows": windows}
