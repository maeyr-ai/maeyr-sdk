"""Canonical parsers for platform CPU and memory resource quantities."""

from __future__ import annotations


def parse_cpu_to_millicores(cpu: str) -> int:
    """Convert CPU text such as ``500m`` or ``1`` to millicores."""
    if not cpu:
        return 0
    normalized = cpu.strip().lower()
    if normalized.endswith("m"):
        return int(normalized[:-1])
    return int(float(normalized) * 1000)


def parse_memory_to_mb(memory: str) -> int:
    """Convert memory text such as ``512Mi`` or ``1Gi`` to mebibytes."""
    if not memory:
        return 0
    normalized = memory.strip()
    if normalized.endswith("Gi"):
        return int(float(normalized[:-2]) * 1024)
    if normalized.endswith("Mi"):
        return int(normalized[:-2])
    if normalized.endswith("G"):
        return int(float(normalized[:-1]) * 1024)
    if normalized.endswith("M"):
        return int(normalized[:-1])
    return int(normalized)


__all__ = ["parse_cpu_to_millicores", "parse_memory_to_mb"]
