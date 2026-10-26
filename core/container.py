"""core/container.py — 依赖注入容器（设计文档 §4.2）。

内核极薄，能力靠注入；register ≠ resolve，单例缓存。
"""

from __future__ import annotations

from typing import Any, Callable


class Container:
    def __init__(self) -> None:
        self._factories: dict[str, Callable[[], Any]] = {}
        self._singletons: dict[str, Any] = {}

    def register(self, name: str, factory: Callable[[], Any]) -> None:
        if name in self._factories:
            raise KeyError(f"component already registered: {name}")
        self._factories[name] = factory

    def resolve(self, name: str) -> Any:
        if name in self._singletons:
            return self._singletons[name]
        if name not in self._factories:
            raise KeyError(f"component not registered: {name}")
        instance = self._factories[name]()
        self._singletons[name] = name and instance
        return instance

    def has(self, name: str) -> bool:
        return name in self._factories or name in self._singletons
