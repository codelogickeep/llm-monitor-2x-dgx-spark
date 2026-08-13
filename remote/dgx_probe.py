#!/usr/bin/env python3
"""Read-only DGX Spark health probe for the Pi monitor."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any


NA_VALUES = {"", "[N/A]", "N/A", "nan", "None"}


def read_text(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except (FileNotFoundError, PermissionError, OSError):
        return None


def read_int(path: Path) -> int | None:
    value = read_text(path)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def parse_number(value: str) -> float | None:
    value = value.strip()
    if value in NA_VALUES:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def run(command: list[str], timeout: float = 3.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        check=False,
        text=True,
        timeout=timeout,
    )


def collect_gpu() -> list[dict[str, Any]]:
    query = (
        "name,temperature.gpu,utilization.gpu,utilization.memory,"
        "memory.used,memory.total,power.draw,power.limit,pstate,clocks.sm,"
        "clocks.max.sm,clocks_throttle_reasons.active"
    )
    proc = run(
        [
            "nvidia-smi",
            f"--query-gpu={query}",
            "--format=csv,noheader,nounits",
        ],
        timeout=4.0,
    )
    if proc.returncode != 0:
        return [
            {
                "status": "error",
                "error": (proc.stderr or proc.stdout).strip()[-300:],
            }
        ]

    gpus: list[dict[str, Any]] = []
    for index, line in enumerate(proc.stdout.splitlines()):
        parts = [part.strip() for part in line.split(",")]
        while len(parts) < 12:
            parts.append("")
        gpus.append(
            {
                "index": index,
                "name": parts[0],
                "temperature_c": parse_number(parts[1]),
                "gpu_util_pct": parse_number(parts[2]),
                "memory_util_pct": parse_number(parts[3]),
                "memory_used_mib": parse_number(parts[4]),
                "memory_total_mib": parse_number(parts[5]),
                "power_w": parse_number(parts[6]),
                "power_limit_w": parse_number(parts[7]),
                "pstate": None if parts[8] in NA_VALUES else parts[8],
                "sm_clock_mhz": parse_number(parts[9]),
                "sm_clock_max_mhz": parse_number(parts[10]),
                "clock_throttle_reasons": None if parts[11] in NA_VALUES else parts[11],
                "status": "ok",
            }
        )
    return gpus


def collect_interfaces(names: list[str]) -> list[dict[str, Any]]:
    interfaces: list[dict[str, Any]] = []
    for name in names:
        base = Path("/sys/class/net") / name
        stats = base / "statistics"
        item: dict[str, Any] = {"name": name, "exists": base.exists()}
        if not base.exists():
            item["status"] = "missing"
            interfaces.append(item)
            continue

        item.update(
            {
                "operstate": read_text(base / "operstate"),
                "speed_mbps": read_int(base / "speed"),
                "rx_bytes": read_int(stats / "rx_bytes"),
                "tx_bytes": read_int(stats / "tx_bytes"),
                "rx_errors": read_int(stats / "rx_errors"),
                "tx_errors": read_int(stats / "tx_errors"),
                "rx_dropped": read_int(stats / "rx_dropped"),
                "tx_dropped": read_int(stats / "tx_dropped"),
                "source": "netdev",
                "status": "ok",
            }
        )
        interfaces.append(item)
    return interfaces


def parse_ib_state(value: str | None) -> str | None:
    if value is None:
        return None
    return "up" if value.startswith("4:") or value.upper().endswith("ACTIVE") else value


def parse_ib_rate_mbps(value: str | None) -> int | None:
    if not value:
        return None
    parts = value.replace("Gb/sec", "Gbps").split()
    for part in parts:
        try:
            return int(float(part) * 1000)
        except ValueError:
            continue
    return None


def collect_infiniband() -> list[dict[str, Any]]:
    base = Path("/sys/class/infiniband")
    if not base.exists():
        return []

    interfaces: list[dict[str, Any]] = []
    for device in sorted(base.iterdir()):
        port = device / "ports" / "1"
        counters = port / "counters"
        if not counters.exists():
            continue
        state = parse_ib_state(read_text(port / "state"))
        if state != "up":
            continue
        rcv_data = read_int(counters / "port_rcv_data")
        xmit_data = read_int(counters / "port_xmit_data")
        interfaces.append(
            {
                "exists": True,
                "name": f"ib:{device.name}:1",
                "operstate": state,
                "speed_mbps": parse_ib_rate_mbps(read_text(port / "rate")),
                "rx_bytes": (rcv_data or 0) * 4,
                "tx_bytes": (xmit_data or 0) * 4,
                "rx_errors": read_int(counters / "port_rcv_errors") or 0,
                "tx_errors": read_int(counters / "port_xmit_discards") or 0,
                "rx_dropped": read_int(counters / "port_rcv_remote_physical_errors") or 0,
                "tx_dropped": 0,
                "xmit_wait": read_int(counters / "port_xmit_wait") or 0,
                "source": "infiniband",
                "status": "ok",
                "is_roce": True,
            }
        )
    return interfaces


def collect_cpu() -> dict[str, Any]:
    parts = Path("/proc/stat").read_text().splitlines()[0].split()[1:]
    values = [int(value) for value in parts]
    idle = values[3] + values[4]
    total = sum(values)
    return {"idle": idle, "total": total}


def millidegrees_to_c(value: int | None) -> float | None:
    if value is None:
        return None
    return round(value / 1000.0 if abs(value) >= 1000 else float(value), 1)


def collect_thermals() -> dict[str, Any]:
    readings: list[dict[str, Any]] = []
    hwmon_root = Path("/sys/class/hwmon")
    if hwmon_root.exists():
        for device in sorted(hwmon_root.glob("hwmon*")):
            name = read_text(device / "name") or device.name
            for temp_input in sorted(device.glob("temp*_input")):
                match = re.match(r"temp(\d+)_input", temp_input.name)
                label = read_text(device / f"temp{match.group(1)}_label") if match else None
                value = millidegrees_to_c(read_int(temp_input))
                if value is None or value < -20 or value > 150:
                    continue
                readings.append(
                    {
                        "source": name,
                        "label": label or temp_input.stem,
                        "temperature_c": value,
                    }
                )

    def maximum_for(source: str) -> float | None:
        values = [
            item["temperature_c"]
            for item in readings
            if item["source"].lower() == source
        ]
        return max(values) if values else None

    return {
        "cpu_soc_temp_max_c": maximum_for("acpitz"),
        "nvme_temp_max_c": maximum_for("nvme"),
        "nic_temp_max_c": maximum_for("mlx5"),
        "readings": readings,
    }


def parse_pressure(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {}
    text = read_text(path) or ""
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        prefix = parts[0]
        for item in parts[1:]:
            key, _, value = item.partition("=")
            if not key or not value:
                continue
            try:
                result[f"{prefix}_{key}"] = float(value) if key != "total" else int(value)
            except ValueError:
                continue
    return result


def collect_memory() -> dict[str, Any]:
    meminfo: dict[str, int] = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, _, rest = line.partition(":")
        value = rest.strip().split()[0]
        try:
            meminfo[key] = int(value)
        except ValueError:
            continue
    total_kib = meminfo.get("MemTotal", 0)
    available_kib = meminfo.get("MemAvailable", 0)
    used_kib = max(0, total_kib - available_kib)
    swap_total_kib = meminfo.get("SwapTotal", 0)
    swap_free_kib = meminfo.get("SwapFree", 0)
    swap_used_kib = max(0, swap_total_kib - swap_free_kib)
    vmstat: dict[str, int] = {}
    for line in Path("/proc/vmstat").read_text().splitlines():
        key, _, value = line.partition(" ")
        if key not in {"pswpin", "pswpout"}:
            continue
        try:
            vmstat[key] = int(value.strip())
        except ValueError:
            continue
    return {
        "total_mib": round(total_kib / 1024, 1),
        "available_mib": round(available_kib / 1024, 1),
        "used_mib": round(used_kib / 1024, 1),
        "used_pct": round((used_kib / total_kib * 100.0) if total_kib else 0.0, 2),
        "swap_total_mib": round(swap_total_kib / 1024, 1),
        "swap_free_mib": round(swap_free_kib / 1024, 1),
        "swap_used_mib": round(swap_used_kib / 1024, 1),
        "swap_used_pct": round((swap_used_kib / swap_total_kib * 100.0) if swap_total_kib else 0.0, 2),
        "swap_in_pages_total": vmstat.get("pswpin", 0),
        "swap_out_pages_total": vmstat.get("pswpout", 0),
        "pressure": parse_pressure(Path("/proc/pressure/memory")),
    }


def collect_docker() -> list[dict[str, str]]:
    try:
        proc = run(["docker", "ps", "--format", "{{.Names}}\t{{.Status}}\t{{.Image}}"], timeout=3.0)
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    containers = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        while len(parts) < 3:
            parts.append("")
        containers.append({"name": parts[0], "status": parts[1], "image": parts[2]})
    return containers


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ifaces", default="")
    args = parser.parse_args()
    ifaces = [item for item in args.ifaces.split(",") if item]

    disk = shutil.disk_usage("/")
    payload = {
        "ts": time.time(),
        "hostname": socket.gethostname(),
        "cpu": collect_cpu(),
        "thermals": collect_thermals(),
        "loadavg": os.getloadavg(),
        "memory": collect_memory(),
        "disk_root": {
            "total_gib": round(disk.total / 1024**3, 2),
            "used_gib": round(disk.used / 1024**3, 2),
            "free_gib": round(disk.free / 1024**3, 2),
            "used_pct": round(disk.used / disk.total * 100.0, 2) if disk.total else 0.0,
        },
        "gpu": collect_gpu(),
        "interfaces": collect_interfaces(ifaces) + collect_infiniband(),
        "containers": collect_docker(),
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
