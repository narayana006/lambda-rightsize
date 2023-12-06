import io

import pytest

from lambda_rightsize.inputs import (InputError, function_from_log_group, parse_report_line,
                                     read_insights_csv, read_report_log)

REPORT = ("REPORT RequestId: abc\tDuration: 101.50 ms\tBilled Duration: 102 ms\t"
          "Memory Size: 512 MB\tMax Memory Used: 140 MB\tInit Duration: 300.00 ms\t")


def test_parse_report_line():
    assert parse_report_line(REPORT) == (101.5, 102.0, 512.0, 140.0, False)
    assert parse_report_line("END RequestId: abc") is None
    assert parse_report_line("REPORT RequestId: abc\tDuration: 1 ms") is None
    assert parse_report_line(REPORT + "Status: timeout")[4] is True


def test_function_from_log_group():
    assert function_from_log_group("123456789012:/aws/lambda/orders-api") == "orders-api"
    assert function_from_log_group("/aws/lambda/orders-api") == "orders-api"
    assert function_from_log_group("123456789012:custom-group") == "custom-group"


def test_report_log_is_reduced_to_one_usage():
    table = {}
    lines = ["START RequestId: abc", REPORT,
             REPORT.replace("101.50", "250.00").replace("102 ms", "250 ms").replace("140 MB", "166 MB")]
    assert read_report_log(lines, "fn", table) == 2
    usage = table["fn"]
    assert usage.invocations == 2
    assert usage.billed_ms == 352
    assert usage.max_duration_ms == 250
    assert usage.max_memory_used_mb == 166
    assert usage.memory_mb == 512
    assert usage.billed_mb_ms == 352 * 512
    assert usage.timeouts == 0 and usage.timeout_seconds is None


def test_timeouts_are_counted_once_per_invocation():
    table = {}
    timeout_report = ("REPORT RequestId: t\tDuration: 3000.00 ms\tBilled Duration: 3000 ms\t"
                      "Memory Size: 512 MB\tMax Memory Used: 90 MB\t")
    lines = [
        "2026-01-05T10:00:03.004Z t Task timed out after 3.00 seconds", timeout_report + "Status: timeout",
        "2026-01-05T10:01:03.004Z t Task timed out after 3.00 seconds", timeout_report,  # older format
        REPORT,
    ]
    read_report_log(lines, "fn", table)
    assert table["fn"].timeouts == 2
    assert table["fn"].timeout_seconds == 3.0
    assert table["fn"].invocations == 3


def test_memory_size_change_keeps_the_most_common_size():
    table = {}
    read_report_log([REPORT, REPORT, REPORT.replace("512 MB", "1024 MB")], "fn", table)
    assert table["fn"].memory_mb == 512
    assert len(table["fn"].memory_sizes) == 2
    assert table["fn"].billed_mb_ms == 102 * 512 * 2 + 102 * 1024


def test_insights_rows_convert_bytes_to_mb():
    text = ("@log,@duration,@billedDuration,@memorySize,@maxMemoryUsed,@initDuration\n"
            "123456789012:/aws/lambda/a,10.5,11,256000000,120000000,\n"
            "123456789012:/aws/lambda/a,20.5,21,256000000,131000000,301.2\n"
            "123456789012:/aws/lambda/b,5.0,5,128000000,60000000,\n")
    table = {}
    assert read_insights_csv(io.StringIO(text), "fallback", table) == 3
    assert set(table) == {"a", "b"}
    assert table["a"].memory_mb == 256
    assert table["a"].max_memory_used_mb == 131
    assert table["a"].billed_ms == 32
    assert table["a"].max_duration_ms == 20.5


def test_insights_aggregated_rows():
    text = ("@log,invocations,billedMs,memorySize,maxMemoryUsed,maxDuration\n"
            "123456789012:/aws/lambda/a,1000,50000,1024000000,212000000,912.5\n"
            "123456789012:/aws/lambda/idle,0,0,128000000,0,0\n")
    table = {}
    assert read_insights_csv(io.StringIO(text), "fallback", table) == 1
    usage = table["a"]
    assert (usage.invocations, usage.billed_ms, usage.memory_mb) == (1000, 50000, 1024)
    assert usage.max_memory_used_mb == 212
    assert usage.billed_mb_ms == 50000 * 1024
    assert usage.max_duration_ms == 912.5


def test_insights_message_column_and_fallback_name():
    text = '@timestamp,@message\n2026-01-05 10:00:00.000,"' + REPORT + '"\n2026-01-05 10:00:01.000,"END RequestId: abc"\n'
    table = {}
    assert read_insights_csv(io.StringIO(text), "from-file-name", table) == 1
    assert table["from-file-name"].max_memory_used_mb == 140


def test_csv_errors_are_explained():
    with pytest.raises(InputError, match="unrecognised CSV columns"):
        read_insights_csv(io.StringIO("a,b\n1,2\n"), "x", {})
    bad = "@billedDuration,@memorySize,@maxMemoryUsed\n12,abc,1\n"
    with pytest.raises(InputError, match="row 2: memorySize is not a number"):
        read_insights_csv(io.StringIO(bad), "x", {})
    assert read_insights_csv(io.StringIO(""), "x", {}) == 0
