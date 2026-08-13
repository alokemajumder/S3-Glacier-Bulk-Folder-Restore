"""Logging configuration.

The console stays terse and human-readable; an optional log file gets full
timestamps, because a run that takes eight hours needs to be reconstructable
afterwards.
"""

from __future__ import annotations

import logging
import os
import sys

CONSOLE_FORMAT = "%(message)s"
CONSOLE_FORMAT_VERBOSE = "%(asctime)s %(levelname)-7s %(message)s"
FILE_FORMAT = "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"

_LEVEL_COLORS = {
    "WARNING": "\033[33m",
    "ERROR": "\033[31m",
    "CRITICAL": "\033[1;31m",
}
_RESET = "\033[0m"


class _ColorFormatter(logging.Formatter):
    """Colours warnings and errors when stderr is a terminal."""

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        color = _LEVEL_COLORS.get(record.levelname)
        return f"{color}{message}{_RESET}" if color else message


def _color_enabled(stream) -> bool:
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


def configure(verbose: int = 0, quiet: bool = False, log_file: str | None = None) -> None:
    """Install handlers on the package logger.

    ``verbose=0`` shows INFO, ``-v`` adds DEBUG for this package, ``-vv`` adds
    DEBUG for botocore too. ``--quiet`` drops to warnings and errors only.
    """
    root = logging.getLogger("s3_glacier_restore")
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    if quiet:
        console_level = logging.WARNING
    elif verbose:
        console_level = logging.DEBUG
    else:
        console_level = logging.INFO

    root.setLevel(logging.DEBUG)
    root.propagate = False

    stream = sys.stderr
    console = logging.StreamHandler(stream)
    console.setLevel(console_level)
    fmt = CONSOLE_FORMAT_VERBOSE if verbose else CONSOLE_FORMAT
    formatter_cls = _ColorFormatter if _color_enabled(stream) else logging.Formatter
    console.setFormatter(formatter_cls(fmt))
    root.addHandler(console)

    if log_file:
        path = os.path.expanduser(log_file)
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(FILE_FORMAT))
        root.addHandler(file_handler)

    # botocore's DEBUG logging prints every request and signature; only worth
    # it at -vv, and never worth it at -v.
    if verbose >= 2:
        logging.getLogger("botocore").setLevel(logging.DEBUG)
        logging.getLogger("botocore").addHandler(console)
    else:
        logging.getLogger("botocore").setLevel(logging.WARNING)
        logging.getLogger("urllib3").setLevel(logging.WARNING)


def emit(message: str = "") -> None:
    """Write operator-facing UI text to stdout, bypassing the log stream.

    Prompts, banners and the summary belong on stdout so they can be piped;
    progress logging belongs on stderr so it does not corrupt that pipe.
    """
    print(message, file=sys.stdout, flush=True)
