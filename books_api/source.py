"""Bounded, paced access to a single public HTTPS origin; no cookies or login."""

import math
import re
import socket
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.robotparser import RobotFileParser

from .errors import ApiError

ORIGIN = "https://books.toscrape.com"
USER_AGENT = "PublicBooksAPI/1.0 (educational; public HTML only)"
MAX_RESPONSE_BYTES = 1024 * 1024
IDENTIFIER_PATTERN = r"[a-z0-9]+(?:-[a-z0-9]+)*_[1-9][0-9]*"
SOURCE_PATH = re.compile(
    r"/catalogue/(?:page-[1-9][0-9]*\.html|{identifier}/index\.html|"
    r"category/books_1/index\.html|"
    r"category/books/{identifier}/(?:index|page-[1-9][0-9]*)\.html)\Z".format(
        identifier=IDENTIFIER_PATTERN,
    )
)


@dataclass(frozen=True)
class SourceResponse:
    status: int
    content_type: str
    body: bytes
    charset: str = "utf-8"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        # Do not follow redirects to login pages, other origins, or private hosts.
        return None


def fetch_public_url(url: str, timeout: float, max_bytes: int) -> SourceResponse:
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html, text/plain;q=0.9",
            "Accept-Encoding": "identity",
        },
    )
    try:
        with build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise ApiError(502, "source_response_too_large", "The source response exceeded the size limit.")
            return SourceResponse(
                response.status,
                response.headers.get_content_type(),
                body,
                response.headers.get_content_charset() or "utf-8",
            )
    except HTTPError as error:
        # We need only the status, not error-page HTML or authentication challenges.
        with error:
            return SourceResponse(error.code, error.headers.get_content_type(), b"")
    except (socket.timeout, TimeoutError) as error:
        raise ApiError(504, "source_timeout", "The public source did not respond in time.") from error
    except URLError as error:
        if isinstance(error.reason, (socket.timeout, TimeoutError)):
            raise ApiError(504, "source_timeout", "The public source did not respond in time.") from error
        raise ApiError(502, "source_unavailable", "The public source could not be reached.") from error
    except OSError as error:
        raise ApiError(502, "source_unavailable", "The public source could not be reached.") from error


class SourceClient:
    def __init__(
        self,
        *,
        timeout: float = 10.0,
        cache_ttl: float = 300.0,
        cache_size: int = 32,
        min_interval: float = 1.0,
        robots_ttl: float = 3600.0,
        transport: Optional[Callable[[str, float, int], SourceResponse]] = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if timeout <= 0 or cache_ttl < 0 or cache_size < 1 or min_interval < 0 or robots_ttl <= 0:
            raise ValueError("Invalid source-client limits.")
        self.timeout = timeout
        self.cache_ttl = cache_ttl
        self.cache_size = cache_size
        self.min_interval = min_interval
        self.robots_ttl = robots_ttl
        self.transport = transport or fetch_public_url
        self.clock = clock
        self.sleep = sleep
        self._cache = OrderedDict()
        self._robots = None
        self._robots_expiry = 0.0
        self._last_fetch_finished = None
        self._effective_interval = min_interval
        self._busy_until = 0.0
        # One lock serializes misses, preventing duplicate concurrent source fetches.
        self._lock = threading.RLock()

    @staticmethod
    def validate_url(url: str) -> None:
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or parts.netloc != "books.toscrape.com"
            or parts.query
            or parts.fragment
            or "%" in parts.path
            or "\\" in parts.path
            or any(part in (".", "..") for part in parts.path.split("/"))
            or not (parts.path in ("/", "/robots.txt") or SOURCE_PATH.fullmatch(parts.path))
        ):
            raise ApiError(502, "unsafe_source_link", "An unexpected source URL was rejected.")

    def _fetch(self, url: str) -> SourceResponse:
        self.validate_url(url)
        remaining = self._busy_until - self.clock()
        if remaining > 0:
            raise ApiError(
                503,
                "source_busy",
                "The source is cooling down; try again later.",
                {"Retry-After": str(math.ceil(remaining))},
            )
        if self._last_fetch_finished is not None:
            delay = self._last_fetch_finished + self._effective_interval - self.clock()
            if delay > 0:
                self.sleep(delay)
        try:
            response = self.transport(url, self.timeout, MAX_RESPONSE_BYTES)
        finally:
            # Space actual requests, not cache hits, including after failures.
            self._last_fetch_finished = self.clock()
        if len(response.body) > MAX_RESPONSE_BYTES:
            raise ApiError(502, "source_response_too_large", "The source response exceeded the size limit.")
        return response

    @staticmethod
    def _text(response: SourceResponse) -> str:
        try:
            return response.body.decode(response.charset)
        except (UnicodeError, LookupError) as error:
            raise ApiError(502, "source_encoding_changed", "The source returned unreadable text.") from error

    def _check_robots(self, url: str) -> None:
        if self._robots is None or self.clock() >= self._robots_expiry:
            try:
                response = self._fetch(ORIGIN + "/robots.txt")
                parser = RobotFileParser(ORIGIN + "/robots.txt")
                if response.status == 404:
                    # This sandbox explicitly invites scraping and currently has no robots file.
                    parser.parse(["User-agent: *", "Disallow:"])
                elif response.status == 200 and response.content_type == "text/plain":
                    parser.parse(self._text(response).splitlines())
                else:
                    if response.status in (429, 503):
                        self._busy_until = self.clock() + 60
                    raise ApiError(503, "source_policy_unavailable", "The source crawl policy could not be verified.")
            except ApiError as error:
                raise ApiError(
                    503,
                    "source_policy_unavailable",
                    "The source crawl policy could not be verified; no catalogue request was made.",
                    {"Retry-After": "60"},
                ) from error
            self._robots = parser
            self._robots_expiry = self.clock() + self.robots_ttl
        if not self._robots.can_fetch(USER_AGENT, url):
            raise ApiError(503, "source_disallowed", "The source robots policy disallows this resource.")
        crawl_delay = self._robots.crawl_delay(USER_AGENT) or 0
        request_rate = self._robots.request_rate(USER_AGENT)
        interval = max(self.min_interval, float(crawl_delay))
        if request_rate and request_rate.requests > 0:
            interval = max(interval, request_rate.seconds / request_rate.requests)
        # Apply newly published crawl delays to the very next request too.
        self._effective_interval = interval

    def get(self, url: str) -> str:
        self.validate_url(url)
        with self._lock:
            # Check policy before serving even cached content when the policy expires.
            self._check_robots(url)
            cached = self._cache.get(url)
            if cached and self.clock() < cached[0]:
                self._cache.move_to_end(url)
                return cached[1]
            if cached:
                del self._cache[url]
            response = self._fetch(url)
            if response.status == 404:
                raise ApiError(404, "not_found", "The requested source page does not exist.")
            if response.status in (429, 503):
                self._busy_until = self.clock() + 60
                raise ApiError(503, "source_busy", "The source is busy; try again later.", {"Retry-After": "60"})
            if response.status in (401, 403):
                raise ApiError(502, "source_access_denied", "The source refused access; no bypass was attempted.")
            if response.status != 200:
                raise ApiError(502, "source_unavailable", "The source returned an unexpected HTTP status.")
            if response.content_type not in ("text/html", "application/xhtml+xml"):
                raise ApiError(502, "source_content_changed", "The source did not return a public HTML page.")
            text = self._text(response)
            if self.cache_ttl:
                self._cache[url] = (self.clock() + self.cache_ttl, text)
                while len(self._cache) > self.cache_size:
                    self._cache.popitem(last=False)
            return text