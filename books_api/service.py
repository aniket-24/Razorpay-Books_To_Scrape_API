"""Catalogue operations and validation, independent of the HTTP server."""

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional
from urllib.parse import urlencode

from .errors import ApiError, invalid_query
from .parsers import IDENTIFIER, parse_categories, parse_detail, parse_listing
from .source import ORIGIN, SourceClient

BOOKS_PATH = "/api/v1/books"
ALLOWED_FILTERS = {"page", "category", "q", "min_price", "max_price", "rating"}


def validate_identifier(value: str, label: str) -> None:
    if len(value) > 512 or not IDENTIFIER.fullmatch(value):
        raise invalid_query("{} must be a source slug and numeric suffix, for example poetry_23.".format(label))


def money_filter(value: Optional[str], label: str) -> Optional[Decimal]:
    if value is None:
        return None
    if not re.fullmatch(r"[0-9]{1,6}(?:\.[0-9]{1,2})?", value):
        raise invalid_query("{} must be a non-negative amount with at most two decimal places.".format(label))
    return Decimal(value)


@dataclass(frozen=True)
class BookQuery:
    page: int
    category: Optional[str]
    title_query: Optional[str]
    min_price: Optional[Decimal]
    max_price: Optional[Decimal]
    rating: Optional[int]

    @classmethod
    def from_parameters(cls, parameters: dict):
        unknown = set(parameters) - ALLOWED_FILTERS
        if unknown:
            raise invalid_query("Unknown query parameter: {}.".format(sorted(unknown)[0]))
        page = parameters.get("page", "1")
        if not re.fullmatch(r"[1-9][0-9]{0,3}", page) or int(page) > 1000:
            raise invalid_query("page must be an integer between 1 and 1000.")
        category = parameters.get("category")
        if category is not None:
            validate_identifier(category, "category")
        title_query = parameters.get("q")
        if title_query is not None:
            title_query = title_query.strip()
            if not title_query or len(title_query) > 200:
                raise invalid_query("q must contain between 1 and 200 characters.")
        rating = parameters.get("rating")
        if rating is not None and rating not in ("1", "2", "3", "4", "5"):
            raise invalid_query("rating must be an integer between 1 and 5.")
        minimum = money_filter(parameters.get("min_price"), "min_price")
        maximum = money_filter(parameters.get("max_price"), "max_price")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise invalid_query("min_price must not exceed max_price.")
        return cls(int(page), category, title_query, minimum, maximum, int(rating) if rating else None)

    def matches(self, book: dict) -> bool:
        amount = Decimal(book["price"]["amount"])
        return (
            (self.title_query is None or self.title_query.casefold() in book["title"].casefold())
            and (self.min_price is None or amount >= self.min_price)
            and (self.max_price is None or amount <= self.max_price)
            and (self.rating is None or book["rating"] == self.rating)
        )


class CatalogueService:
    def __init__(self, source: Optional[SourceClient] = None) -> None:
        self.source = source if source is not None else SourceClient()

    def categories(self) -> dict:
        source_url = ORIGIN + "/"
        categories = parse_categories(self.source.get(source_url), source_url)
        return {"data": categories, "meta": {"count": len(categories), "source_url": source_url}}

    def books(self, parameters: dict) -> dict:
        query = BookQuery.from_parameters(parameters)
        if query.category is not None:
            category = next(
                (item for item in self.categories()["data"] if item["id"] == query.category),
                None,
            )
            if category is None:
                raise ApiError(404, "category_not_found", "The requested category does not exist.")
            source_url = category["source_url"]
            if query.page != 1:
                source_url = source_url.rsplit("/", 1)[0] + "/page-{}.html".format(query.page)
        else:
            source_url = ORIGIN + ("/" if query.page == 1 else "/catalogue/page-{}.html".format(query.page))
        listing = parse_listing(self.source.get(source_url), source_url, query.page)
        books = [book for book in listing["books"] if query.matches(book)]

        def page_link(page_number: int) -> str:
            next_parameters = dict(parameters)
            next_parameters["page"] = str(page_number)
            return BOOKS_PATH + "?" + urlencode(next_parameters)

        return {
            "data": books,
            "meta": {
                "page": query.page,
                "total_pages": listing["total_pages"],
                "total_on_source": listing["total_on_source"],
                "source_page_count": len(listing["books"]),
                "returned_count": len(books),
                "filter_scope": "source_page",
                "filters": {name: value for name, value in parameters.items() if name != "page"},
                "source_url": source_url,
            },
            "links": {
                "previous": page_link(query.page - 1) if query.page > 1 else None,
                "next": page_link(query.page + 1) if query.page < listing["total_pages"] else None,
            },
        }

    def book(self, book_id: str) -> dict:
        validate_identifier(book_id, "book_id")
        source_url = ORIGIN + "/catalogue/{}/index.html".format(book_id)
        return {"data": parse_detail(self.source.get(source_url), source_url)}