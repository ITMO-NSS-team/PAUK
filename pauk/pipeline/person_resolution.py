"""LLM execution for the duplicate-person resolution cascade.

The inference contract lives in :mod:`pauk.graph.person_resolution`.  This
module is the operational adapter: it runs model requests concurrently,
validates every response, records the calls and reuses successful answers
for identical evidence on later pipeline runs.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from functools import partial
from typing import Any

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
from pauk.graph.person_resolution_single import (
    BATCH_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    SingleVerdict,
    batch_payload,
    full_payload,
    parse_response,
    split_batch_response,
)
from pauk.settings import Settings
from pauk.sources.llm import LlmClient, kill_running_clis, llm_client
from pauk.storage import LlmLogStore

logger = logging.getLogger(__name__)

CACHE_COLLECTION = "llm_person_resolution_cache"
LOG_COLLECTION = "llm_logs_person_resolution"
FIRST_STAGE = "qwen_first"
SECOND_STAGE = "qwen_second"
SINGLE_STAGE = "qwen_single"
BATCH_STAGE = "qwen_single_batch"
Verdict = ModelVerdict | SingleVerdict


@dataclass(frozen=True)
class _Request:
    pair_id: int
    stage: str
    system_prompt: str
    payload: dict
    max_tokens: int
    parser: Callable[[dict, int], Verdict]


@dataclass(frozen=True)
class _Result:
    request: _Request
    verdict: Verdict | None
    raw_response: dict | None
    parsed: dict | None
    usage: dict | None
    error: str | None
    cache_hit: bool
    batch_id: str | None = None


def cache_payload(stage: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """The request as the cache keys it.

    A pair's number is where the answer comes back, not evidence about
    anybody: it is handed out by counting pairs, so one merge upstream
    renumbers the rest and every stored verdict stops matching. The person
    ids inside each side stay: they do say who is being compared.
    """
    if stage == FIRST_STAGE:
        pairs = payload.get("pairs")
        if not isinstance(pairs, list):
            return dict(payload)
        return {**payload, "pairs": [{**pair, "id": 0} for pair in pairs]}
    return {**payload, "id": 0}


def _renumbered(stage: str, parsed: Mapping[str, Any], pair_id: int) -> dict[str, Any]:
    """A stored answer, addressed to the number this run uses."""
    if stage == FIRST_STAGE:
        results = parsed.get("results")
        if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], Mapping):
            return dict(parsed)
        return {**parsed, "results": [{**results[0], "id": pair_id}]}
    return {**parsed, "id": pair_id}


def _batch_request(request: _Request) -> _Request:
    """The request a batch answer is cached under: the same evidence asked with the batch prompt."""
    return replace(request, stage=BATCH_STAGE, system_prompt=BATCH_SYSTEM_PROMPT)


class ResolutionModels:
    """Runs full-context person-resolution requests with bounded concurrency and a Mongo cache."""

    def __init__(self, config: Settings, db: Database, group: str) -> None:
        self.config = config
        self.db = db
        self.group = group
        self.workers = max(1, config.person_resolution_concurrency)
        self.batch_size = max(1, config.person_resolution_batch_size)
        self._local = threading.local()
        client = self._client()
        self.model = client.model
        self._unavailable = client.unavailable_reason()
        self._cache = db[CACHE_COLLECTION]
        self._log = LlmLogStore(db, LOG_COLLECTION)

    def single_many(self, items: list[tuple[SecondStageContext, PairEvidence]]) -> dict[int, SingleVerdict | None]:
        return self._run(
            [
                _Request(
                    context.pair_id, SINGLE_STAGE, SYSTEM_PROMPT, full_payload(context, evidence), 260, parse_response
                )
                for context, evidence in items
            ],
            self.batch_size,
        )

    def first_many(self, items: list[tuple[int, PairEvidence]]) -> dict[int, ModelVerdict | None]:
        requests = [
            _Request(
                pair_id=pair_id,
                stage=FIRST_STAGE,
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
                stage=SECOND_STAGE,
                system_prompt=SECOND_STAGE_SYSTEM_PROMPT,
                payload=context.as_judge_payload(),
                max_tokens=220,
                parser=parse_second_stage_response,
            )
            for context in contexts
        ]
        return self._run(requests)

    def _client(self) -> LlmClient:
        client = getattr(self._local, "client", None)
        if client is None:
            client = llm_client(
                self.config.provider_for("person_resolution"),
                timeout=self.config.request_timeout,
                api_key=self.config.openrouter_api_key,
                openrouter_model=self.config.person_resolution_model,
                proxy_url=self.config.openrouter_proxy_url,
            )
            self._local.client = client
        return client

    def _fingerprint(self, request: _Request) -> str:
        raw = json.dumps(
            {
                "model": self.model,
                "stage": request.stage,
                "system": request.system_prompt,
                "payload": cache_payload(request.stage, request.payload),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _cached(self, request: _Request) -> _Result | None:
        cached = self._cache.find_one({"_id": self._fingerprint(request), "model": self.model})
        if not cached or not isinstance(cached.get("parsed"), dict):
            return None
        parsed = _renumbered(request.stage, cached["parsed"], request.pair_id)
        try:
            verdict = request.parser(parsed, request.pair_id)
        except ValueError:
            return None
        return _Result(request, verdict, cached.get("raw_response"), parsed, cached.get("usage"), None, True)

    def _store(self, request: _Request, parsed: dict, raw_response: Any, usage: dict | None) -> None:
        fingerprint = self._fingerprint(request)
        self._cache.replace_one(
            {"_id": fingerprint},
            {
                "_id": fingerprint,
                "model": self.model,
                "stage": request.stage,
                "parsed": parsed,
                "raw_response": raw_response,
                "usage": usage,
            },
            upsert=True,
        )

    def _invoke(self, request: _Request) -> _Result:
        if cached := self._cached(request):
            return cached

        if self._unavailable:
            return _Result(request, None, None, None, None, self._unavailable, False)

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
            self._store(request, parsed, client.last_response, client.last_usage)
        return result

    def _invoke_one(self, request: _Request) -> list[_Result]:
        return [self._invoke(request)]

    def _ask_batch(self, client: LlmClient, batch: list[_Request], batch_id: str) -> dict | None:
        payload = batch_payload([request.payload for request in batch])
        parsed = client.chat_json(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            system_prompt=BATCH_SYSTEM_PROMPT,
            max_tokens=sum(request.max_tokens for request in batch),
        )
        self._log.record(
            group=self.group,
            model=self.model,
            prompt=json.dumps({"system": BATCH_SYSTEM_PROMPT, "input": payload}, ensure_ascii=False),
            raw_response=client.last_response,
            parsed=parsed,
            usage=client.last_usage,
            error=None if parsed is not None else client.last_error or "no response",
            context={"stage": BATCH_STAGE, "batch_id": batch_id, "pair_ids": [request.pair_id for request in batch]},
        )
        return parsed

    def _invoke_batch(self, batch: list[_Request]) -> list[_Result]:
        """One request for several pairs, tried twice; a pair the reply leaves out or gets wrong is asked alone.

        The batch call and every pair it answered are logged under one batch_id: a pair's own record shows the
        batch prompt it was never sent alone, and no usage.
        """
        client = self._client()
        batch_id = uuid.uuid4().hex
        parsed = None
        for _attempt in range(2):
            if (parsed := self._ask_batch(client, batch, batch_id)) is not None:
                break
        if parsed is None:
            error = client.last_error or "no response"
            return [
                _Result(request, None, client.last_response, None, None, error, False, batch_id) for request in batch
            ]
        answers = split_batch_response(parsed)
        results = []
        for request in batch:
            keyed = _batch_request(request)
            answer = dict(answers[request.pair_id]) if request.pair_id in answers else None
            try:
                verdict = keyed.parser(answer, request.pair_id) if answer is not None else None
            except ValueError:
                verdict = None
            if verdict is None:
                results.append(self._invoke(request))
                continue
            self._store(keyed, answer, None, None)
            results.append(_Result(keyed, verdict, None, answer, None, None, False, batch_id))
        return results

    def _hit(self, request: _Request, batch_size: int) -> _Result | None:
        """A cached single answer, or in batch mode a cached batch answer."""
        return self._cached(request) or (self._cached(_batch_request(request)) if batch_size > 1 else None)

    def _jobs(
        self, requests: list[_Request], batch_size: int
    ) -> tuple[list[_Result], list[Callable[[], list[_Result]]]]:
        """Cache hits, and the calls left to make: one per request, or one per batch of uncached requests."""
        if batch_size == 1:
            return [], [partial(self._invoke_one, request) for request in requests]
        hits, pending = [], []
        for request in requests:
            if cached := self._hit(request, batch_size):
                hits.append(cached)
            else:
                pending.append(request)
        batches = [pending[start : start + batch_size] for start in range(0, len(pending), batch_size)]
        return hits, [partial(self._invoke_batch, batch) for batch in batches]

    def _run(self, requests: list[_Request], batch_size: int = 1) -> dict[int, Verdict | None]:
        if not requests:
            return {}
        results: list[_Result] = []
        if self._unavailable:
            results = [self._hit(request, batch_size) or self._invoke(request) for request in requests]
        else:
            results, jobs = self._jobs(requests, batch_size)
            done = len(results)
            for start in range(0, len(jobs), self.workers * 8):
                block = jobs[start : start + self.workers * 8]
                with ThreadPoolExecutor(max_workers=min(self.workers, len(block))) as pool:
                    futures = [pool.submit(job) for job in block]
                    try:
                        for future in as_completed(futures):
                            answered = future.result()
                            results.extend(answered)
                            done += len(answered)
                            if done % 50 < len(answered) or done == len(requests):
                                logger.info(
                                    "person resolution %s: %d/%d",
                                    requests[0].stage,
                                    done,
                                    len(requests),
                                )
                    except BaseException:
                        # Ctrl-C has to drop what is still queued: leaving it
                        # to the pool's own shutdown would pay for a whole
                        # block of answers nobody is going to read.
                        pool.shutdown(wait=False, cancel_futures=True)
                        kill_running_clis()
                        raise

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
                    **({"batch_id": result.batch_id} if result.batch_id else {}),
                },
            )
        return {result.request.pair_id: result.verdict for result in results}
