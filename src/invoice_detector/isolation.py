"""Run risky work (parsing uploaded files) in a separate, short-lived process.

PDF and image parsers are large C and Python libraries handed bytes chosen by whoever sent
the file. A malformed or hostile file can make them spin for minutes or eat memory. Doing
the parsing in a child process means:

- a time limit really works: a stuck child is killed, the web server isn't;
- a memory limit (Linux) stops one file from starving the server;
- a crash in a C library takes down only the child.

The child is started fresh ("spawn"), so it shares nothing with the server but the
arguments it is given, and it returns only plain data.

The answer comes back as a pickle, read with an allow-list: only the few result types below
can be rebuilt. If a hostile file ever took over the child, an ordinary pickle would let it
run code in the server when the answer is read; with the allow-list it can only send data.
"""
from __future__ import annotations

import importlib
import io
import multiprocessing as mp
import pickle

from .importer import FileProblem

ALLOWED_RESULT_TYPES = {
    ("builtins", "set"), ("builtins", "frozenset"),
    ("datetime", "date"), ("datetime", "datetime"), ("datetime", "time"), ("datetime", "timedelta"),
    ("datetime", "timezone"), ("decimal", "Decimal"),
    ("invoice_detector.documents", "Extraction"), ("invoice_detector.importer", "Table"),
}


class _ResultUnpickler(pickle.Unpickler):
    def find_class(self, module: str, name: str):
        if (module, name) not in ALLOWED_RESULT_TYPES:
            raise pickle.UnpicklingError(f"refused type {module}.{name}")
        return super().find_class(module, name)


def read_result(data: bytes):
    """Rebuild the child's answer, allowing only plain data and the known result types."""
    return _ResultUnpickler(io.BytesIO(data)).load()


def _send(conn, message) -> None:
    conn.send_bytes(pickle.dumps(message, protocol=pickle.HIGHEST_PROTOCOL))


def _child(conn, target: str, args: tuple, memory_mb: int) -> None:
    try:
        if memory_mb:
            try:
                import resource
                limit = memory_mb * 1024 * 1024
                resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
            except (ImportError, ValueError, OSError):
                pass   # not on this platform (Windows, macOS): the time limit still applies
        module, name = target.split(":")
        result = getattr(importlib.import_module(module), name)(*args)
        _send(conn, ("ok", result))
    except FileProblem as exc:
        _send(conn, ("problem", str(exc)))
    except MemoryError:
        _send(conn, ("problem", "This file needs too much memory to read. Try a smaller or simpler file."))
    except BaseException as exc:  # never let the details of a parser crash reach the user
        _send(conn, ("error", type(exc).__name__))
    finally:
        conn.close()


def run_isolated(target: str, *args, timeout: float = 45, memory_mb: int = 2048):
    """Call "package.module:function"(*args) in a child process and return its result.
    Raises FileProblem with a readable message if the file can't be read in time."""
    ctx = mp.get_context("spawn")
    receive, send = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_child, args=(send, target, args, memory_mb), daemon=True)
    proc.start()
    send.close()
    try:
        if not receive.poll(timeout):
            raise FileProblem(f"Reading this file took longer than {int(timeout)} seconds, so it was stopped. "
                              "It may be damaged, or far larger than an invoice.")
        try:
            kind, value = read_result(receive.recv_bytes(256 * 1024 * 1024))
        except (EOFError, OSError, pickle.UnpicklingError, ValueError, TypeError):
            raise FileProblem("This file could not be read (the reader stopped unexpectedly).") from None
    finally:
        if proc.is_alive():
            proc.kill()
        proc.join(5)
        receive.close()
    if kind == "ok":
        return value
    if kind == "problem":
        raise FileProblem(value)
    raise FileProblem("This file could not be read. It may be damaged.")
