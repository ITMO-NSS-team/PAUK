# `pauk/reports/`: OSA reports on repositories

**What it covers:** how the PDF behind the "Report" button of a repository
card is produced and how the site finds it.

**Files:** `pauk/reports/builder.py` (`ReportsBuilder`: which repositories,
in parallel, the index), `pauk/reports/osa.py` (`OsaRunner`: one OSA run),
`pauk/cli.py::_cmd_reports`, the report row in
`pauk/gui/web/src/features/panels.ts`.

[OSA](https://github.com/aimclub/OSA) reads a GitHub repository and writes a
PDF report on it with an LLM. The lab server can't reach an LLM, so the step
runs locally, outside the lab VPN, between `pauk cache export` and
`./scripts/deploy.sh`. It reads the same snapshot as `pauk gui build` and
does not depend on it: the two can run in any order.

```
pauk reports build              # every repository without a report
pauk reports build github_a_b   # just these ids; --force regenerates
```

## One run per repository and language

OSA runs through `uvx --from osa_tool==<pinned>`, in its own cached
environment: none of its ~270 dependencies enter the project's. Flags are all
explicit (`OsaRunner.command`): `--mode advanced --report` builds the report only,
since the basic mode also writes a README, docs and docstrings into the
clone, LLM calls whose output is deleted with it; `--api openai` keeps
OpenRouter even in later OSA versions that default to a subscription CLI.
The key goes to OSA as `LLM_SERVICE_KEY`, the name `protollm` reads for
OpenRouter.

Git LFS is switched off for OSA's git through `GIT_CONFIG_*` env
(`NO_LFS_GIT_CONFIG`): the report needs the tree, not the large files, and a
gitconfig that names the LFS filter without `git-lfs` installed fails the
checkout. OSA then retries the clone into the same, now non-empty directory,
so such a repository would fail every run.

Each run works in `data/osa/tmp/<lang>/<id>/` and logs to
`data/osa/logs/<lang>/<id>.log`. A report counts only when OSA exits 0,
leaves exactly one `*_report.pdf` that starts with `%PDF-`, and its log shows
no degraded run (`DEGRADED_RUN_MARKERS`: a switch to one of OSA's fallback
models, or an unparsable reply replaced by an "Unknown" report); it is then moved
into place with `os.replace`, so the site never sees half a file. The work
summary OSA also writes is dropped. A run past `RUN_TIMEOUT` (15 minutes; the
slowest of the first 399 real runs took 13, the median 25 seconds) is stopped
with SIGTERM, which `uvx` forwards to OSA.

The file on disk is the only state: an existing PDF is skipped, a failed one
is retried by the next run. `reports/index.json` (`{"en": [ids], "ru": [ids]}`, the
site's `contracts/graph.ts::ReportsIndex`) is
rebuilt from the disk at the end of every run, failed or interrupted.
`WORKERS` (8) runs go in parallel; the clones, not the LLM, set the pace.

Before the first OSA run the step reads the key's spend from OpenRouter:
without a working key OSA does not fail but retries through its fallback
models for minutes, and the lab VPN looks the same. The summary reports the
dollars the run cost, a lower bound: OpenRouter books the last calls a few
seconds late.

## Languages

`LANGUAGES` maps each site language to OSA's `--artefacts-language`
(`en` -> `English`, `ru` -> `Russian`, the `language` of OSA's locale files),
so every repository gets one report per language and run. The site shows
the report of the interface language only: a repository without one gets no
button rather than a report in a language the reader didn't pick.
