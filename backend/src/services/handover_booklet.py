"""Compatibility alias for handover domain rules and the optional DB service.

Pure rule imports stay stdlib-only.  When SQLAlchemy/FastAPI are installed,
service attributes are lazily loaded and an ``engine`` patch on this alias
module is forwarded through the ``bind`` keyword used by the service layer.
"""

import inspect

from .handover_core import *  # noqa: F401,F403
from . import handover_core as _core

engine = None


def __getattr__(name):
    if hasattr(_core, name):
        return getattr(_core, name)
    try:
        from . import handover_books as _service
    except ImportError:
        if name in {'HandoverBook', 'HandoverManifest', 'HandoverBooklet'}:
            return _core.HandoverBookBuilder
        raise AttributeError(f'module {__name__!r} has no attribute {name!r}') from None

    if not hasattr(_service, name):
        if name in {'HandoverManifest', 'HandoverBooklet'} and hasattr(_service, 'HandoverBook'):
            return _service.HandoverBook
        raise AttributeError(f'module {__name__!r} has no attribute {name!r}')

    attr = getattr(_service, name)
    if isinstance(attr, type):
        return attr
    try:
        accepts_bind = callable(attr) and 'bind' in inspect.signature(attr).parameters
    except (TypeError, ValueError):
        accepts_bind = False
    if not accepts_bind:
        return attr

    def _bound(*args, **kwargs):
        if engine is not None:
            kwargs.setdefault('bind', engine)
        return attr(*args, **kwargs)

    _bound.__name__ = name
    return _bound
