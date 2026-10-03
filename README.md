# Public Books API

Read-only JSON API for [Books to Scrape](https://books.toscrape.com/). Requires Python 3.9+; no packages or API keys needed.

## Run

From the project directory:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m books_api
```

Open http://127.0.0.1:8000. Stop the server with `Ctrl+C`.

To use a different port:

```sh
python -m books_api --port 8080
```

## Requests

Run these in a second terminal while the server is running:

```sh
curl -s http://127.0.0.1:8000/health
curl -s http://127.0.0.1:8000/api/v1/books
curl -s http://127.0.0.1:8000/api/v1/categories
curl -s 'http://127.0.0.1:8000/api/v1/books?page=2'
curl -s 'http://127.0.0.1:8000/api/v1/books?q=light&rating=3'
curl -s 'http://127.0.0.1:8000/api/v1/books?category=poetry_23&min_price=10&max_price=60'
curl -s http://127.0.0.1:8000/api/v1/books/a-light-in-the-attic_1000
```

Catalogue requests need internet access. Filters apply to the selected page, not the entire catalogue.
