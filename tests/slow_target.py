"""Misbehaving 'parsers' for the isolation tests (run in a child process)."""
import time


def hang():
    time.sleep(120)


def hog():
    chunks = []
    while True:
        chunks.append(bytearray(64 * 1024 * 1024))
