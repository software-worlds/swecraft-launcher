"""CodSpeed adapters for wsproto's benchmark and core tested workloads."""

from __future__ import annotations

import random
from collections.abc import Callable
from typing import Any

import wsproto
from wsproto.connection import CLIENT, SERVER
from wsproto.events import AcceptConnection, BytesMessage, Request, TextMessage
from wsproto.extensions import PerMessageDeflate
from wsproto.handshake import H11Handshake


def _consume(connection: wsproto.connection.Connection) -> int:
    return sum(1 for _ in connection.events())


def _connection_pair(
    *, compressed: bool = False,
) -> tuple[wsproto.connection.Connection, wsproto.connection.Connection]:
    if not compressed:
        return (
            wsproto.connection.Connection(CLIENT),
            wsproto.connection.Connection(SERVER),
        )

    client_extension = PerMessageDeflate()
    offer = client_extension.offer()
    assert isinstance(offer, str)
    server_extension = PerMessageDeflate()
    response = server_extension.accept(offer)
    assert isinstance(response, str)
    client_extension.finalize(response)
    return (
        wsproto.connection.Connection(CLIENT, extensions=[client_extension]),
        wsproto.connection.Connection(SERVER, extensions=[server_extension]),
    )


def _legacy_connection_workload() -> tuple[int, int]:
    """Scaled form of bench/connection.py's deterministic timed loop."""
    rand = random.Random(0)
    client, server = _connection_pair()
    events = 0
    payload_bytes = 0

    for _ in range(16):
        client_size = max(0, round(rand.gauss(125 * 1024, 75 * 1024)))
        client_message = b"0" * client_size
        server.receive_data(client.send(BytesMessage(client_message)))
        events += _consume(server)
        payload_bytes += client_size

        server_size = max(0, round(rand.gauss(125 * 1024, 75 * 1024)))
        server_message = "0" * server_size
        client.receive_data(server.send(TextMessage(server_message)))
        events += _consume(client)
        payload_bytes += server_size

    return events, payload_bytes


def test_connection_large_messages(benchmark: Callable[..., Any]) -> None:
    events, payload_bytes = benchmark(_legacy_connection_workload)
    assert events == 32
    assert payload_bytes > 0


def _fragmented_text_workload() -> tuple[int, int]:
    client, server = _connection_pair()
    events = 0
    payload_bytes = 0
    fragments = ("a" * 256, "∂" * 128, "b" * 256, "λ" * 128)

    for _ in range(128):
        for index, fragment in enumerate(fragments):
            encoded = client.send(
                TextMessage(fragment, message_finished=index == len(fragments) - 1),
            )
            payload_bytes += len(fragment.encode())
            # Exercise the incremental buffer/parser with realistic short reads.
            for offset in range(0, len(encoded), 97):
                server.receive_data(encoded[offset : offset + 97])
                events += _consume(server)

    return events, payload_bytes


def test_fragmented_text_stream(benchmark: Callable[..., Any]) -> None:
    events, payload_bytes = benchmark(_fragmented_text_workload)
    assert events >= 512
    assert payload_bytes == 128 * 1152


def _permessage_deflate_workload() -> tuple[int, int]:
    client, server = _connection_pair(compressed=True)
    binary_payload = (b"wsproto-compressible-payload-" * 2341)[: 64 * 1024]
    text_payload = ("websocket-text-∂-" * 4096)[: 64 * 1024]
    events = 0

    for _ in range(16):
        server.receive_data(client.send(BytesMessage(binary_payload)))
        events += _consume(server)
        client.receive_data(server.send(TextMessage(text_payload)))
        events += _consume(client)

    return events, 16 * (len(binary_payload) + len(text_payload.encode()))


def test_permessage_deflate_roundtrip(benchmark: Callable[..., Any]) -> None:
    events, payload_bytes = benchmark(_permessage_deflate_workload)
    assert events == 32
    assert payload_bytes > 0


def _h11_handshake_workload() -> int:
    completed = 0
    for index in range(128):
        client = H11Handshake(CLIENT)
        server = H11Handshake(SERVER)
        request_bytes = client.send(
            Request(
                host="benchmark.example",
                target=f"/stream/{index}",
                subprotocols=["chat", "telemetry"],
                extra_headers=[(b"x-benchmark", b"wsproto")],
            ),
        )
        server.receive_data(request_bytes)
        request = next(server.events())
        assert isinstance(request, Request)

        response_bytes = server.send(AcceptConnection(subprotocol="chat"))
        client.receive_data(response_bytes)
        accepted = next(client.events())
        assert isinstance(accepted, AcceptConnection)
        completed += 1
    return completed


def test_h11_opening_handshake(benchmark: Callable[..., Any]) -> None:
    assert benchmark(_h11_handshake_workload) == 128
