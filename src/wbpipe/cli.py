"""Command-line entry point. `wbpipe run` executes every step in order."""

from __future__ import annotations

import argparse
import logging
from collections.abc import Callable

from wbpipe.config import Settings, load_settings
from wbpipe.ingest import run_ingest

log = logging.getLogger("wbpipe")


def _ingest(settings: Settings) -> None:
    run_ingest(settings)


# Steps run in this order by `wbpipe run`; None marks a step that is not built yet.
STEPS: dict[str, Callable[[Settings], None] | None] = {
    "ingest": _ingest,
    "transform": None,
    "check": None,
    "report": None,
    "dashboard": None,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wbpipe", description=__doc__)
    parser.add_argument("--config", default="config/pipeline.yaml", help="pipeline config file")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("step", choices=("run", *STEPS), help="step to execute ('run' = all)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = load_settings(args.config)
    for name in STEPS if args.step == "run" else [args.step]:
        step = STEPS[name]
        if step is None:
            log.warning("step %r is not implemented yet; skipping", name)
            continue
        log.info("== %s ==", name)
        step(settings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
