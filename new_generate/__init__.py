"""new_generate - generates static data for the site: cache snapshot ->
layout -> JSON. Consumes `pauk.cache`'s dict-shaped `db` (not the old
positional-tuple shape) and outputs plain JSON split into a lightweight
summary for the map and per-entity-type detail files (see
`graph_builder.py`'s docstring). `serve.py` was deliberately not ported -
it mixes static-file serving with dynamic `/api/*` routes
(`checks.py`/`generate_stats.py`, not part of this rewrite), out of scope here.
"""
