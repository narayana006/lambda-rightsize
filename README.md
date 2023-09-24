# lambda-rightsize

Recommend the cheapest memory setting for each AWS Lambda function from its real CloudWatch usage.

> **Project status: planning. There is no working code in this repository yet.**
> This README describes what the project will do. Nothing below can be installed or run today.
> Features are ticked off in the checklist as they are built.

## What it is

A command-line tool.

**The problem it solves:** Most Lambda functions run with whatever memory was set on day one, which is usually too much or too little.

## Who it is for

Teams with more than a few Lambda functions.

## How it will work

1. You run a report for an account and region.
2. The tool reads each function's recent usage.
3. It lists a recommended memory size and the estimated saving per function.

## Planned features

Unticked items are not built yet.

- [ ] Reads duration and max memory used from CloudWatch
- [ ] Estimates cost at each memory size
- [ ] Flags functions close to running out of memory or timing out
- [ ] Ranked list of savings
- [ ] Read-only: prints the change, does not apply it

## Planned usage

This is the intended interface, shown so you can judge the design. **It does not work yet**, and names may change before the first release.

```bash
lambda-rightsize report --days 14
```

## Contributing

Feedback on the design is the most useful contribution right now. Open an issue to suggest a feature, question an approach or describe your use case. Code contributions are welcome once the first version lands.

## Author

Built and maintained by [Narayana Vemburaj](https://github.com/narayana006).

## License

[MIT](LICENSE)
