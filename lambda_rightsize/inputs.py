"""Read Lambda usage from exported files and reduce it to one record per function.

Three input shapes are supported:

* a text log containing REPORT lines (function name comes from the caller);
* a CloudWatch Logs Insights CSV with one row per invocation
  (``@log, @duration, @billedDuration, @memorySize, @maxMemoryUsed``), or with
  an ``@message`` column holding the REPORT line;
* a Logs Insights CSV already aggregated per function with the aliases
  ``invocations, billedMs, memorySize, maxMemoryUsed, maxDuration``.

In Logs Insights, ``@memorySize`` and ``@maxMemoryUsed`` are in bytes, where
1 MB is 1,000,000 bytes. They are converted to MB here.
"""

from __future__ import annotations

import csv
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

_REQUEST_ID = re.compile(r"REPORT RequestId:\s*(\S+)")
_NUMBER = r"([0-9]+(?:\.[0-9]+)?)"
_DURATION = re.compile(r"(?<!Billed )(?<!Init )(?<!Restore )Duration:\s*" + _NUMBER + r"\s*ms")
_BILLED = re.compile(r"Billed Duration:\s*" + _NUMBER + r"\s*ms")
_MEMORY_SIZE = re.compile(r"Memory Size:\s*" + _NUMBER + r"\s*MB")
_MAX_MEMORY = re.compile(r"Max Memory Used:\s*" + _NUMBER + r"\s*MB")
_STATUS_TIMEOUT = re.compile(r"Status:\s*timeout")
_TIMED_OUT = re.compile(r"Task timed out after " + _NUMBER + r" seconds")
BYTES_PER_MB = 1_000_000


class InputError(Exception):
    pass


@dataclass
class FunctionUsage:
    """Usage of one function over the exported period."""

    function: str
    invocations: int = 0
    billed_ms: float = 0.0
    max_duration_ms: float = 0.0
    max_memory_used_mb: float = 0.0
    memory_sizes: Counter = field(default_factory=Counter)  # MB -> invocations
    billed_mb_ms: float = 0.0   # sum of billed ms x configured MB, for the current cost
    timeouts: int = 0
    timeout_seconds: Optional[float] = None  # from "Task timed out after N seconds"

    @property
    def memory_mb(self) -> int:
        """The configured size; if it changed during the period, the most common one."""
        return self.memory_sizes.most_common(1)[0][0]

    def add(self, duration_ms: float, billed_ms: float, memory_mb: float, used_mb: float,
            count: int = 1, timed_out: bool = False) -> None:
        self.invocations += count
        self.billed_ms += billed_ms
        self.billed_mb_ms += billed_ms * memory_mb
        self.max_duration_ms = max(self.max_duration_ms, duration_ms)
        self.max_memory_used_mb = max(self.max_memory_used_mb, used_mb)
        self.memory_sizes[int(round(memory_mb))] += count
        self.timeouts += 1 if timed_out else 0


def parse_report_line(line: str) -> Optional[tuple[float, float, float, float, bool]]:
    """(duration ms, billed ms, memory MB, max used MB, timed out) or None."""
    request = _REQUEST_ID.search(line)
    if not request:
        return None
    rest = line[request.end():]
    found = [pattern.search(rest) for pattern in (_DURATION, _BILLED, _MEMORY_SIZE, _MAX_MEMORY)]
    if not all(found):
        return None
    duration, billed, size, used = (float(match.group(1)) for match in found)
    return duration, billed, size, used, bool(_STATUS_TIMEOUT.search(rest))


def function_from_log_group(value: str) -> str:
    """'123456789012:/aws/lambda/orders-api' -> 'orders-api'."""
    value = value.strip()
    marker = "/aws/lambda/"
    if marker in value:
        return value.split(marker, 1)[1]
    return value.rsplit(":", 1)[-1].rsplit("/", 1)[-1] or value


