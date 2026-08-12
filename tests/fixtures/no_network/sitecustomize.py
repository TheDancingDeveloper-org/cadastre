"""Python child-process half of tests/conftest.py's network guard."""

from __future__ import annotations

import ipaddress
import os
import socket


def _loopback(address: object) -> bool:
    if not isinstance(address, tuple) or not address:
        return True
    host = address[0]
    if not isinstance(host, str):
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


if os.environ.get("CADASTRE_TEST_NO_NETWORK") == "1":
    _connect = socket.socket.connect
    _connect_ex = socket.socket.connect_ex

    def connect(sock: socket.socket, address: object) -> object:
        if not _loopback(address):
            raise AssertionError(
                f"test child attempted a non-loopback connection: {address!r}"
            )
        return _connect(sock, address)  # type: ignore[arg-type]

    def connect_ex(sock: socket.socket, address: object) -> int:
        if not _loopback(address):
            raise AssertionError(
                f"test child attempted a non-loopback connection: {address!r}"
            )
        return _connect_ex(sock, address)  # type: ignore[arg-type]

    socket.socket.connect = connect  # type: ignore[method-assign, assignment]
    socket.socket.connect_ex = connect_ex  # type: ignore[method-assign, assignment]
