"""Small HTML parser for the sandbox's listings and public product information."""

import re
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urljoin, urlsplit

from .errors import ApiError, layout_changed
from .source import ORIGIN, SourceClient

IDENTIFIER = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*_[1-9][0-9]*\Z")
BOOK_PATH = re.compile(r"/catalogue/([^/]+)/index\.html\Z")
CATEGORY_PATH = re.compile(r"/catalogue/category/books/([^/]+)/index\.html\Z")
RATINGS = {"One": 1, "Two": 2, "Three": 3, "Four": 4, "Five": 5}
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


class Node:
    def __init__(self, tag: str, attributes=None, parent=None) -> None:
        self.tag = tag
        self.attributes = dict(attributes or [])
        self.parent = parent
        self.children = []

    def has_class(self, name: str) -> bool:
        return name in (self.attributes.get("class") or "").split()

    def find_all(self, tag=None, class_name=None, identifier=None):
        for child in self.children:
            if not isinstance(child, Node):
                continue
            if (
                (tag is None or child.tag == tag)
                and (class_name is None or child.has_class(class_name))
                and (identifier is None or child.attributes.get("id") == identifier)
            ):
                yield child
            yield from child.find_all(tag, class_name, identifier)

    def find(self, tag=None, class_name=None, identifier=None):
        return next(self.find_all(tag, class_name, identifier), None)

    def text(self) -> str:
        def fragments(node):
            for child in node.children:
                if isinstance(child, Node):
                    yield from fragments(child)
                else:
                    yield child

        return " ".join("".join(fragments(self)).split())


class DocumentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("document")
        self.stack = [self.root]

    def handle_starttag(self, tag, attributes):
        node = Node(tag, attributes, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attributes):
        self.handle_starttag(tag, attributes)
        if tag not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def document(html: str) -> Node:
    parser = DocumentParser()
    parser.feed(html)
    parser.close()
    return parser.root


def required(node: Optional[Node]) -> Node:
    if node is None:
        raise layout_changed()
    return node


def nonempty_text(node: Optional[Node]) -> str:
    text = required(node).text()
    if not text:
        raise layout_changed()
    return text


def public_link(page_url: str, href: Optional[str], image: bool = False) -> str:
    if not href:
        raise layout_changed()
    url = urljoin(page_url, href)
    if image:
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.netloc != "books.toscrape.com" or not parts.path.startswith("/media/") or parts.query or parts.fragment:
            raise ApiError(502, "unsafe_source_link", "An unexpected image URL was rejected.")
    else:
        SourceClient.validate_url(url)
    return url


def identifier_from_url(url: str, pattern) -> str:
    match = pattern.fullmatch(urlsplit(url).path)
    if not match or not IDENTIFIER.fullmatch(match.group(1)):
        raise layout_changed()
    return match.group(1)


def parse_categories(html: str, page_url: str) -> list:
    sidebar = required(document(html).find(class_name="side_categories"))
    categories = []
    seen = set()
    for anchor in sidebar.find_all("a"):
        url = public_link(page_url, anchor.attributes.get("href"))
        if urlsplit(url).path == "/catalogue/category/books_1/index.html":
            continue  # The outer "Books" link is not a category.
        category_id = identifier_from_url(url, CATEGORY_PATH)
        if category_id not in seen:
            categories.append({"id": category_id, "name": nonempty_text(anchor), "source_url": url})
            seen.add(category_id)
    if not categories:
        raise layout_changed()
    return categories


def price(node: Optional[Node]) -> dict:
    match = re.fullmatch(r"£([0-9]+\.[0-9]{2})", nonempty_text(node))
    if not match:
        raise layout_changed()
    # Money stays a decimal string: JSON floats would lose exact penny precision.
    return {"amount": match.group(1), "currency": "GBP"}


def rating(node: Node) -> int:
    classes = required(node.find(class_name="star-rating")).attributes.get("class", "").split()
    values = [RATINGS[name] for name in classes if name in RATINGS]
    if len(values) != 1:
        raise layout_changed()
    return values[0]


