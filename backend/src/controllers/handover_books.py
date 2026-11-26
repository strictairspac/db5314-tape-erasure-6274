"""HTTP response helpers for handover book downloads."""

from src.services import handover_books


def download(identity: int, format: str = 'json'):
    return handover_books.book_response(identity, format)


def download_handover_book(identity: int, format: str = 'json'):
    return handover_books.book_response(identity, format)
