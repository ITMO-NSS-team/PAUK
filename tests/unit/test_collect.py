import unittest

import mongomock

from pauk.pipeline.collect import AUTHORSHIP_TRUNCATION_LIMIT, Collector
from pauk.pipeline.selectors import PeriodSelector
from pauk.storage import RawStore


def work(work_id, authors, truncated=False):
    payload = {
        "id": f"https://openalex.org/{work_id}",
        "title": f"Work {work_id}",
        "authorships": [
            {"author": {"id": f"https://openalex.org/A{i}", "display_name": f"Author {i}"}}
            for i in range(authors)
        ],
    }
    if truncated:
        payload["is_authors_truncated"] = True
    return payload


class FakeOpenAlexClient:
    def __init__(self, listed=(), full=()):
        self.listed = list(listed)
        self.full = {w["id"].rsplit("/", 1)[-1]: w for w in full}
        self.single_fetches = []

    def iter_works(self, ror_id, date_from, date_to):
        yield from self.listed

    def get_work(self, work_id):
        normalized = work_id.rstrip("/").split("/")[-1].upper()
        self.single_fetches.append(normalized)
        return self.full[normalized]


class CollectTruncatedAuthorsTest(unittest.TestCase):
    def setUp(self):
        db = mongomock.MongoClient()["pauk_test"]
        self.raw = RawStore(db, "sample")

    def last_payloads(self):
        rows = {}
        for row in self.raw.read("openalex_works"):
            wid = row["payload"]["id"].rsplit("/", 1)[-1]
            rows[wid] = row["payload"]
        return rows

    def test_truncated_list_result_is_stored_from_the_full_record(self):
        # The list endpoint serves at most 100 authorships and no marker;
        # the single-work endpoint has the complete list.
        client = FakeOpenAlexClient(
            listed=[work("W1", AUTHORSHIP_TRUNCATION_LIMIT), work("W2", 3)],
            full=[work("W1", 587)],
        )
        count = Collector(client, self.raw).collect(PeriodSelector("2026-01-01", "2026-12-31"))
        self.assertEqual(count, 2)
        self.assertEqual(client.single_fetches, ["W1"])
        self.assertEqual(len(self.last_payloads()["W1"]["authorships"]), 587)

    def test_previously_collected_truncated_work_is_repaired(self):
        # A group collected before truncation was handled still carries the
        # 100-author payload; collect re-fetches the full record once.
        self.raw.append("openalex_works", work("W1", 100), {"from": "x", "to": "y"})
        client = FakeOpenAlexClient(listed=[], full=[work("W1", 587)])
        Collector(client, self.raw).collect(PeriodSelector("2026-01-01", "2026-12-31"))
        self.assertEqual(client.single_fetches, ["W1"])
        self.assertEqual(len(self.last_payloads()["W1"]["authorships"]), 587)
        # The repaired envelope is the latest state: no re-fetch next time.
        Collector(client, self.raw).collect(PeriodSelector("2026-01-01", "2026-12-31"))
        self.assertEqual(client.single_fetches, ["W1"])

    def test_explicit_truncation_flag_triggers_the_full_fetch_too(self):
        client = FakeOpenAlexClient(
            listed=[work("W1", 3, truncated=True)],
            full=[work("W1", 7)],
        )
        Collector(client, self.raw).collect(PeriodSelector("2026-01-01", "2026-12-31"))
        self.assertEqual(len(self.last_payloads()["W1"]["authorships"]), 7)

if __name__ == "__main__":
    unittest.main()