def parse_listing(html: str, page_url: str, requested_page: int) -> dict:
    root = document(html)
    cards = list(root.find_all("article", "product_pod"))
    if not cards or len(cards) > 20:
        raise layout_changed()
    books = []
    seen = set()
    for card in cards:
        anchor = required(required(card.find("h3")).find("a"))
        title = anchor.attributes.get("title")
        if not title:
            raise layout_changed()  # The visible link text is truncated on this website.
        source_url = public_link(page_url, anchor.attributes.get("href"))
        book_id = identifier_from_url(source_url, BOOK_PATH)
        if book_id in seen:
            raise layout_changed()
        seen.add(book_id)
        image = required(card.find("img"))
        books.append({
            "id": book_id,
            "title": title,
            "price": price(card.find(class_name="price_color")),
            "rating": rating(card),
            "availability": nonempty_text(card.find(class_name="availability")),
            "image_url": public_link(page_url, image.attributes.get("src"), image=True),
            "source_url": source_url,
        })
    results = nonempty_text(root.find("form", "form-horizontal"))
    count_match = re.search(r"\b([0-9]+) results?\b", results)
    if not count_match:
        raise layout_changed()
    total = int(count_match.group(1))
    current = root.find("li", "current")
    total_pages = 1
    if current is not None:
        match = re.fullmatch(r"Page ([0-9]+) of ([0-9]+)", current.text())
        if not match or int(match.group(1)) != requested_page:
            raise layout_changed()
        total_pages = int(match.group(2))
    if requested_page > total_pages or total < len(books) or total_pages < 1:
        raise layout_changed()
    return {"books": books, "total_on_source": total, "total_pages": total_pages}


def parse_detail(html: str, page_url: str) -> dict:
    root = document(html)
    main = required(root.find(class_name="product_main"))
    table = required(root.find("table", "table-striped"))
    information = {}
    for row in table.find_all("tr"):
        information[nonempty_text(row.find("th"))] = nonempty_text(row.find("td"))
    fields = {"UPC", "Product Type", "Price (excl. tax)", "Price (incl. tax)", "Tax", "Availability", "Number of reviews"}
    if not fields.issubset(information) or not information["Number of reviews"].isdigit():
        raise layout_changed()
    breadcrumb = required(root.find("ul", "breadcrumb"))
    category = None
    for anchor in breadcrumb.find_all("a"):
        href = anchor.attributes.get("href")
        candidate = urljoin(page_url, href or "")
        if CATEGORY_PATH.fullmatch(urlsplit(candidate).path):
            url = public_link(page_url, href)
            category = {
                "id": identifier_from_url(url, CATEGORY_PATH),
                "name": nonempty_text(anchor),
                "source_url": url,
            }
    if category is None:
        raise layout_changed()
    description = None
    heading = root.find(identifier="product_description")
    if heading is not None:
        siblings = heading.parent.children
        for sibling in siblings[siblings.index(heading) + 1:]:
            if isinstance(sibling, Node):
                if sibling.tag != "p":
                    raise layout_changed()
                description = nonempty_text(sibling)
                break
        if description is None:
            raise layout_changed()
    stock_match = re.search(r"\(([0-9]+) available\)", information["Availability"])
    image = required(required(root.find(identifier="product_gallery")).find("img"))
    return {
        "id": identifier_from_url(page_url, BOOK_PATH),
        "title": nonempty_text(main.find("h1")),
        "price": price(main.find(class_name="price_color")),
        "rating": rating(main),
        "availability": information["Availability"],
        "stock": int(stock_match.group(1)) if stock_match else None,
        "category": category,
        "description": description,
        "upc": information["UPC"],
        "product_type": information["Product Type"],
        "reviews_count": int(information["Number of reviews"]),
        "product_information": information,
        "image_url": public_link(page_url, image.attributes.get("src"), image=True),
        "source_url": page_url,
    }