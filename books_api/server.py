"""Local HTTP interface. Deploy behind a hardened server before public use."""

import json
import logging
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .errors import ApiError, invalid_query
from .service import BOOKS_PATH, CatalogueService
from .source import ORIGIN

LOGGER = logging.getLogger(__name__)


class ApiServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, catalogue: CatalogueService) -> None:
        self.catalogue = catalogue
        super().__init__(address, ApiHandler)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(15)
        return connection, address


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "PublicBooksAPI/" + __version__
    sys_version = ""

    def _send(self, status: int, payload: dict, headers=None) -> None:
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.close_connection = True
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass  # A disconnected caller must not affect other requests.

    def _route(self) -> dict:
        if len(self.path) > 4096:
            raise ApiError(414, "request_too_long", "The request target exceeds 4096 characters.")
        try:
            url = urlsplit(self.path)
            if url.scheme or url.netloc or url.fragment or re.search(r"%(?![0-9a-fA-F]{2})", self.path):
                raise ValueError("Invalid request target")
            parsed = {}
            if url.query:
                parsed = parse_qs(
                    url.query,
                    keep_blank_values=True,
                    strict_parsing=True,
                    max_num_fields=16,
                    encoding="utf-8",
                    errors="strict",
                )
        except ValueError as error:
            raise invalid_query("The request URL or query string is malformed.") from error
        if any(len(values) != 1 for values in parsed.values()):
            raise invalid_query("Query parameters must not be repeated.")
        parameters = {name: values[0] for name, values in parsed.items()}
        if url.path != BOOKS_PATH and parameters:
            raise invalid_query("This endpoint does not accept query parameters.")
        if url.path == "/":
            return {
                "name": "Public Books API",
                "version": __version__,
                "official": False,
                "source": ORIGIN,
                "endpoints": {
                    "health": "/health",
                    "categories": "/api/v1/categories",
                    "books": BOOKS_PATH,
                    "book_detail": BOOKS_PATH + "/{book_id}",
                },
                "book_filters": ["page", "category", "q", "min_price", "max_price", "rating"],
                "filter_scope": "source_page",
            }
        if url.path == "/health":
            return {"status": "ok", "version": __version__, "upstream_checked": False}
        if url.path == "/api/v1/categories":
            return self.server.catalogue.categories()
        if url.path == BOOKS_PATH:
            return self.server.catalogue.books(parameters)
        if url.path.startswith(BOOKS_PATH + "/"):
            book_id = url.path[len(BOOKS_PATH) + 1:]
            return self.server.catalogue.book(book_id)
        raise ApiError(404, "not_found", "The requested API endpoint does not exist.")

    def do_GET(self) -> None:
        try:
            payload = self._route()
            self._send(200, payload)
        except ApiError as error:
            self._send(error.status, error.as_dict(), error.headers)
        except Exception:
            LOGGER.exception("Unhandled API error")
            error = ApiError(500, "internal_error", "The API could not complete this request.")
            self._send(error.status, error.as_dict())

    def do_HEAD(self) -> None:
        self.do_GET()

    def _read_only(self) -> None:
        error = ApiError(405, "method_not_allowed", "Only GET and HEAD are supported.")
        self._send(error.status, error.as_dict(), {"Allow": "GET, HEAD"})

    do_POST = _read_only
    do_PUT = _read_only
    do_PATCH = _read_only
    do_DELETE = _read_only
    do_OPTIONS = _read_only
    do_TRACE = _read_only
    do_CONNECT = _read_only

    def send_error(self, code, message=None, explain=None):
        # Keep HTTP-parser errors and unknown methods in the same JSON envelope.
        error = ApiError(code, "http_error", "The HTTP request could not be processed.")
        self._send(code, error.as_dict())

    def log_message(self, format, *arguments):
        LOGGER.info("%s %s", self.client_address[0], format % arguments)


def make_server(
    host: str = "127.0.0.1",
    port: int = 8000,
    catalogue: Optional[CatalogueService] = None,
) -> ApiServer:
    return ApiServer((host, port), catalogue if catalogue is not None else CatalogueService())