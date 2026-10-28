"""Command-line entry point. `wbpipe run` executes every step in order."""

from __future__ import annotations

import argparse

STEPS = ("ingest", "transform", "check", "report", "dashboard")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wbpipe", description=__doc__)
    parser.add_argument("--config", default="config/pipeline.yaml", help="pipeline config file")
    parser.add_argument("step", choices=("run", *STEPS), help="step to execute ('run' = all)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    raise NotImplementedError(f"step {args.step!r} is not implemented yet")


if __name__ == "__main__":
    raise SystemExit(main())
