from __future__ import annotations

import csv
import ctypes
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlparse


def _round_mb(value: int | float | None) -> float | None:
    if value is None:
        return None
    return round(float(value) / (1024.0 * 1024.0), 1)


def _run_text(command: list[str], *, timeout: float = 3.0) -> str:
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or f"exit {completed.returncode}").strip())
    return completed.stdout


def system_memory_snapshot() -> dict:
    """Return host RAM usage without introducing psutil."""
    if sys.platform == "win32":
        class MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatusEx()
        status.dwLength = ctypes.sizeof(status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return {"available": False}
        total = int(status.ullTotalPhys)
        free = int(status.ullAvailPhys)
        used = max(0, total - free)
        return {
            "available": True,
            "total_mb": _round_mb(total),
            "used_mb": _round_mb(used),
            "free_mb": _round_mb(free),
            "used_percent": round((used * 100.0 / total), 1) if total else None,
        }

    meminfo = Path("/proc/meminfo")
    if meminfo.is_file():
        values: dict[str, int] = {}
        for line in meminfo.read_text(encoding="utf-8", errors="replace").splitlines():
            if ":" not in line:
                continue
            key, raw = line.split(":", 1)
            token = raw.strip().split()[0] if raw.strip() else "0"
            try:
                values[key] = int(token) * 1024
            except ValueError:
                continue
        total = values.get("MemTotal")
        free = values.get("MemAvailable")
        if total is not None and free is not None:
            used = max(0, total - free)
            return {
                "available": True,
                "total_mb": _round_mb(total),
                "used_mb": _round_mb(used),
                "free_mb": _round_mb(free),
                "used_percent": round((used * 100.0 / total), 1) if total else None,
            }
    return {"available": False}


def process_rss_bytes(pid: int | None) -> int | None:
    if not pid or pid <= 0:
        return None
    if sys.platform == "win32":
        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong),
                ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        kernel32 = ctypes.windll.kernel32
        psapi = ctypes.windll.psapi
        process_query_information = 0x0400
        process_vm_read = 0x0010
        handle = kernel32.OpenProcess(process_query_information | process_vm_read, False, int(pid))
        if not handle:
            return None
        try:
            counters = ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                return None
            return int(counters.WorkingSetSize)
        finally:
            kernel32.CloseHandle(handle)

    status = Path(f"/proc/{int(pid)}/status")
    if status.is_file():
        for line in status.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("VmRSS:"):
                parts = line.split()
                if len(parts) >= 2:
                    try:
                        return int(parts[1]) * 1024
                    except ValueError:
                        return None
    return None


def pid_for_port(port: int | None) -> int | None:
    """Best-effort listener PID lookup; currently needed mainly for Windows dev use."""
    if not port or port <= 0 or sys.platform != "win32":
        return None
    try:
        output = _run_text(["netstat", "-ano", "-p", "tcp"], timeout=3.0)
    except Exception:
        return None
    suffix = f":{int(port)}"
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP":
            continue
        local = parts[1]
        state = parts[-2].upper()
        if state != "LISTENING" or not local.endswith(suffix):
            continue
        try:
            return int(parts[-1])
        except ValueError:
            continue
    return None


def _base_port(base_url: str) -> int | None:
    try:
        parsed = urlparse(base_url)
        return parsed.port
    except ValueError:
        return None


def nvidia_snapshot() -> dict:
    """Best-effort NVIDIA VRAM snapshot. Per-PID values can be unavailable under WDDM."""
    try:
        gpu_text = _run_text(
            [
                "nvidia-smi",
                "--query-gpu=index,name,memory.total,memory.used,memory.free",
                "--format=csv,noheader,nounits",
            ],
            timeout=4.0,
        )
    except Exception as exc:
        return {"available": False, "error": str(exc), "devices": [], "processes": {}}

    devices = []
    for row in csv.reader(gpu_text.splitlines()):
        if len(row) < 5:
            continue
        try:
            index = int(row[0].strip())
        except ValueError:
            index = len(devices)
        def number(value: str) -> float | None:
            try:
                return round(float(value.strip()), 1)
            except ValueError:
                return None
        devices.append(
            {
                "index": index,
                "name": row[1].strip(),
                "total_mb": number(row[2]),
                "used_mb": number(row[3]),
                "free_mb": number(row[4]),
            }
        )

    processes: dict[str, float | None] = {}
    try:
        process_text = _run_text(
            [
                "nvidia-smi",
                "--query-compute-apps=pid,used_memory",
                "--format=csv,noheader,nounits",
            ],
            timeout=4.0,
        )
        for row in csv.reader(process_text.splitlines()):
            if len(row) < 2:
                continue
            pid = row[0].strip()
            try:
                processes[pid] = round(float(row[1].strip()), 1)
            except ValueError:
                processes[pid] = None
    except Exception:
        # WDDM commonly cannot expose reliable per-process VRAM. System totals
        # remain useful, so keep the GPU snapshot available instead of failing.
        pass

    return {"available": True, "devices": devices, "processes": processes}


def collect_resource_snapshot(
    *,
    character_base_url: str,
    media_base_url: str,
    media_health: dict | None = None,
    character_health: dict | None = None,
) -> dict:
    gpu = nvidia_snapshot()
    gpu_processes = gpu.get("processes", {}) if isinstance(gpu, dict) else {}

    media_pid = None
    if isinstance(media_health, dict):
        try:
            media_pid = int(media_health.get("pid")) if media_health.get("pid") else None
        except (TypeError, ValueError):
            media_pid = None
    if media_pid is None:
        media_pid = pid_for_port(_base_port(media_base_url))

    character_pid = None
    if isinstance(character_health, dict):
        try:
            character_pid = int(character_health.get("pid")) if character_health.get("pid") else None
        except (TypeError, ValueError):
            character_pid = None
    if character_pid is None:
        character_pid = pid_for_port(_base_port(character_base_url))

    def process_item(name: str, pid: int | None, **extra) -> dict:
        rss = process_rss_bytes(pid)
        return {
            "name": name,
            "pid": pid,
            "rss_mb": _round_mb(rss),
            "gpu_vram_mb": gpu_processes.get(str(pid)) if pid else None,
            **extra,
        }

    media_asr = media_health.get("asr", {}) if isinstance(media_health, dict) else {}
    media_tts = media_health.get("tts", {}) if isinstance(media_health, dict) else {}
    character_loaded = character_health.get("runtime_loaded") if isinstance(character_health, dict) else None

    return {
        "ok": True,
        "system_memory": system_memory_snapshot(),
        "gpu": gpu,
        "processes": [
            process_item("Character Runtime", character_pid, runtime_loaded=character_loaded),
            process_item(
                "Media Runtime",
                media_pid,
                asr_loaded=media_asr.get("loaded"),
                tts_loaded=media_tts.get("loaded"),
            ),
            process_item("Dev Console", os.getpid()),
        ],
    }
