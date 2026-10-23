from __future__ import annotations

import json
import socket
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from swecraft_index_cap.index import (
    JSON_TYPE,
    IndexCapError,
    cap_page,
    normalize,
    parse_cutoff,
)
from swecraft_index_cap.server import Upstream, make_handler


def file(name: str, uploaded: str | None, **extra: object) -> dict:
    entry = {
        "filename": name,
        "url": f"https://files.example/{name}",
        "hashes": {"sha256": "ab" * 32},
        **extra,
    }
    if uploaded is not None:
        entry["upload-time"] = uploaded
    return entry


# urllib3's shape in September 2026: the pinned 2.7.0, the 1.26.x maintenance
# line beside it, and 2.8.0, published three weeks after the world's pin.
URLLIB3 = {
    "meta": {"api-version": "1.4"},
    "name": "urllib3",
    "versions": ["1.26.20", "2.7.0", "2.8.0"],
    "files": [
        file("urllib3-1.26.20-py2.py3-none-any.whl", "2024-08-29T15:43:08.921Z"),
        file("urllib3-2.7.0-py3-none-any.whl", "2026-05-07T16:13:01.000000Z"),
        file(
            "urllib3-2.7.0.tar.gz",
            "2026-05-07T16:13:05.000000Z",
            **{"requires-python": ">=3.10", "core-metadata": {"sha256": "cd" * 32}},
        ),
        file("urllib3-2.8.0-py3-none-any.whl", "2026-09-15T19:29:40.000000Z"),
        file("urllib3-2.8.0.tar.gz", "2026-09-15T19:29:44.000000Z"),
    ],
}
CUTOFF = datetime(2026, 8, 16, tzinfo=UTC)


class CutoffTests(unittest.TestCase):
    def test_a_date_is_the_first_instant_of_that_day(self) -> None:
        self.assertEqual(parse_cutoff("2026-08-16"), CUTOFF)
        self.assertEqual(parse_cutoff("2026-08-16T00:00:00Z"), CUTOFF)
        self.assertEqual(parse_cutoff("2026-08-15T20:00:00-04:00"), CUTOFF)

    def test_a_cutoff_without_a_zone_or_a_shape_is_refused(self) -> None:
        for bad in ("2026-08-16T00:00:00", "last tuesday", "", "2026-13-40"):
            with self.assertRaises(IndexCapError):
                parse_cutoff(bad)


class CapPageTests(unittest.TestCase):
    def test_the_future_release_is_withheld_and_the_past_is_kept(self) -> None:
        capped = cap_page(URLLIB3, CUTOFF)
        names = [entry["filename"] for entry in capped.page["files"]]
        self.assertEqual(
            names,
            [
                "urllib3-1.26.20-py2.py3-none-any.whl",
                "urllib3-2.7.0-py3-none-any.whl",
                "urllib3-2.7.0.tar.gz",
            ],
        )
        self.assertEqual((capped.kept, capped.dropped), (3, 2))
        self.assertEqual(capped.page["versions"], ["1.26.20", "2.7.0"])
        self.assertEqual(capped.page["meta"], URLLIB3["meta"])
        self.assertEqual(capped.page["name"], "urllib3")

    def test_the_upstream_page_is_not_modified(self) -> None:
        before = json.dumps(URLLIB3, sort_keys=True)
        cap_page(URLLIB3, CUTOFF)
        self.assertEqual(json.dumps(URLLIB3, sort_keys=True), before)

    def test_a_file_uploaded_at_the_cutoff_itself_is_withheld(self) -> None:
        page = {
            "name": "demo",
            "files": [file("demo-1.tar.gz", "2026-08-16T00:00:00Z")],
        }
        self.assertEqual(cap_page(page, CUTOFF).kept, 0)

    def test_a_file_that_cannot_be_dated_is_withheld(self) -> None:
        page = {
            "name": "demo",
            "files": [file("demo-1.tar.gz", None), file("demo-2.tar.gz", "soon")],
        }
        capped = cap_page(page, CUTOFF)
        self.assertEqual((capped.kept, capped.dropped), (0, 2))

    def test_a_page_without_a_file_list_is_an_error(self) -> None:
        with self.assertRaises(IndexCapError):
            cap_page({"name": "demo"}, CUTOFF)

    def test_html_carries_what_an_old_installer_needs(self) -> None:
        text = cap_page(URLLIB3, CUTOFF).html().decode()
        self.assertIn(
            'href="https://files.example/urllib3-2.7.0.tar.gz#sha256=' + "ab" * 32,
            text,
        )
        self.assertIn('data-requires-python="&gt;=3.10"', text)
        self.assertIn(f'data-core-metadata="sha256={"cd" * 32}"', text)
        self.assertNotIn("2.8.0", text)

    def test_names_normalize_as_the_index_does(self) -> None:
        self.assertEqual(normalize("Requests_GSSAPI"), "requests-gssapi")
        self.assertEqual(normalize("zope.interface"), "zope-interface")


