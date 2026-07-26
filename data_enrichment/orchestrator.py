import logging
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from .ratelimit import RateLimiter

logger = logging.getLogger(__name__)


@dataclass
class Context:
    """Общий контекст, который получает каждая операция."""
    connector: object
    rate_limiter: RateLimiter | None = None
    unit_workers: int = 8  # воркеров на параллель батчей внутри операции


class Node(ABC):
    """Узел дерева исполнения. execute возвращает число изменений."""
    name: str = ""

    @abstractmethod
    def execute(self, ctx: Context) -> int:
        raise NotImplementedError


class Op(Node):
    """Лист дерева - одна операция."""

    def __init__(self, operation) -> None:
        self.operation = operation
        self.name = operation.name

    def execute(self, ctx: Context) -> int:
        return self.operation.run(ctx)


class Sequence(Node):
    """Дочерние узлы по порядку - зависимость по данным."""

    def __init__(self, name: str, *nodes: Node) -> None:
        self.name = name
        self.nodes = nodes

    def execute(self, ctx: Context) -> int:
        logger.info(">>> последовательно: %s", self.name)
        return sum(node.execute(ctx) for node in self.nodes)


class Parallel(Node):
    """Дочерние узлы одновременно - независимы."""

    def __init__(self, name: str, *nodes: Node) -> None:
        self.name = name
        self.nodes = nodes

    def execute(self, ctx: Context) -> int:
        logger.info(">>> параллельно: %s (ветвей: %d)", self.name, len(self.nodes))
        # Ветви независимы, поэтому падение одной не должно ронять соседние 
        with ThreadPoolExecutor(max_workers=len(self.nodes)) as pool:
            futures = {pool.submit(node.execute, ctx): node for node in self.nodes}
            total = 0
            for fut, node in futures.items():
                try:
                    total += fut.result()
                except Exception:
                    logger.exception("[%s] ветвь '%s' упала", self.name, node.name)
            return total


class Loop(Node):
    """Повторять узел, пока он что-то добавляет."""

    def __init__(self, name: str, node: Node, max_iterations: int = 5) -> None:
        self.name = name
        self.node = node
        self.max_iterations = max_iterations

    def execute(self, ctx: Context) -> int:
        logger.info(">>> цикл: %s до схождения", self.name)
        total = 0
        for i in range(1, self.max_iterations + 1):
            added = self.node.execute(ctx)
            logger.info("[%s] виток %d: добавлений %d", self.name, i, added)
            total += added
            if added == 0:
                logger.info("[%s] схождение на витке %d", self.name, i)
                break
        else:
            logger.info("[%s] достигнут потолок витков (%d)", self.name, self.max_iterations)
        return total


class Orchestrator:
    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx

    def run(self, root: Node) -> None:
        root.execute(self.ctx)