#!/usr/bin/env python3
"""Call documented, read-only Kontur.Zakupki endpoints using Python's standard library."""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from uuid import UUID


BASE_URL = "https://api-zakupki.kontur.ru"
SPEC = json.loads((Path(__file__).resolve().parents[1] / "references" /
                   "zakupki.external_api-api.json").read_text(encoding="utf-8"))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "Redirect rejected", headers, fp)


def parse_datetime(value):
    if not isinstance(value, str) or "T" not in value:
        raise ValueError("date-time must contain a date, T, time, and timezone")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("date-time must include Z or a UTC offset")
    return parsed


def validate(value, schema, label):
    """Validate the primitive and array types used by this snapshot's requests."""
    if "$ref" in schema:
        schema = SPEC["components"]["schemas"][schema["$ref"].rsplit("/", 1)[1]]
    if value is None and schema.get("nullable"):
        return
    expected = {"string": str, "integer": int, "boolean": bool, "array": list}
    if type(value) is not expected[schema["type"]]:
        raise ValueError(f"{label} must be {schema['type']}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{label} must be one of {schema['enum']}")
    if schema.get("format") == "date-time":
        parse_datetime(value)
    if schema.get("format") == "uuid":
        UUID(value)
    if schema["type"] == "array":
        for item in value:
            validate(item, schema["items"], label)


def json_object(raw):
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("JSON input must be an object")
    return value


def prepare(method, path, params, body):
    operation = None
    for template, methods in SPEC["paths"].items():
        pattern = re.escape(template).replace(r"\{id\}", r"[A-Za-z0-9_-]+")
        if re.fullmatch(pattern, path):
            operation = methods.get(method.lower())
            break
    if operation is None:
        raise ValueError("method and relative path must match the bundled OpenAPI")

    allowed = {p["name"]: p for p in operation.get("parameters", []) if p["in"] == "query"}
    if params.keys() - allowed.keys():
        raise ValueError(f"unknown query parameters: {sorted(params.keys() - allowed.keys())}")
    for name, parameter in allowed.items():
        if parameter.get("required") and name not in params:
            raise ValueError(f"missing query parameter: {name}")
        if name in params:
            validate(params[name], parameter["schema"], name)
    if "page" in params and params["page"] < 0:
        raise ValueError("page must be non-negative")
    if len(params.get("markersNames", [])) > 100:
        raise ValueError("markersNames allows at most 100 names")
    if "dateTo" in params and parse_datetime(params["dateFrom"]) >= parse_datetime(params["dateTo"]):
        raise ValueError("dateFrom must precede dateTo")

    if method == "POST":
        if body is None:
            raise ValueError("search requires a JSON body")
        schema = SPEC["components"]["schemas"]["ExternalApiSearchArgs"]
        if body.keys() - schema["properties"].keys():
            raise ValueError("unknown search fields; use exact names from ExternalApiSearchArgs")
        for name in schema["required"]:
            if name not in body:
                raise ValueError(f"missing search field: {name}")
        for name, value in body.items():
            validate(value, schema["properties"][name], name)
        if parse_datetime(body["DateTimeFrom"]) >= parse_datetime(body["DateTimeTo"]):
            raise ValueError("DateTimeFrom must precede DateTimeTo")
        if body.get("PageNumber", 0) < 0:
            raise ValueError("PageNumber must be non-negative")
    elif body is not None:
        raise ValueError("GET does not accept a body")

    query = urllib.parse.urlencode(params, doseq=True)
    url = BASE_URL + path + ("?" + query if query else "")
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    return url, data


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--method", choices=("GET", "POST"), default="GET")
    parser.add_argument("--url", required=True, help="Relative /external/v1/... path without query")
    parser.add_argument("--params", default="{}", help="JSON object; arrays become repeated query parameters")
    parser.add_argument("--body", help="JSON object or @path/to/file.json")
    parser.add_argument("--dry-run", action="store_true", help="Validate and preview without key or network")
    args = parser.parse_args(argv)
    token = os.environ.get("KONTUR_ZAKUPKI_API_KEY", "")

    def redact(text):
        return text.replace(token, "[REDACTED]") if token else text

    try:
        params = json_object(args.params)
        raw_body = args.body
        if raw_body is not None and raw_body.startswith("@"):
            raw_body = Path(raw_body[1:]).read_text(encoding="utf-8")
        body = None if raw_body is None else json_object(raw_body)
        url, data = prepare(args.method, args.url, params, body)
        if args.dry_run:
            print(redact(json.dumps({"method": args.method, "url": url, "body": body}, ensure_ascii=False)))
            return 0
        if not token.strip():
            raise ValueError("KONTUR_ZAKUPKI_API_KEY is not set")
        headers = {"Accept": "application/json", "X-Kontur-Apikey": token}
        if data is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
        request = urllib.request.Request(url, data=data, headers=headers, method=args.method)
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
            print(redact(response.read().decode("utf-8")))
        return 0
    except urllib.error.HTTPError as error:
        hints = {
            401: "Check API key validity and expiry.",
            403: "Check subscription and endpoint permissions.",
            404: "Check purchase Id and test endpoint restrictions.",
            429: "Check quotas before retrying.",
        }
        hint = hints.get(error.code, "Request failed; no automatic retry.")
        retry_after = error.headers.get("Retry-After") if error.headers else None
        detail = error.read(8192).decode("utf-8", errors="replace")
        print(redact(f"error: HTTP {error.code}. {hint}"), file=sys.stderr)
        if retry_after:
            print(redact(f"Retry-After: {retry_after}"), file=sys.stderr)
        if detail:
            print(redact(detail), file=sys.stderr)
        return 1
    except (ValueError, OSError, urllib.error.URLError) as error:
        print(redact(f"error: {error}"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
