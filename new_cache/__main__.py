"""CLI-точка входа: `python -m new_cache`.

Не `python -m new_cache.export` — тот способ провоцирует
`RuntimeWarning: '...' found in sys.modules after import of package '...',
but prior to execution`: `new_cache/__init__.py` уже импортирует
`export.py` (ради `from new_cache import GraphSnapshotExporter`), так что
к моменту, когда `runpy` пытается исполнить `new_cache/export.py` ещё раз
как `__main__`, модуль `new_cache.export` уже сидит в `sys.modules` под
своим настоящим именем — получаются два разных объекта модуля с одним и
тем же кодом. `__main__.py` не импортируется пакетом заранее, поэтому
конфликта нет.
"""

from .export import main

if __name__ == "__main__":
    main()
