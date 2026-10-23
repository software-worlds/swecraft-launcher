"""The HTTP face of the capped index.

One process serves every cutoff, because the cutoff is part of the path:

    /<cutoff>/simple/<project>/     a project page, JSON (PEP 691) or HTML (PEP 503)
    /healthz                        liveness

so an image or a CI runner is pointed at a world's date by its index URL alone,
e.g. ``http://172.17.0.1:5280/2026-08-16/simple/``.
"""

from __future__ import annotations

import http.client
import json
import logging
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

from swecraft_index_cap.index import (
    JSON_TYPE,
    IndexCapError,
    cap_page,
    normalize,
    parse_cutoff,
)

logger = logging.getLogger("swecraft_index_cap")

UPSTREAM = "https://pypi.org/simple"
# A page before the cutoff never gains files, so the cache only has to notice
# yanks and deletions; an hour keeps a busy world off the upstream entirely.
CACHE_SECONDS = 3600
MISSING_SECONDS = 60
# A name lookup is not free: glibc walks the host's whole interface table over
# netlink on every getaddrinfo and serialises concurrent lookups, and a docker
# host with hundreds of networks pays seconds per lookup (954 interfaces:
# 5-8 s each). Resolve the upstream once and keep it.
RESOLVE_SECONDS = 600
USER_AGENT = (
    "swecraft-index-cap/0.1 (+https://github.com/software-worlds/swecraft-launcher)"
)


class UpstreamError(Exception):
    """The upstream index could not be read."""


@dataclass
class _Entry:
    fetched: float
    page: dict | None  # None: the upstream has no such project
    etag: str | None


class Resolver:
    """``getaddrinfo`` with a TTL cache: one lookup per host and port per TTL.

    Lookups are serialised, so a burst of cold callers costs one lookup, not one
    per caller; a failed lookup is not cached.
    """

    def __init__(
        self, *, ttl: float = RESOLVE_SECONDS, lookup=socket.getaddrinfo
    ) -> None:
        self._ttl = ttl
        self._lookup = lookup
        self._cache: dict[tuple[str, int], tuple[float, list]] = {}
        self._lock = threading.Lock()

    def addresses(self, host: str, port: int) -> list[tuple[int, tuple]]:
        """``(family, sockaddr)`` pairs for the host, in getaddrinfo order."""

        key = (host, port)
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None and time.monotonic() - hit[0] < self._ttl:
                return hit[1]
            infos = self._lookup(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
            found = [(family, sockaddr) for family, _, _, _, sockaddr in infos]
            if not found:
                raise OSError(f"no addresses for {host}")
            self._cache[key] = (time.monotonic(), found)
            return found


def _connector(resolver: Resolver):
    """A ``socket.create_connection`` that takes its addresses from the resolver."""

    def create_connection(address, timeout=None, source_address=None):
        host, port = address
        error: OSError | None = None
        for family, sockaddr in resolver.addresses(host, port):
            sock = socket.socket(family, socket.SOCK_STREAM)
            try:
                if timeout is not None:
                    sock.settimeout(timeout)
                if source_address:
                    sock.bind(source_address)
                sock.connect(sockaddr)
                return sock
            except OSError as failure:
                error = failure
                sock.close()
        raise error or OSError(f"could not connect to {host}:{port}")

    return create_connection


def _connection_classes(resolver: Resolver):
    connect = _connector(resolver)

    class HTTPConnection(http.client.HTTPConnection):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self._create_connection = connect

    class HTTPSConnection(http.client.HTTPSConnection):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self._create_connection = connect

    return HTTPConnection, HTTPSConnection


def _opener(resolver: Resolver) -> urllib.request.OpenerDirector:
    """urllib with the resolver in front of every connection it opens."""

    http_class, https_class = _connection_classes(resolver)

    class HTTPHandler(urllib.request.HTTPHandler):
        def http_open(self, request):
            return self.do_open(http_class, request)

    class HTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, request):
            return self.do_open(https_class, request, context=self._context)

    return urllib.request.build_opener(HTTPHandler(), HTTPSHandler())


