# `author_names`: Russian and English name parts

**What this covers:** the LLM response contract, guards, retries and the repair
of invalid names from earlier runs.

**Files involved:** `pauk/pipeline/stages/author_names.py`,
`pauk/sources/llm.py`, `scripts/plan_author_names_repair.py`.

The stage handles both ITMO staff and external coauthors. The staff catalog
provides candidates and the academic degree, and the LLM splits `name_raw` and
the name variants into surname, first name and, when known, second name
(patronymic), in two languages.

## Completion condition

`author_names=completed` means `surname_ru`, `first_name_ru`, `surname_en` and
`first_name_en` are non-empty strings. The required Russian fields must consist
of Cyrillic letters and the English ones of Latin letters; hyphens, spaces and
apostrophes are allowed. `second_name_ru` and `second_name_en` may be empty.

The check belongs to the stage: the shared client validates the JSON transport,
while each call site has its own response schema. Before checking the required
fields, the stage normalizes strings and validates `matched_candidate`, then
runs guards for an invented or misclassified second name and for broken
transliteration. The final check comes after the guards, because a guard can
itself declare a required field unreliable.

An incomplete semantic response gets one retry with the exact reason for the
rejection. If the retry is also incomplete, the row is marked `failed` and the
partial response is not copied into `Person`. A full name already stored is not
erased by a forced run. For a new row only a safe fallback remains, the
composite `name_ru`/`name_en`; name components are never guessed from word
order.

`OpenRouterClient` separately retries a network error, `429` and
`500`/`502`/`503`/`504`: at most three transport attempts with exponential
backoff, honoring `Retry-After`. Other `4xx` errors are not retried. Every
semantic attempt is logged to `llm_logs_author_names` with its own
`context.response_attempt`; a transport retry stays inside one such call.

## Repairing invalid `completed` rows

Build a plan without writing to Mongo:

```bash
uv run python scripts/plan_author_names_repair.py \
  --out data/reports/author-names-repair
```

The script looks for `_processing.author_names.status=completed` rows that
violate the same contract: a required field is missing, `null`, empty,
whitespace, not a string, has no letters, or uses the wrong alphabet. `Person`
is global and can belong to several groups, so each id is assigned to exactly
one group; a greedy cover minimizes the number of runs. The manifest keeps the
original groups, the field violations and the exact commands:

```bash
uv run pauk enrich author_names --group <group> \
  --input <group>.txt --entity persons --force
```

Run `snapshot_mongo.py` before executing the commands. Run them sequentially:
separate `pauk enrich` invocations take no cross-group lock, and two groups can
reference the same global `Person`.

After the repair, rerunning the planner should report zero. Then publish the
affected groups with `pauk publish graph --group <group>` and rebuild the
cache and web data; fixing Mongo does not update Neo4j by itself.
