from .base import HttpClient
from urllib.parse import quote


class CrossrefClient(HttpClient):
    def __init__(self, timeout: int) -> None:
        super().__init__(timeout, {"User-Agent": "PAUK/2.0"})

    def get_work(self, doi: str) -> dict:
        clean = doi.removeprefix("https://doi.org/").removeprefix("http://dx.doi.org/")
        return self.get_json(f"https://api.crossref.org/works/{quote(clean, safe='/')}")
