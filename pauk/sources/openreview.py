from .base import HttpClient


class OpenReviewClient(HttpClient):
    def __init__(self, timeout: int, username: str, password: str) -> None:
        super().__init__(timeout)
        self.username, self.password, self.token = username, password, None

    def _login(self) -> None:
        if self.token or not (self.username and self.password):
            return
        data = self.session.post("https://api.openreview.net/login", json={"id": self.username, "password": self.password}, timeout=self.timeout)
        data.raise_for_status()
        self.token = data.json()["token"]
        self.session.headers["Authorization"] = f"Bearer {self.token}"

    def search(self, term: str) -> dict:
        self._login()
        return self.get_json("https://api.openreview.net/profiles/search", params={"term": term})
