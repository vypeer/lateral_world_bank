"""Command-line entry point. `wbpipe run` executes every step in order."""

from __future__ import annotations

import argparse
import logging
from collections.abc import Callable

from wbpipe.api_client import PagingError, RetriesExhausted, WorldBankApiError
from wbpipe.config import ConfigError, Settings, load_settings
from wbpipe.dashboard import run_dashboard
from wbpipe.ingest import run_ingest
from wbpipe.quality import DataQualityError, run_checks
from wbpipe.report import ReportError, run_report
from wbpipe.transform import TransformError, run_transform

log = logging.getLogger("wbpipe")


def _ingest(settings: Settings, args: argparse.Namespace) -> None:
    run_ingest(settings)


def _transform(settings: Settings, args: argparse.Namespace) -> None:
    run_transform(settings, run_id=args.run_id)


def _check(settings: Settings, args: argparse.Namespace) -> None:
    run_checks(settings)


def _report(settings: Settings, args: argparse.Namespace) -> None:
    run_report(settings)


def _dashboard(settings: Settings, args: argparse.Namespace) -> None:
    run_dashboard(settings)


EXPECTED_ERRORS = (
    ConfigError,
    WorldBankApiError,
    PagingError,
    RetriesExhausted,
    TransformError,
    DataQualityError,
    ReportError,
)

# Steps run in this order by `wbpipe run`.
STEPS: dict[str, Callable[[Settings, argparse.Namespace], None]] = {
    "ingest": _ingest,
    "transform": _transform,
    "check": _check,
    "report": _report,
    "dashboard": _dashboard,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wbpipe", description=__doc__)
    parser.add_argument("--config", default="config/pipeline.yaml", help="pipeline config file")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument(
        "--run-id",
        help="transform only: rebuild from this ingest run instead of the latest (replay)",
    )
    parser.add_argument("step", choices=("run", *STEPS), help="step to execute ('run' = all)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.run_id and args.step != "transform":
        parser.error("--run-id can only be used with the transform step")
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        settings = load_settings(args.config)
        for name in STEPS if args.step == "run" else [args.step]:
            log.info("== %s ==", name)
            STEPS[name](settings, args)
    except EXPECTED_ERRORS as exc:
        # Known failure modes: report clearly and exit non-zero without a traceback.
        log.error("%s: %s", type(exc).__name__, exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
