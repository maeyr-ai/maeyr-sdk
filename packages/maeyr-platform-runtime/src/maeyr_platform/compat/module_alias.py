"""Identity-preserving helpers for import-only legacy module aliases."""

from __future__ import annotations

import gc
import sys
from importlib import import_module
from types import ModuleType
from typing import Any, Mapping


class ImportAlias(ModuleType):
    """Forward attribute access and mutation to one canonical module object."""

    _alias_target: ModuleType

    @classmethod
    def bind(cls, alias: ModuleType, target: ModuleType) -> None:
        alias.__class__ = cls
        ModuleType.__setattr__(alias, "_alias_target", target)

    @classmethod
    def bind_namespace(cls, namespace: Mapping[str, Any], target: ModuleType) -> None:
        """Bind aliases loaded by custom loaders that omit ``sys.modules`` registration."""
        for candidate in gc.get_referrers(namespace):
            if isinstance(candidate, ModuleType) and vars(candidate) is namespace:
                cls.bind(candidate, target)
                return

    def __getattr__(self, name: str) -> Any:
        return getattr(self._alias_target, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "_alias_target":
            ModuleType.__setattr__(self, name, value)
            return
        setattr(self._alias_target, name, value)


def install_module_alias(
    namespace: Mapping[str, Any],
    target_name: str,
) -> ModuleType:
    """Install an identity-preserving alias for the module owning ``namespace``."""
    alias_name = namespace.get("__name__")
    if not isinstance(alias_name, str) or not alias_name:
        raise ValueError("module alias namespace must define a non-empty __name__")
    target = import_module(target_name)
    ImportAlias.bind_namespace(namespace, target)
    sys.modules[alias_name] = target
    return target


__all__ = ["ImportAlias", "install_module_alias"]