class Upstream:
    """Project pages from the upstream index, cached in memory.

    Concurrent requests for one project share one upstream fetch (the first
    caller fetches, the others wait for its result), and the upstream host is
    resolved through a :class:`Resolver` rather than on every connection.
    """

    def __init__(
        self,
        base: str = UPSTREAM,
        *,
        timeout: float = 30.0,
        resolver: Resolver | None = None,
    ) -> None:
        self.base = base.rstrip("/")
        self.timeout = timeout
        self._cache: dict[str, _Entry] = {}
        self._inflight: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._opener = _opener(resolver or Resolver())

    def page(self, project: str) -> dict | None:
        name = normalize(project)
        while True:
            now = time.monotonic()
            with self._lock:
                entry = self._cache.get(name)
                if entry is not None:
                    age = now - entry.fetched
                    if age < (
                        CACHE_SECONDS if entry.page is not None else MISSING_SECONDS
                    ):
                        return entry.page
                waiting_on = self._inflight.get(name)
                if waiting_on is None:
                    waiting_on = self._inflight[name] = threading.Event()
                    fetcher = True
                else:
                    fetcher = False
            if not fetcher:
                # Another request is fetching this page; take its result. If
                # that fetch failed the cache is still stale and the loop makes
                # this request the next fetcher.
                waiting_on.wait(self.timeout * 2)
                continue
            try:
                fresh = self._fetch(name, entry)
                with self._lock:
                    self._cache[name] = fresh
                return fresh.page
            finally:
                with self._lock:
                    self._inflight.pop(name, None)
                waiting_on.set()

    def _fetch(self, name: str, known: _Entry | None) -> _Entry:
        request = urllib.request.Request(
            f"{self.base}/{name}/",
            headers={"Accept": JSON_TYPE, "User-Agent": USER_AGENT},
        )
        if known is not None and known.page is not None and known.etag:
            request.add_header("If-None-Match", known.etag)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                page = json.load(response)
                etag = response.headers.get("ETag")
        except urllib.error.HTTPError as error:
            if error.code == HTTPStatus.NOT_MODIFIED and known is not None:
                return _Entry(time.monotonic(), known.page, known.etag)
            if error.code == HTTPStatus.NOT_FOUND:
                return _Entry(time.monotonic(), None, None)
            if (
                (error.code == HTTPStatus.TOO_MANY_REQUESTS or 500 <= error.code < 600)
                and known is not None
                and known.page is not None
            ):
                logger.warning(
                    "upstream HTTP %s for %s, serving cached", error.code, name
                )
                return _Entry(time.monotonic(), known.page, known.etag)
            raise UpstreamError(f"upstream answered {error.code} for {name}") from error
        except (OSError, ValueError) as error:
            if known is not None and known.page is not None:
                # A stale page is still a true page of the past; serving it
                # beats failing an install on an upstream hiccup.
                logger.warning("upstream unreachable for %s, serving cached", name)
                return _Entry(time.monotonic(), known.page, known.etag)
            raise UpstreamError(f"upstream unreachable for {name}: {error}") from error
        if not isinstance(page, dict):
            raise UpstreamError(f"upstream page for {name} is not an object")
        return _Entry(time.monotonic(), page, etag)


def _wants_json(accept: str) -> bool:
    """Whether the client listed the JSON form at all; HTML is the fallback."""

    return JSON_TYPE in accept or "application/json" in accept


def make_handler(upstream: Upstream) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "swecraft-index-cap/0.1"
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            logger.debug("%s %s", self.address_string(), format % args)

        def _send(
            self, status: int, body: bytes, content_type: str, *, head: bool = False
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=600, public")
            self.end_headers()
            if not head:
                self.wfile.write(body)

        def _text(self, status: int, message: str, *, head: bool = False) -> None:
            self._send(status, (message + "\n").encode(), "text/plain", head=head)

        def do_HEAD(self) -> None:  # noqa: N802
            self._serve(head=True)

        def do_GET(self) -> None:  # noqa: N802
            self._serve(head=False)

        def _serve(self, *, head: bool) -> None:
            path = unquote(self.path.split("?", 1)[0])
            if path == "/healthz":
                self._text(HTTPStatus.OK, "ok", head=head)
                return
            parts = [part for part in path.split("/") if part]
            if len(parts) == 2 and parts[1] == "simple":
                self._text(
                    HTTPStatus.NOT_FOUND,
                    "this index serves project pages only: /<cutoff>/simple/<project>/",
                    head=head,
                )
                return
            if len(parts) != 3 or parts[1] != "simple":
                self._text(
                    HTTPStatus.NOT_FOUND,
                    "use /<cutoff>/simple/<project>/, "
                    "e.g. /2026-08-16/simple/requests/",
                    head=head,
                )
                return
            try:
                cutoff = parse_cutoff(parts[0])
            except IndexCapError as error:
                self._text(HTTPStatus.BAD_REQUEST, str(error), head=head)
                return
            project = parts[2]
            try:
                page = upstream.page(project)
            except UpstreamError as error:
                logger.error("%s", error)
                self._text(HTTPStatus.BAD_GATEWAY, str(error), head=head)
                return
            if page is None:
                self._text(HTTPStatus.NOT_FOUND, f"no project {project}", head=head)
                return
            try:
                capped = cap_page(page, cutoff)
            except IndexCapError as error:
                self._text(HTTPStatus.BAD_GATEWAY, str(error), head=head)
                return
            logger.info(
                "%s %s @%s kept=%d dropped=%d",
                self.address_string(),
                normalize(project),
                parts[0],
                capped.kept,
                capped.dropped,
            )
            if _wants_json(self.headers.get("Accept", "")):
                self._send(HTTPStatus.OK, capped.json(), JSON_TYPE, head=head)
            else:
                # text/html, not the versioned type: the HTML form exists for
                # installers older than PEP 691, and those accept nothing else.
                self._send(
                    HTTPStatus.OK, capped.html(), "text/html; charset=utf-8", head=head
                )

    return Handler


def serve(host: str, port: int, *, upstream: Upstream | None = None) -> None:
    server = ThreadingHTTPServer((host, port), make_handler(upstream or Upstream()))
    server.daemon_threads = True
    logger.info("capped index on http://%s:%d/<cutoff>/simple/", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
