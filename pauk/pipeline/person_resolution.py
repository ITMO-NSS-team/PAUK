"""OpenRouter execution for the duplicate-person resolution cascade.

The inference contract lives in :mod:`pauk.graph.person_resolution`.  This
module is the operational adapter: it runs both model stages concurrently,
validates every response, records the calls and reuses successful answers
for identical evidence on later pipeline runs.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from pymongo.database import Database

from pauk.graph.person_resolution import (
    FIRST_STAGE_SYSTEM_PROMPT,
    SECOND_STAGE_SYSTEM_PROMPT,
    ModelVerdict,
    PairEvidence,
    SecondStageContext,
    first_stage_payload,
    parse_first_stage_response,
    parse_second_stage_response,
)
from pauk.settings import Settings
from pauk.sources import OpenRouterClient
from pauk.storage import LlmLogStore

logger = logging.getLogger(__name__)

CACHE_COLLECTION = "llm_person_resolution_cache"
LOG_COLLECTION = "llm_logs_person_resolution"


@dataclass(frozen=True)
class _Request:
    pair_id: int
    stage: str
    system_prompt: str
    payload: dict
    max_tokens: int
    parser: Callable[[dict, int], ModelVerdict]


@dataclass(frozen=True)
class _Result:
    request: _Request
    verdict: ModelVerdict | None
    raw_response: dict | None
    parsed: dict | None
    usage: dict | None
    error: str | None
    cache_hit: bool


class OpenRouterResolutionModels:
    """Runs the two Qwen stages with bounded concurrency and a Mongo cache."""

    def __init__(self, config: Settings, db: Database, group: str) -> None:
        self.config = config
        self.db = db
        self.group = group
        self.model = config.person_resolution_model
        self.workers = max(1, config.person_resolution_concurrency)
        self._local = threading.local()
        self._cache = db[CACHE_COLLECTION]
        self._log = LlmLogStore(db, LOG_COLLECTION)

    def first_many(self, items: list[tuple[int, PairEvidence]]) -> dict[int, ModelVerdict | None]:
        requests = [
            _Request(
                pair_id=pair_id,
                stage="qwen_first",
                system_prompt=FIRST_STAGE_SYSTEM_PROMPT,
                payload=first_stage_payload(pair_id, evidence),
                max_tokens=160,
                parser=parse_first_stage_response,
            )
            for pair_id, evidence in items
        ]
        return self._run(requests)

    def second_many(self, contexts: list[SecondStageContext]) -> dict[int, ModelVerdict | None]:
        requests = [
            _Request(
                pair_id=context.pair_id,
                stage="qwen_second",
                system_prompt=SECOND_STAGE_SYSTEM_PROMPT,
                payload=context.as_payload(),
                max_tokens=220,
                parser=parse_second_stage_response,
            )
            for context in contexts
        ]
        return self._run(requests)

    def _client(self) -> OpenRouterClient:
        client = getattr(self._local, "client", None)
        if client is None:
            client = OpenRouterClient(
                self.config.request_timeout,
                self.config.openrouter_api_key,
                self.model,
                self.config.openrouter_proxy_url,
            )
            self._local.client = client
        return client

    def _fingerprint(self, request: _Request) -> str:
        raw = json.dumps(
            {
                "model": self.model,
                "stage": request.stage,
                "system": request.system_prompt,
                "payload": request.payload,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _invoke(self, request: _Request) -> _Result:
        fingerprint = self._fingerprint(request)
        cached = self._cache.find_one({"_id": fingerprint, "model": self.model})
        if cached and isinstance(cached.get("parsed"), dict):
            try:
                verdict = request.parser(cached["parsed"], request.pair_id)
            except ValueError:
                verdict = None
            else:
                return _Result(
                    request,
                    verdict,
                    cached.get("raw_response"),
                    cached["parsed"],
                    cached.get("usage"),
                    None,
                    True,
                )

        if not self.config.openrouter_api_key:
            return _Result(
                request,
                None,
                None,
                None,
                None,
                "OPENROUTER_API_KEY not set",
                False,
            )

        client = self._client()
        parsed = None
        error = None
        verdict = None
        for _attempt in range(2):
            parsed = client.chat_json(
                json.dumps(request.payload, ensure_ascii=False, separators=(",", ":")),
                system_prompt=request.system_prompt,
                max_tokens=request.max_tokens,
            )
            if parsed is None:
                error = client.last_error or "no response"
                continue
            try:
                verdict = request.parser(parsed, request.pair_id)
            except ValueError as exc:
                error = str(exc)
                continue
            error = None
            break

        result = _Result(
            request,
            verdict,
            client.last_response,
            parsed,
            client.last_usage,
            error,
            False,
        )
        if verdict is not None:
            self._cache.replace_one(
                {"_id": fingerprint},
                {
                    "_id": fingerprint,
                    "model": self.model,
                    "stage": request.stage,
                    "parsed": parsed,
                    "raw_response": client.last_response,
                    "usage": client.last_usage,
                },
                upsert=True,
            )
        return result

    def _run(self, requests: list[_Request]) -> dict[int, ModelVerdict | None]:
        if not requests:
            return {}
        results: list[_Result] = []
        if not self.config.openrouter_api_key:
            results = [self._invoke(request) for request in requests]
        else:
            with ThreadPoolExecutor(max_workers=min(self.workers, len(requests))) as pool:
                futures = [pool.submit(self._invoke, request) for request in requests]
                for number, future in enumerate(as_completed(futures), 1):
                    results.append(future.result())
                    if number % 50 == 0 or number == len(futures):
                        logger.info(
                            "person resolution %s: %d/%d",
                            requests[0].stage,
                            number,
                            len(futures),
                        )

        for result in results:
            self._log.record(
                group=self.group,
                model=self.model,
                prompt=json.dumps(
                    {
                        "system": result.request.system_prompt,
                        "input": result.request.payload,
                    },
                    ensure_ascii=False,
                ),
                raw_response=result.raw_response,
                parsed=result.parsed,
                usage=result.usage,
                error=result.error,
                context={
                    "pair_id": result.request.pair_id,
                    "stage": result.request.stage,
                    "cache_hit": result.cache_hit,
                },
            )
        return {result.request.pair_id: result.verdict for result in results}