class _FakePyPI(BaseHTTPRequestHandler):
    hits = 0
    status = 200

    def log_message(self, *args: object) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802
        type(self).hits += 1
        if type(self).status != 200:
            body = b"upstream failure"
            self.send_response(type(self).status)
        elif self.path == "/urllib3/":
            body = json.dumps(URLLIB3).encode()
            self.send_response(200)
            self.send_header("Content-Type", JSON_TYPE)
        else:
            body = b"not found"
            self.send_response(404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ResolverTests(unittest.TestCase):
    def test_one_lookup_per_ttl_even_under_a_burst(self) -> None:
        from swecraft_index_cap.server import Resolver

        calls = []

        def lookup(host, port, family, kind):
            calls.append(host)
            time.sleep(0.05)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

        resolver = Resolver(ttl=60, lookup=lookup)
        threads = [
            threading.Thread(target=resolver.addresses, args=("pypi.org", 443))
            for _ in range(20)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(calls, ["pypi.org"])
        self.assertEqual(
            resolver.addresses("pypi.org", 443),
            [(socket.AF_INET, ("127.0.0.1", 443))],
        )

    def test_expired_entries_are_looked_up_again(self) -> None:
        from swecraft_index_cap.server import Resolver

        calls = []

        def lookup(host, port, family, kind):
            calls.append(host)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

        resolver = Resolver(ttl=0, lookup=lookup)
        resolver.addresses("pypi.org", 443)
        resolver.addresses("pypi.org", 443)
        self.assertEqual(len(calls), 2)


class SingleFlightTests(unittest.TestCase):
    def test_concurrent_requests_for_one_project_fetch_once(self) -> None:
        from swecraft_index_cap.server import Upstream, _Entry

        fetches = []

        class Counting(Upstream):
            def _fetch(self, name, known):
                fetches.append(name)
                time.sleep(0.1)
                return _Entry(time.monotonic(), {"name": name, "files": []}, None)

        upstream = Counting("http://127.0.0.1:1")
        pages = []
        threads = [
            threading.Thread(target=lambda: pages.append(upstream.page("Requests")))
            for _ in range(25)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(fetches, ["requests"])
        self.assertEqual(len(pages), 25)
        self.assertTrue(
            all(page == {"name": "requests", "files": []} for page in pages)
        )

    def test_a_failed_fetch_lets_the_next_request_try_again(self) -> None:
        from swecraft_index_cap.server import Upstream, UpstreamError, _Entry

        attempts = []

        class Flaky(Upstream):
            def _fetch(self, name, known):
                attempts.append(name)
                if len(attempts) == 1:
                    raise UpstreamError("first try fails")
                return _Entry(time.monotonic(), {"name": name, "files": []}, None)

        upstream = Flaky("http://127.0.0.1:1")
        with self.assertRaises(UpstreamError):
            upstream.page("requests")
        self.assertEqual(upstream.page("requests"), {"name": "requests", "files": []})
        self.assertEqual(len(attempts), 2)


class ServerTests(unittest.TestCase):
    def setUp(self) -> None:
        _FakePyPI.hits = 0
        _FakePyPI.status = 200
        self.pypi = ThreadingHTTPServer(("127.0.0.1", 0), _FakePyPI)
        upstream = Upstream(f"http://127.0.0.1:{self.pypi.server_port}")
        self.upstream = upstream
        self.cap = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(upstream))
        for server in (self.pypi, self.cap):
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        self.base = f"http://127.0.0.1:{self.cap.server_port}"

    def get(self, path: str, accept: str = JSON_TYPE) -> tuple[int, str, bytes]:
        request = urllib.request.Request(self.base + path, headers={"Accept": accept})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return (
                    response.status,
                    response.headers["Content-Type"],
                    response.read(),
                )
        except urllib.error.HTTPError as error:
            return error.code, error.headers["Content-Type"], error.read()

    def test_a_json_client_gets_the_capped_page(self) -> None:
        status, content_type, body = self.get("/2026-08-16/simple/urllib3/")
        self.assertEqual((status, content_type), (200, JSON_TYPE))
        page = json.loads(body)
        self.assertEqual(page["versions"], ["1.26.20", "2.7.0"])
        self.assertTrue(all("upload-time" in entry for entry in page["files"]))

    def test_the_cutoff_is_per_request(self) -> None:
        _, _, later = self.get("/2026-09-16T00:00:00Z/simple/urllib3/")
        self.assertEqual(json.loads(later)["versions"], ["1.26.20", "2.7.0", "2.8.0"])
        _, _, earlier = self.get("/2025-01-01/simple/urllib3/")
        self.assertEqual(json.loads(earlier)["versions"], ["1.26.20"])

    def test_an_installer_that_asks_for_html_gets_html(self) -> None:
        status, content_type, body = self.get(
            "/2026-08-16/simple/URLLIB3/", accept="text/html"
        )
        self.assertEqual(status, 200)
        self.assertTrue(content_type.startswith("text/html"))
        self.assertIn(b"urllib3-2.7.0.tar.gz", body)
        self.assertNotIn(b"2.8.0", body)

    def test_pages_are_cached_across_cutoffs(self) -> None:
        self.get("/2026-08-16/simple/urllib3/")
        self.get("/2026-08-17/simple/urllib3/")
        self.assertEqual(_FakePyPI.hits, 1)

    def test_expired_page_survives_transient_http_errors(self) -> None:
        for status in (429, 500, 502, 503, 504):
            with self.subTest(status=status):
                _FakePyPI.status = 200
                self.upstream._cache.clear()
                _, _, before = self.get("/2026-08-16/simple/urllib3/")
                self.upstream._cache["urllib3"].fetched = float("-inf")
                _FakePyPI.status = status
                hits = _FakePyPI.hits
                actual, _, after = self.get("/2026-08-16/simple/urllib3/")
                self.assertEqual(actual, 200)
                self.assertEqual(after, before)
                self.assertEqual(_FakePyPI.hits, hits + 1)
                # Revalidated stale data is cached; don't hammer the failing upstream.
                self.get("/2026-08-16/simple/urllib3/")
                self.assertEqual(_FakePyPI.hits, hits + 1)

    def test_transient_http_error_without_cached_page_still_fails(self) -> None:
        for status in (429, 500, 502, 503, 504):
            with self.subTest(status=status):
                _FakePyPI.status = status
                self.assertEqual(self.get("/2026-08-16/simple/urllib3/")[0], 502)
                self.assertNotIn("urllib3", self.upstream._cache)

    def test_expired_page_does_not_mask_a_deleted_project(self) -> None:
        self.get("/2026-08-16/simple/urllib3/")
        self.upstream._cache["urllib3"].fetched = float("-inf")
        _FakePyPI.status = 404
        self.assertEqual(self.get("/2026-08-16/simple/urllib3/")[0], 404)
        self.assertIsNone(self.upstream._cache["urllib3"].page)

    def test_errors_say_what_to_do(self) -> None:
        self.assertEqual(self.get("/2026-08-16/simple/no-such-project/")[0], 404)
        self.assertEqual(self.get("/yesterday/simple/urllib3/")[0], 400)
        self.assertEqual(self.get("/2026-08-16/simple/")[0], 404)
        self.assertEqual(self.get("/healthz")[0], 200)


if __name__ == "__main__":
    unittest.main()
