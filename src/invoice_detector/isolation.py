"""Run risky work (parsing uploaded files) in a separate, short-lived process.

PDF and image parsers are large C and Python libraries handed bytes chosen by whoever sent
the file. A malformed or hostile file can make them spin for minutes or eat memory. Doing
the parsing in a child process means:

- a time limit really works: a stuck child is killed, the web server isn't;
- a memory limit (Linux) stops one file from starving the server;
- a crash in a C library takes down only the child.

The child is started fresh ("spawn"), so it shares nothing with the server but the
arguments it is given, and it returns only plain data.
"""
from __future__ import annotations

import importlib
import multiprocessing as mp

from .importer import FileProblem


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
        conn.send(("ok", result))
    except FileProblem as exc:
        conn.send(("problem", str(exc)))
    except MemoryError:
        conn.send(("problem", "This file needs too much memory to read. Try a smaller or simpler file."))
    except BaseException as exc:  # never let the details of a parser crash reach the user
        conn.send(("error", type(exc).__name__))
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
            kind, value = receive.recv()
        except EOFError:
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
