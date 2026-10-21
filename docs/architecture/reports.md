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

## Settings

`pauk/reports/settings.py::ReportsSettings`, apart from the project-wide
`Settings`: the pinned OSA version, the model and its context window, the
languages, parallel runs, timeouts, degraded-run markers, the LFS and proxy
bypass config, and `out_dir` from `PAUK_REPORTS_DIR` (relative to the repository root; unset
means `data/gui/private/reports`). Where the PDFs live elsewhere, link that
folder as `data/gui/private/reports`, so the site build and `deploy.sh` find it.

## Which repositories

Every repository of the snapshot except the third-party ones
(`third_party_repos`): no `IMPLEMENTS`, every `MENTIONS_LINK` judged
`is_relevant=false`, and no ITMO contributor (`CONTRIBUTED_TO`). A report on
those rates code nobody here wrote. A mention the classifier left undecided
(`null`) or an ITMO contributor keeps the repository, since the classifier can
miss the authors' own code. In the 14.10 snapshot that leaves out 119 of 400.
Reports that already exist are not deleted and stay linked; ids named on the
command line are generated whatever they are.

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
(`ReportsSettings.no_lfs_git_config`): the report needs the tree, not the large files, and a
gitconfig that names the LFS filter without `git-lfs` installed fails the
checkout. OSA then retries the clone into the same, now non-empty directory,
so such a repository would fail every run.

Where OpenRouter is blocked (the lab server answers 403), `OPENROUTER_PROXY_URL`
goes to OSA as `HTTPS_PROXY`/`HTTP_PROXY` and to the spend check; GitHub stays
off the proxy (`proxy_bypass`), so clones of several gigabytes go direct.

Each run works in `data/osa/tmp/<lang>/<id>/` and logs to
`data/osa/logs/<lang>/<id>.log`. A report counts only when OSA exits 0,
leaves exactly one `*_report.pdf` that starts with `%PDF-`, and its log shows
no degraded run (`degraded_run_markers`: a switch to one of OSA's fallback
models, or an unparsable reply replaced by an "Unknown" report); it is then moved
into place with `os.replace`, so the site never sees half a file. The work
summary OSA also writes is dropped. A run past `run_timeout` (15 minutes; the
slowest of the first 399 real runs took 13, the median 25 seconds) is logged
at once and stopped: each run has its own process group, which gets SIGTERM
and, if anything of it is still alive after `stop_grace`, SIGKILL, so the git
and Python children of uvx never outlive it. An interrupted build (Ctrl+C)
stops every run in flight the same way (`OsaRunner.stop`) and drops the
queued ones; a run that is stopped never moves its PDF into place.

The file on disk is the only state: an existing PDF is skipped, a failed one
is retried by the next run. `reports/index.json` (`{"en": [ids], "ru": [ids]}`, the
site's `contracts/graph.ts::ReportsIndex`) is
rebuilt from the disk at the end of every run, failed or interrupted.
`workers` (8) runs go in parallel; the clones, not the LLM, set the pace.

When every report already exists the step only rebuilds the index, with no
key, uvx or network. Otherwise, before the first OSA run, it reads the key's
spend from OpenRouter:
without a working key OSA does not fail but retries through its fallback
models for minutes, and the lab VPN looks the same. The summary reports the
dollars the run cost, a lower bound: OpenRouter books the last calls a few
seconds late.

## Languages

`languages` maps each site language to OSA's `--artefacts-language`
(`en` -> `English`, `ru` -> `Russian`, the `language` of OSA's locale files),
so every repository gets one report per language and run. The site shows
the report of the interface language only: a repository without one gets no
button rather than a report in a language the reader didn't pick.
