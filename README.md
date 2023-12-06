# lambda-rightsize

Suggest a memory size for each AWS Lambda function from the usage it already logged, with an estimate of what the change does to the bill.

**Status:** working, offline only. It reads exported `REPORT` log lines and CloudWatch Logs Insights CSV files, suggests a memory size per function, estimates the cost difference, flags functions near their memory limit or timing out, and ranks the list. It does not call AWS and does not change anything. Querying CloudWatch directly is not built yet.

The dollar figures are estimates under stated assumptions, not exact savings. Read [How the numbers are produced](#how-the-numbers-are-produced) before quoting them.

## Install

```bash
pip install git+https://github.com/narayana006/lambda-rightsize
```

Python 3.10 or newer, no dependencies.

## Quick start

The repository has sample exports in `examples/` (synthetic data shaped like real exports, with the placeholder account ID 123456789012). This is the real output of the tool on the aggregated sample:

```bash
lambda-rightsize report examples/insights-aggregated.csv --days 14
```

```text
function         invocations  memory MB  max used MB  used  suggested MB  est. cost now  est. cost suggested  est. saving  flags
---------------  -----------  ---------  -----------  ----  ------------  -------------  -------------------  -----------  -------------------------------
image-resizer      2,104,332       1024          212   21%           256         $32.36                $8.77       $23.59
orders-api         6,412,908        512          148   29%           192          $7.12                $4.39        $2.73
auth-token        11,038,551        128           74   58%          keep          $5.03                $5.03            -
thumbnail-cache      903,417        256          181   71%          keep        $0.5679              $0.5679            -
nightly-export            14       2048         1893   92%          2304        $0.4124              $0.4639     -$0.0515  near-memory-limit, small-sample
pdf-render            48,210       3008         3008  100%          3648         $14.74               $17.88       -$3.13  at-memory-limit

Costs are estimates per 30 days, scaled from a 14-day export.
Suggested decreases: up to $26.33 less. Suggested increases: up to $3.18 more.

Prices: $0.0000166667 per GB-second, $0.20 per 1M requests (x86_64 us-east-1 on-demand list price, first tier, no free tier).
Assumptions: 20% headroom over the highest Max Memory Used, rounded up to 64 MB; duration is assumed not to change with memory, so the cost changes shown are upper bounds (see --cpu-bound-fraction).
Nothing was changed. These are estimates from a sample, not exact savings; test a new memory size before relying on it.
```

A negative saving means the suggestion costs more: those functions are at or near their memory limit and the suggestion is to give them room.

## Getting the input

The tool makes no AWS calls and needs no IAM permissions. You export the data yourself, in any of these forms. Files ending in `.csv` are read as Logs Insights exports; anything else is read as a log file.

**1. Logs Insights, aggregated per function (best for busy functions).** Select the functions' log groups, run this query over the period you care about and export the results as CSV:

```
filter @type = "REPORT"
| stats count(*) as invocations, sum(@billedDuration) as billedMs,
        max(@memorySize) as memorySize, max(@maxMemoryUsed) as maxMemoryUsed,
        max(@duration) as maxDuration by @log
```

The column names matter: `invocations`, `billedMs`, `memorySize`, `maxMemoryUsed`, and optionally `maxDuration`. The function name is taken from `@log` (`123456789012:/aws/lambda/orders-api` becomes `orders-api`).

**2. Logs Insights, one row per invocation.**

```
fields @log, @duration, @billedDuration, @memorySize, @maxMemoryUsed
| filter @type = "REPORT"
```

Logs Insights limits how many rows a query returns, so for high-volume functions this is a sample; use the aggregated query instead. A CSV with only an `@message` column containing the `REPORT` lines is accepted too.

**3. A plain log file with `REPORT` lines**, one file per function, for example saved output of `aws logs tail` or `aws logs filter-log-events`. The function name is the file name without its extension, or give it explicitly: `orders-api=some/file.txt`. With this form the tool also sees `Task timed out after N seconds` lines and `Status: timeout`.

Logs Insights reports `@memorySize` and `@maxMemoryUsed` in bytes with 1 MB = 1,000,000 bytes; the tool converts them, so the numbers match the MB values in `REPORT` lines.

You can pass several files of different kinds in one run:

```bash
lambda-rightsize report examples/webhook-handler.log examples/insights-rows.csv --cpu-bound-fraction 0.5 --arch arm64
```

```text
function         invocations  memory MB  max used MB  used  suggested MB  est. cost now  est. cost suggested  est. saving  flags
---------------  -----------  ---------  -----------  ----  ------------  -------------  -------------------  -----------  ------------------------------------------
email-sender              20       1024          122   12%           192      $0.000067            $0.000041    $0.000025  small-sample
webhook-handler           30        512           98   19%          keep      $0.000331            $0.000331            -  timed-out x2, kept: timeouts, small-sample
queue-worker              20        256          234   91%           320      $0.000010            $0.000011   -$0.000001  near-memory-limit, small-sample

Costs are estimates for the period the input covers (pass --days N to scale to 30 days).
Suggested decreases: about $0.000025 less. Suggested increases: about $0.000001 more.

Prices: $0.0000133334 per GB-second, $0.20 per 1M requests (arm64 us-east-1 on-demand list price, first tier, no free tier).
Assumptions: 20% headroom over the highest Max Memory Used, rounded up to 64 MB; 50% of billed time is assumed to scale inversely with memory.
Nothing was changed. These are estimates from a sample, not exact savings; test a new memory size before relying on it.
```

## Options

```text
lambda-rightsize report [FUNCTION=]FILE [[FUNCTION=]FILE ...] [options]
```

| Option | Default | |
| --- | --- | --- |
| `--days N` | not set | Number of days the export covers. Costs are multiplied by 30/N. Without it, costs are for whatever period the input covers. |
| `--headroom PERCENT` | 20 | Free memory to keep above the highest `Max Memory Used`. |
| `--step MB` | 64 | Suggestions are rounded up to a multiple of this, within Lambda's 128 to 10240 MB range. |
| `--arch x86_64\|arm64` | x86_64 | Picks the default GB-second price. |
| `--price-gb-second USD` | 0.0000166667 (x86_64), 0.0000133334 (arm64) | Price per GB-second. |
| `--price-per-million-requests USD` | 0.20 | Price per one million requests. |
| `--cpu-bound-fraction 0..1` | 0 | Share of billed time assumed to scale inversely with memory. See below. |
| `--timeout SECONDS` | not set | Your functions' configured timeout, used for the `near-timeout` flag. Applies to every function in the run. |
| `--min-invocations N` | 100 | Functions with fewer invocations get the `small-sample` flag. |
| `--format table\|csv\|json` | table | |

### Pricing defaults

The default prices are the public on-demand list prices for US East (N. Virginia), first pricing tier, as published on the [AWS Lambda pricing page](https://aws.amazon.com/lambda/pricing/) when this was written. They live in [`lambda_rightsize/pricing.py`](lambda_rightsize/pricing.py). Prices differ by region and change over time, so check the pricing page for your region and pass `--price-gb-second` and `--price-per-million-requests` if they differ. Every run prints the prices it used.

Not modelled: the free tier, volume pricing tiers, Savings Plans, provisioned concurrency, ephemeral storage and data transfer.

## How the numbers are produced

- **Suggested size** = highest `Max Memory Used` in the input, plus headroom, rounded up to the step. If that equals the current size the function shows `keep`.
- **Cost now** = billed duration times configured memory (1 GB = 1024 MB) times the GB-second price, plus invocations times the request price.
- **Cost at the suggested size** needs an assumption about duration, because the input cannot tell how fast the function would run with different memory. Lambda allocates CPU in proportion to memory, so a function doing CPU work gets slower when memory goes down.
  - With the default `--cpu-bound-fraction 0`, duration is assumed not to change. For a decrease this is the best case, so the saving shown is an **upper bound**. For an increase it is the worst case for cost.
  - With `--cpu-bound-fraction 1`, all billed time is assumed to scale with `current memory / new memory`; GB-seconds then stay the same and the estimated saving is zero.
  - Values in between mix the two. Running the report at 0 and at 1 gives you the range; the real figure has to be measured.
- **Scaling to 30 days** (`--days`) is a straight multiplication. It assumes the exported period is typical.

### Flags

| Flag | Meaning |
| --- | --- |
| `at-memory-limit` | An invocation used all of the configured memory. The function may be getting killed for running out of memory; the suggestion is an increase. |
| `near-memory-limit` | Highest usage is at least 90% of the configured memory. |
| `timed-out xN` | N invocations timed out (log file input only). |
| `near-timeout` | Longest duration is at least 90% of the timeout (needs `--timeout`, or a timeout seen in a log file). |
| `kept: timeouts` | A decrease was possible on memory alone but was not suggested, because the function is timing out or close to it and less memory means less CPU. |
| `memory-changed-in-sample` | The input has more than one configured memory size for this function. The most common one is treated as current. |
| `small-sample` | Fewer invocations than `--min-invocations`. |

## Features

- [x] Reads duration, billed duration and max memory used from exported `REPORT` lines and Logs Insights CSV (per invocation or aggregated)
- [x] Estimates cost at the current and the suggested memory size, with configurable prices
- [x] Flags functions close to running out of memory or timing out
- [x] Ranked list by estimated saving; table, CSV or JSON
- [x] Read-only: prints the suggestion, changes nothing
- [ ] Queries CloudWatch Logs directly (`report --days 14` with no files)
- [ ] Cost estimate at every memory size, not only the suggested one
- [ ] Per-function timeout and architecture read from the Lambda API

## Limitations

- The estimated saving is not a measurement. It depends on the duration assumption above, and on the sample being representative.
- `Max Memory Used` at a lower memory size can differ from what was observed, for example for runtimes that size their heap from the available memory. Leave headroom and test.
- The highest observed usage is used, so one unusual invocation sets the suggestion. That is deliberate (running out of memory fails the invocation) but it can hide a possible decrease.
- Cold start time usually changes with memory size; that is not modelled.
- One architecture and one price per run. Run it separately for arm64 and x86_64 functions.
- The Logs Insights queries above were written from the documented field names; the tool is tested on files in the export format, not against a live CloudWatch account.
- The sample files are synthetic.

## Development

```bash
git clone https://github.com/narayana006/lambda-rightsize
cd lambda-rightsize
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
```

## Contributing

Issues and pull requests are welcome. If an export of yours is not read correctly, an issue with the header row and one or two (anonymised) data rows is the fastest way to get it fixed.

Built and maintained by [Narayana Vemburaj](https://github.com/narayana006).

## License

[MIT](LICENSE)
