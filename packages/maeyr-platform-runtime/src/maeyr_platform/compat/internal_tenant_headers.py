from importlib import import_module as _import_module
from sys import modules as _modules
from typing import TYPE_CHECKING as _TYPE_CHECKING

from maeyr_platform.compat.module_alias import ImportAlias as _ImportAlias

if _TYPE_CHECKING:
    from maeyr_platform.security.internal_tenant_headers import *  # noqa: F403

_implementation = _import_module("maeyr_platform.security.internal_tenant_headers")
_ImportAlias.bind_namespace(globals(), _implementation)
_modules[__name__] = _implementation