def _usage(table: dict[str, FunctionUsage], function: str) -> FunctionUsage:
    if function not in table:
        table[function] = FunctionUsage(function)
    return table[function]


def read_report_log(lines: Iterable[str], function: str, table: dict[str, FunctionUsage]) -> int:
    """Add the REPORT lines of one function's log. Returns the number of lines used."""
    used = 0
    explicit_timeouts = 0
    usage: Optional[FunctionUsage] = None
    for line in lines:
        timed_out = _TIMED_OUT.search(line)
        if timed_out:
            usage = _usage(table, function)
            explicit_timeouts += 1
            usage.timeout_seconds = float(timed_out.group(1))
            continue
        parsed = parse_report_line(line)
        if parsed is None:
            continue
        usage = _usage(table, function)
        duration, billed, size, max_used, status_timeout = parsed
        usage.add(duration, billed, size, max_used, timed_out=status_timeout)
        used += 1
    if usage is not None and explicit_timeouts > usage.timeouts:
        # older REPORT lines have no "Status: timeout"; trust the "Task timed out" lines
        usage.timeouts = explicit_timeouts
    return used


def _column(row: dict[str, str], *names: str) -> Optional[str]:
    for name in names:
        for key in (name, "@" + name):
            value = row.get(key)
            if value not in (None, ""):
                return value
    return None


def _number(row: dict[str, str], line: int, *names: str) -> float:
    value = _column(row, *names)
    if value is None:
        raise InputError(f"row {line}: missing column {names[0]}")
    try:
        return float(value)
    except ValueError:
        raise InputError(f"row {line}: {names[0]} is not a number: {value!r}") from None


def read_insights_csv(lines: Iterable[str], default_function: str, table: dict[str, FunctionUsage]) -> int:
    """Add rows of a Logs Insights CSV export. Returns the number of rows used."""
    reader = csv.DictReader(lines)
    if not reader.fieldnames:
        return 0
    fields = {name.lstrip("@") for name in reader.fieldnames if name}
    aggregated = "invocations" in fields
    per_row = {"billedDuration", "memorySize", "maxMemoryUsed"} <= fields
    if not (aggregated or per_row or "message" in fields):
        raise InputError(
            "unrecognised CSV columns: " + ", ".join(reader.fieldnames) + ". Expected @billedDuration, "
            "@memorySize and @maxMemoryUsed (one row per invocation), an @message column with REPORT "
            "lines, or the aggregated columns invocations, billedMs, memorySize, maxMemoryUsed.")
    used = 0
    for line, row in enumerate(reader, start=2):
        log = _column(row, "log", "function")
        function = function_from_log_group(log) if log else default_function
        if aggregated:
            count = int(_number(row, line, "invocations"))
            if count <= 0:
                continue
            billed = _number(row, line, "billedMs")
            size = _number(row, line, "memorySize") / BYTES_PER_MB
            max_used = _number(row, line, "maxMemoryUsed") / BYTES_PER_MB
            duration = float(_column(row, "maxDuration") or 0)
            _usage(table, function).add(duration, billed, size, max_used, count=count)
        elif per_row:
            billed = _number(row, line, "billedDuration")
            duration = float(_column(row, "duration") or billed)
            size = _number(row, line, "memorySize") / BYTES_PER_MB
            max_used = _number(row, line, "maxMemoryUsed") / BYTES_PER_MB
            _usage(table, function).add(duration, billed, size, max_used)
        else:
            parsed = parse_report_line(row.get("@message") or row.get("message") or "")
            if parsed is None:
                continue
            duration, billed, size, max_used, timed_out = parsed
            _usage(table, function).add(duration, billed, size, max_used, timed_out=timed_out)
        used += 1
    return used


def read_file(path: Path, label: str, table: dict[str, FunctionUsage]) -> int:
    with open(path, encoding="utf-8-sig", errors="replace", newline="") as handle:
        if path.suffix.lower() == ".csv":
            return read_insights_csv(handle, label, table)
        return read_report_log(handle, label, table)
