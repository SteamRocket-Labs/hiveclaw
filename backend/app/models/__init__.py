"""Model registry helpers."""

from __future__ import annotations

import importlib
import pkgutil


def import_all_models() -> tuple[str, ...]:
    """Import every model module so SQLAlchemy metadata is complete."""

    imported: list[str] = []
    for module in pkgutil.iter_modules(__path__):
        if module.name.startswith("_"):
            continue
        importlib.import_module(f"{__name__}.{module.name}")
        imported.append(module.name)
    return tuple(imported)
