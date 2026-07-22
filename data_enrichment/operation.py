from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)


class Operation(ABC):
    """Атомарная операция обработки данных."""

    name: str = "operation"
    uses_external_api: bool = False
    source: str = ""

    @abstractmethod
    def run(self, ctx) -> int:
        """Выполняет операцию. Возвращает число обработанных единиц/изменений."""
        raise NotImplementedError


class PerRecordOperation(Operation):
    """Операция по каждым записям"""

    batch_size: int = 1
    # False если батчи зависят друг от друга, тогда идём строго последовательно
    parallel_fetch: bool = True

    def run(self, ctx) -> int:
        units = self.pending(ctx)
        batches = [units[i:i + self.batch_size] for i in range(0, len(units), self.batch_size)]
        logger.info("[%s] единиц: %d, батчей: %d (по %d)",
                    self.name, len(units), len(batches), self.batch_size)
        parallel = (self.parallel_fetch and self.uses_external_api
                    and getattr(ctx, "unit_workers", 1) > 1)
        done = self._run_parallel(ctx, batches) if parallel else self._run_sequential(ctx, batches)
        logger.info("[%s] обработано: %d", self.name, done)
        return done

    def _run_sequential(self, ctx, batches) -> int:
        done = 0
        for batch in batches:
            try:
                if self.uses_external_api and ctx.rate_limiter:
                    ctx.rate_limiter.acquire()
                self.save(ctx, batch, self.fetch(ctx, batch))
                ctx.connector.commit()
                done += len(batch)
            except Exception:
                logger.exception("[%s] батч упал", self.name)
        return done

    def _run_parallel(self, ctx, batches) -> int:
        # 1. параллельно: fetch под рейт лимитером
        # 2. главный поток, последовательно: save и commit по батчу
        done = 0
        with ThreadPoolExecutor(max_workers=ctx.unit_workers) as pool:
            futures = {pool.submit(self._fetch_guarded, ctx, b): b for b in batches}
            for fut in as_completed(futures):
                batch = futures[fut]
                try:
                    data = fut.result()
                except Exception:
                    logger.exception("[%s] fetch батча упал", self.name)
                    continue
                try:
                    self.save(ctx, batch, data)
                    ctx.connector.commit()
                    done += len(batch)
                except Exception:
                    logger.exception("[%s] save батча упал", self.name)
        return done

    def _fetch_guarded(self, ctx, batch):
        if self.uses_external_api and ctx.rate_limiter:
            ctx.rate_limiter.acquire()
        return self.fetch(ctx, batch)

    @abstractmethod
    def pending(self, ctx) -> list:
        """Единицы, которые ещё не обработаны."""
        raise NotImplementedError

    @abstractmethod
    def fetch(self, ctx, batch):
        """Внешний вызов/чтение по батчу единиц. Параллелится. Без записи в БД."""
        raise NotImplementedError

    @abstractmethod
    def save(self, ctx, batch, data) -> None:
        """Запись результата через коннектор. Идёт последовательно."""
        raise NotImplementedError


class WholeSetOperation(Operation):
    """Операция по всему набору сразу"""

    def run(self, ctx) -> int:
        logger.info("[%s] прогон по всему набору", self.name)
        changed = self.process_all(ctx)
        ctx.connector.commit()
        logger.info("[%s] изменений: %d", self.name, changed)
        return changed

    @abstractmethod
    def process_all(self, ctx) -> int:
        raise NotImplementedError