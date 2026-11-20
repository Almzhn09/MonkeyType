"""Bounded, strict JSON lines for policy IPC and recorded actions."""

import json
from hashlib import sha256
from math import isfinite
import os
import selectors
from time import monotonic

MAX_MESSAGE = 1024 * 1024
MAX_TRACE_ROW = 2 * MAX_MESSAGE + 1024


class MessageError(ValueError):
    pass


class MessageTooLarge(MessageError):
    pass


def finite_float(value):
    parsed = float(value)
    if not isfinite(parsed):
        raise ValueError("Number is out of range")
    return parsed


def reject_constant(value):
    raise ValueError("Non-JSON numeric constant")


def unique_object(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def parse_json(data):
    try:
        return json.loads(
            data, parse_constant=reject_constant, parse_float=finite_float, object_pairs_hook=unique_object,
        )
    except (ValueError, RecursionError) as error:
        raise MessageError("Invalid JSON") from error


def encode_json(message, *, sort_keys=False):
    encoded = json.dumps(
        message, ensure_ascii=True, allow_nan=False, separators=(",", ":"), sort_keys=sort_keys,
    )
    return (encoded + "\n").encode("ascii")


def fingerprint(message):
    return sha256(encode_json(message, sort_keys=True)).hexdigest()


class MessageReader:
    """Read one line at a time, retaining subsequent lines until requested."""

    def __init__(self, fd, *, limit=MAX_MESSAGE):
        self.fd = fd
        self.limit = limit
        self.pending = bytearray()

    def read(self, deadline):
        with selectors.PollSelector() as selector:
            selector.register(self.fd, selectors.EVENT_READ)
            while True:
                end = self.pending.find(b"\n", 0, self.limit)
                if end != -1:
                    line = bytes(self.pending[:end])
                    del self.pending[:end + 1]
                    return parse_json(line)
                if len(self.pending) >= self.limit:
                    raise MessageTooLarge("Message exceeded its size limit")
                remaining = deadline - monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise TimeoutError("Input exceeded its deadline")
                data = os.read(self.fd, 8192)
                if not data:
                    if self.pending:
                        self.pending.clear()
                        raise MessageError("Message must end in a newline")
                    raise EOFError("Input closed")
                self.pending.extend(data)


def send_message(fd, message, deadline):
    data = memoryview(encode_json(message))
    if len(data) > MAX_MESSAGE:
        raise MessageTooLarge("Message exceeded its size limit")
    with selectors.PollSelector() as selector:
        selector.register(fd, selectors.EVENT_WRITE)
        while data:
            remaining = deadline - monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise TimeoutError("Output exceeded its deadline")
            try:
                count = os.write(fd, data)
            except BlockingIOError:
                continue
            data = data[count:]
