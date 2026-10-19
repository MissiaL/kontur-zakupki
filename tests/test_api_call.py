import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, HTTPServer
import urllib.error
import urllib.parse
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
loader = importlib.util.spec_from_file_location("api_call", ROOT / "scripts" / "api_call.py")
api = importlib.util.module_from_spec(loader)
loader.loader.exec_module(api)
SEARCH = {"DateTimeFrom": "2026-10-01T00:00:00Z", "DateTimeTo": "2026-10-02T00:00:00Z"}


class ClientTests(unittest.TestCase):
    def run_client(self, args, token="test-secret", **kwargs):
        output, errors = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"KONTUR_ZAKUPKI_API_KEY": token}), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = api.main(args, **kwargs)
        return code, output.getvalue(), errors.getvalue()

    def test_every_documented_operation_can_be_prepared(self):
        for template, methods in api.SPEC["paths"].items():
            for method in methods:
                with self.subTest(path=template, method=method):
                    path = template.replace("{id}", "0141600001021000268")
                    params = {"dateFrom": SEARCH["DateTimeFrom"]} if "favorites" in path else {}
                    body = SEARCH if method == "post" else None
                    url, data = api.prepare(method.upper(), path, params, body)
                    self.assertEqual(urllib.parse.urlsplit(url).netloc, "api-zakupki.kontur.ru")
                    self.assertEqual(urllib.parse.urlsplit(url).path, path)

    def test_search_preserves_json_types_and_unicode(self):
        body = dict(SEARCH, Text=["оборудование"], IncludeInns=["0123456789"],
                    Laws=[1, 2], PageNumber=0, StrictSearch=False, MaxPriceFrom=100000)
        _, data = api.prepare("POST", "/external/v1/search", {}, body)
        self.assertEqual(json.loads(data), body)

    def test_favorite_arrays_use_repeated_query_parameters(self):
        params = {"dateFrom": SEARCH["DateTimeFrom"], "markersNames": ["Интересно", "Участвую"], "page": 0}
        url, _ = api.prepare("GET", "/external/v1/favorites", params, None)
        decoded = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.assertEqual(decoded["markersNames"], params["markersNames"])
        self.assertEqual(decoded["page"], ["0"])

    def test_arbitrary_urls_and_methods_are_rejected(self):
        for path in ("https://evil.example/external/v1/markers", "//evil.example/external/v1/markers",
                     "/external/v1/markers?apiKey=foo", "/external/v1/purchases/../markers",
                     "/external/v1/purchases/%2e%2e", "/external/v1/markers#fragment",
                     "/external/v1/markers\n", "/external/v1/purchases/a/b"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                api.prepare("GET", path, {}, None)
        with self.assertRaises(ValueError):
            api.prepare("POST", "/external/v1/markers", {}, {})

    def test_invalid_search_is_rejected(self):
        for body in ({}, dict(SEARCH, text=["wrong case"]), dict(SEARCH, Laws=[44]),
                     dict(SEARCH, Text="not-array"), dict(SEARCH, PageNumber=True),
                     dict(SEARCH, PageNumber=-1), dict(SEARCH, DateTimeTo=SEARCH["DateTimeFrom"]),
                     dict(SEARCH, DateTimeFrom="2026-10-01T00:00:00"),
                     dict(SEARCH, RegionIds=["77"]), dict(SEARCH, IncludeInns=[1234567890])):
            with self.subTest(body=body), self.assertRaises(ValueError):
                api.prepare("POST", "/external/v1/search", {}, body)

    def test_invalid_query_is_rejected(self):
        valid = {"dateFrom": SEARCH["DateTimeFrom"]}
        for params in ({}, dict(valid, page=-1), dict(valid, page="0"),
                       dict(valid, markersNames=["x"] * 101), dict(valid, sortOrder=8),
                       dict(valid, dateTo="2026-09-30T00:00:00Z"), dict(valid, token="secret")):
            with self.subTest(params=params), self.assertRaises(ValueError):
                api.prepare("GET", "/external/v1/favorites", params, None)
        with self.assertRaises(ValueError):
            api.prepare("GET", "/external/v1/markers", {"page": 0}, None)
        with self.assertRaises(ValueError):
            api.prepare("GET", "/external/v1/markers", {}, {})

    def test_null_optional_body_fields_are_valid(self):
        body = dict(SEARCH, Text=None, RegionIds=None, MaxPriceFrom=None)
        self.assertEqual(json.loads(api.prepare("POST", "/external/v1/search", {}, body)[1]), body)

    def test_dry_run_needs_no_key_or_network(self):
        with patch.object(api.urllib.request, "build_opener") as opener:
            code, output, errors = self.run_client(["--url", "/external/v1/markers", "--dry-run"], token="")
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(json.loads(output)["method"], "GET")
        opener.assert_not_called()

    def test_missing_key_never_sends_request(self):
        with patch.object(api.urllib.request, "build_opener") as opener:
            code, _, errors = self.run_client(["--url", "/external/v1/markers"], token="")
        self.assertEqual(code, 1)
        self.assertIn("KONTUR_ZAKUPKI_API_KEY", errors)
        opener.assert_not_called()

    def test_body_can_be_read_from_file_outside_working_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            body_file = Path(folder) / "search.json"
            body_file.write_text(json.dumps(SEARCH), encoding="utf-8")
            code, output, errors = self.run_client(["--method", "POST", "--url", "/external/v1/search",
                                                   "--body", "@" + str(body_file), "--dry-run"])
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(json.loads(output)["body"], SEARCH)

    def test_invalid_json_does_not_reach_network(self):
        for raw in ("[]", "null", "{broken"):
            with self.subTest(raw=raw), patch.object(api.urllib.request, "build_opener") as opener:
                code, _, _ = self.run_client(["--url", "/external/v1/markers", "--params", raw])
                self.assertEqual(code, 1)
                opener.assert_not_called()

    def test_request_uses_api_key_header_and_json(self):
        with patch.object(api.urllib.request, "build_opener") as opener:
            opener.return_value.open.return_value.__enter__.return_value.read.return_value = b'{"Items":[]}'
            code, output, errors = self.run_client(["--method", "POST", "--url", "/external/v1/search",
                                                   "--body", json.dumps(SEARCH)])
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.get_header("X-kontur-apikey"), "test-secret")
            self.assertIsNone(request.get_header("Authorization"))
            self.assertEqual(json.loads(request.data), SEARCH)
            self.assertEqual(opener.return_value.open.call_args.kwargs["timeout"], 30)
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(json.loads(output), {"Items": []})

    def test_http_error_is_redacted_and_never_retried(self):
        for status in (401, 403, 404, 429, 500):
            with self.subTest(status=status), patch.object(api.urllib.request, "build_opener") as opener:
                opener.return_value.open.side_effect = urllib.error.HTTPError(
                    api.BASE_URL, status, "error", {"Retry-After": "60"}, io.BytesIO(b"test-secret"))
                code, _, errors = self.run_client(["--url", "/external/v1/markers"])
                self.assertEqual(code, 1)
                self.assertIn(f"HTTP {status}", errors)
                self.assertIn("Retry-After: 60", errors)
                self.assertNotIn("test-secret", errors)
                opener.return_value.open.assert_called_once()

    def test_network_error_is_redacted(self):
        with patch.object(api.urllib.request, "build_opener") as opener:
            opener.return_value.open.side_effect = urllib.error.URLError("test-secret failed")
            code, _, errors = self.run_client(["--url", "/external/v1/markers"])
        self.assertEqual(code, 1)
        self.assertIn("[REDACTED]", errors)
        self.assertNotIn("test-secret", errors)

    def test_redirect_handler_does_not_follow_redirect(self):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.path)
                self.send_response(302)
                self.send_header("Location", "/sink")
                self.end_headers()

            def log_message(self, *args):
                pass

        with HTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}/start"
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), api.NoRedirect())
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    opener.open(url, timeout=5)
                raised.exception.close()
                self.assertEqual(raised.exception.code, 302)
                self.assertEqual(seen, ["/start"])
            finally:
                server.shutdown()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
