"""Central logging setup: richer file log, stdlib-logging capture, and crash/exception hooks."""

import faulthandler
import glob
import logging
import os
import sys
import threading

from loguru import logger

LOG_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {thread.name} | "
    "{name}:{function}:{line} - {message}"
)

# Held open for the life of the process so faulthandler can write native-crash tracebacks.
_fault_file = None


class _InterceptHandler(logging.Handler):
    """Routes stdlib `logging` records (wizwalker, libraries) into loguru."""

    def emit(self, record: logging.LogRecord):
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def _prune_old_logs(log_dir: str, prefix: str, keep: int):
    files = sorted(glob.glob(os.path.join(log_dir, f"{prefix}*.log")), key=os.path.getmtime, reverse=True)
    for stale in files[keep:]:
        try:
            os.remove(stale)
        except OSError:
            pass


def setup_logging(log_dir: str, file_stem: str, prefix: str, keep: int = 25) -> int:
    """Add the file sink, capture stdlib logging, install exception hooks. Returns the sink id."""
    global _fault_file
    os.makedirs(log_dir, exist_ok=True)
    _prune_old_logs(log_dir, prefix, keep)

    sink_id = logger.add(
        os.path.join(log_dir, f"{file_stem}.log"),
        level="DEBUG",
        format=LOG_FORMAT,
        encoding="utf-8",
        enqueue=True,
        backtrace=True,
        # Variable values in tracebacks could include account details, so keep this off.
        diagnose=False,
    )

    logging.basicConfig(handlers=[_InterceptHandler()], level=logging.DEBUG, force=True)
    for noisy in ("asyncio", "urllib3", "PIL", "charset_normalizer", "chardet"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("pymem").setLevel(logging.FATAL)

    def _sys_hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        logger.opt(exception=(exc_type, exc, tb)).critical("Uncaught exception")

    def _thread_hook(args):
        name = args.thread.name if args.thread else "?"
        logger.opt(exception=(args.exc_type, args.exc_value, args.exc_traceback)).critical(
            f"Uncaught exception in thread {name}"
        )

    sys.excepthook = _sys_hook
    threading.excepthook = _thread_hook

    try:
        _fault_file = open(os.path.join(log_dir, f"{file_stem}-faults.txt"), "a", encoding="utf-8")
        faulthandler.enable(file=_fault_file, all_threads=True)
    except Exception as e:
        logger.debug(f"faulthandler unavailable: {e!r}")

    return sink_id


def install_loop_exception_handler(loop):
    """Log task exceptions nobody awaited (otherwise they vanish)."""

    def _handler(_loop, context):
        exc = context.get("exception")
        message = context.get("message", "")
        if exc is not None:
            logger.opt(exception=exc).error(f"Unhandled asyncio exception: {message}")
        else:
            logger.error(f"asyncio: {message}")

    loop.set_exception_handler(_handler)


def log_startup_banner(version: str, **extra):
    details = " ".join(f"{k}={v}" for k, v in extra.items())
    logger.info(
        f"Startup: version={version} python={sys.version.split()[0]} platform={sys.platform} "
        f"frozen={getattr(sys, 'frozen', False)} pid={os.getpid()} cwd={os.getcwd()} {details}".rstrip()
    )
