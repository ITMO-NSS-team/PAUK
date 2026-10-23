# `author_names`: local-language and English name parts

**What this covers:** the LLM response contract, guards, retries and the repair
of invalid names from earlier runs.

**Files involved:** `pauk/pipeline/stages/author_names.py`,
`pauk/sources/llm.py`, `scripts/plan_author_names_repair.py`.

The stage handles both member staff and external coauthors. The staff catalog
provides candidates and the academic degree, and the LLM splits `name_raw` and
the name variants into surname, first name and, when known, second name
(patronymic), in two languages: the university's own (`*_local`) and English.
What follows describes ITMO, where the local language is Russian; UFJF's
differences are in the next section.

## A Latin-script profile without patronymics (UFJF)

The profile (`Institution.local_script`, `has_patronymic`, `staff_catalog`,
[../institutions.md](../institutions.md)) changes four things:

- **No staff catalog.** UFJF has none, so the stage runs without the file and
  `matched_candidate` stays null. `PAUK_STAFF_CATALOG_FILE` can still point at
  one.
- **Its own prompt** (`LATIN_NAME_SPLIT_PROMPT`): the split Brazilian citation
  (ABNT) uses. The surname is the last family name, joined with a generational
  suffix ("Souza Filho"); everything before it, particles included, is the
  first name: "Felipe de Paula Alves Benedicto" is "Benedicto" / "Felipe de
  Paula Alves", which is how papers cite the person and how the map labels
  them ("Benedicto F."). Diacritics are kept when a source spelling has them
  and never added. Checked on 25 real UFJF names with
  `anthropic/claude-haiku-4.5`: all 25 passed the contract.
- **No patronymic.** Any `second_name_*` the model returns is a given name in
  the wrong slot and is folded into `first_name_*` before anything else runs
  ("Marcio E. C. Rodrigues" is "Rodrigues" / "Marcio E C").
- **Particles stay with the given names.** A surname the model starts with
  "da", "de", "do", "das" or "dos" (`Institution.surname_particles`) gets the
  particle moved to the end of the first name: "de Oliveira" / "Luiz Fernando
  Cappa" becomes "Oliveira" / "Luiz Fernando Cappa de". On 923 real names one
  reply in twenty did this despite the prompt, and the same family name ended
  up spelled two ways.
- **No `matched_candidate`.** The prompt neither offers candidates nor asks for
  the field, and whatever the model puts there anyway is ignored: rejecting it
  cost a retry on 40% of the first real run.
- **Latin script.** The `*_local` fields must be Latin letters (diacritics
  count as Latin); there is no transliteration guard, and a failed call keeps
  `name_raw` as `name_local` instead of `to_cyrillic`.

## Completion condition

`author_names=completed` means `surname_local`, `first_name_local`, `surname_en` and
`first_name_en` are non-empty strings. The required `*_local` fields must consist
of letters of the profile's script (Cyrillic for ITMO, Latin for UFJF) and the
English ones of Latin letters; hyphens, spaces and
apostrophes are allowed. `second_name_local` and `second_name_en` may be empty.

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
composite `name_local`/`name_en`; name components are never guessed from word
order.

## Model

`PAUK_LLM_MODEL`, `anthropic/claude-haiku-5.5` by default since 2026-10-21
(10x cheaper than haiku-4.5). Compared on 60 Portuguese and 60 Russian real
names: on Portuguese names haiku-5.5 did as well or better (it expands an
initial when a variant spells the name out); on Russian names it was worse,
putting patronymic initials into the first name ("М Ю" for "М" + "Ю"), adding
stress marks ("Каво́кина"), garbling a surname ("Пальедино") and leaving 3 of 60
replies empty. Kept for every university by decision while prototyping; for a
Russian-language graph that matters, set `PAUK_LLM_MODEL=anthropic/claude-haiku-4.5`.

A reply that answers in a fenced JSON block, reconsiders in prose and answers
again in a second block is read from the last block that parses
(`pauk/sources/llm.py::_parse_json_body`): before that, all 8 failures of the
first UFJF run were such replies.

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
