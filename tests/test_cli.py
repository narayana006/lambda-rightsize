import csv
import io
import json
from pathlib import Path

from lambda_rightsize.cli import main, money

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    code = main(argv, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def test_money():
    assert money(1234.5) == "$1,234.50"
    assert money(0.01234) == "$0.0123"
    assert money(0.0000671) == "$0.000067"
    assert money(-2.5) == "-$2.50"
    assert money(0) == "$0.00"


def test_aggregated_example_json():
    code, out, _ = run(["report", str(EXAMPLES / "insights-aggregated.csv"), "--days", "14", "--format", "json"])
    assert code == 0
    doc = json.loads(out)
    assert doc["price_per_gb_second"] == 0.0000166667
    assert doc["price_per_million_requests"] == 0.2
    by_name = {f["function"]: f for f in doc["functions"]}
    assert by_name["image-resizer"]["recommended_mb"] == 256
    assert by_name["image-resizer"]["action"] == "decrease"
    assert by_name["auth-token"]["action"] == "keep"
    assert by_name["pdf-render"]["flags"] == ["at-memory-limit"]
    assert by_name["pdf-render"]["estimated_saving_usd"] < 0
    assert by_name["nightly-export"]["flags"] == ["near-memory-limit", "small-sample"]
    assert doc["functions"][0]["function"] == "image-resizer"   # ranked by saving
    assert doc["functions"][-1]["function"] == "pdf-render"
    # scaled from 14 to 30 days
    resizer = by_name["image-resizer"]
    expected = (int(2104332 * 418.6) / 1000 * 0.0000166667 + 2104332 * 0.2 / 1e6) * 30 / 14
    assert abs(resizer["estimated_cost_now_usd"] - expected) < 0.01


def test_table_states_prices_and_that_savings_are_estimates():
    code, out, _ = run(["report", str(EXAMPLES / "insights-aggregated.csv"), "--days", "14"])
    assert code == 0
    assert "$0.0000166667 per GB-second, $0.20 per 1M requests (x86_64 us-east-1 on-demand list price" in out
    assert "per 30 days, scaled from a 14-day export" in out
    assert "not exact savings" in out
    assert "Suggested decreases: up to" in out


def test_price_overrides_and_cpu_fraction_change_the_output():
    base = ["report", str(EXAMPLES / "insights-aggregated.csv"), "--format", "json"]
    default = json.loads(run(base)[1])
    doubled = json.loads(run(base + ["--price-gb-second", "0.0000333334", "--price-per-million-requests", "0"])[1])
    cpu = json.loads(run(base + ["--cpu-bound-fraction", "1"])[1])
    first = lambda doc: next(f for f in doc["functions"] if f["function"] == "image-resizer")
    assert abs(first(doubled)["estimated_saving_usd"] - 2 * first(default)["estimated_saving_usd"]) < 0.01
    assert doubled["price_source"] == "set on the command line"
    assert abs(first(cpu)["estimated_saving_usd"]) < 1e-6
    assert "about" in run(base[:2] + ["--cpu-bound-fraction", "0.5"])[1]


def test_log_file_and_row_csv_together_as_csv_output():
    code, out, _ = run(["report", f"webhook-handler={EXAMPLES / 'webhook-handler.log'}",
                        str(EXAMPLES / "insights-rows.csv"), "--format", "csv", "--min-invocations", "10"])
    assert code == 0
    rows = {row["function"]: row for row in csv.DictReader(io.StringIO(out))}
    assert set(rows) == {"webhook-handler", "email-sender", "queue-worker"}
    assert rows["webhook-handler"]["flags"] == "timed-out x2; kept: timeouts"
    assert rows["webhook-handler"]["action"] == "keep"
    assert rows["email-sender"]["action"] == "decrease"
    assert rows["queue-worker"]["action"] == "increase"


def test_file_name_is_the_default_function_name():
    code, out, _ = run(["report", str(EXAMPLES / "webhook-handler.log")])
    assert code == 0
    assert out.splitlines()[2].startswith("webhook-handler")


def test_bad_arguments_and_inputs(tmp_path):
    assert run(["report", "x.log", "--days", "0"])[0] == 2
    assert run(["report", "x.log", "--cpu-bound-fraction", "2"])[0] == 2
    code, _, err = run(["report", str(tmp_path / "missing.log")])
    assert code == 2 and "cannot read" in err
    empty = tmp_path / "empty.log"
    empty.write_text("nothing here\n")
    code, _, err = run(["report", str(empty)])
    assert code == 1 and "no usage data" in err
    bad = tmp_path / "bad.csv"
    bad.write_text("a,b\n1,2\n")
    code, _, err = run(["report", str(bad)])
    assert code == 2 and "unrecognised CSV columns" in err
