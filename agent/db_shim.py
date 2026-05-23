"""Drop-in replacement for `core.db` on the agent.

Implements only the calls bundled uploaders make at runtime.
Every other db.* attribute access raises NotImplementedError so
future coupling surfaces loudly instead of silently failing on a
SQLite file the agent doesn't have.

Audit (Phase 3): ``grep -E "(from core import db|core\\.db\\.|_db\\.)" uploaders/``
finds **one** call site — ``_db.record_image_use`` in
``uploaders/rock/orchestrator.py``. The server records upload_history /
sessions itself from the emitted success events, so no other db calls are
needed on the agent path. If a future uploader adds ``record_email_send``,
``record_post``, etc., add the corresponding event-emitting method below and
wire it onto the synthetic module in ``install_as_core_db``.
"""
from __future__ import annotations
from typing import Callable

_EmitFn = Callable[[dict], None]


class Shim:
    def __init__(self, *, emit: _EmitFn) -> None:
        self._emit = emit

    def record_image_use(self, *, photo_id, source, topic, used_on_date,
                         photographer=None, photo_url=None) -> None:
        self._emit({
            "type": "image_used",
            "photo_id": photo_id,
            "source": source,
            "topic": topic,
            "used_on_date": used_on_date,
            "photographer": photographer,
            "photo_url": photo_url,
        })

    def __getattr__(self, name: str):
        raise NotImplementedError(
            f"agent does not implement core.db.{name} — the agent ships a "
            "minimal db_shim. Add the call to agent/db_shim.py if you really "
            "need it on the agent path."
        )


def install_as_core_db(*, emit: _EmitFn) -> Shim:
    import sys as _sys, types as _types
    shim = Shim(emit=emit)
    mod = _types.ModuleType("core.db")
    # Bind the methods we DO support as module-level callables.
    mod.record_image_use = shim.record_image_use   # type: ignore[attr-defined]
    # Sentinel that proxies all other attrs to NotImplementedError.
    def _missing(name):
        def _raise(*a, **kw):
            raise NotImplementedError(
                f"agent does not implement core.db.{name} — see agent/db_shim.py"
            )
        return _raise
    class _ProxyModule(_types.ModuleType):
        def __getattr__(self, name):
            return _missing(name)
    proxy = _ProxyModule("core.db")
    proxy.record_image_use = shim.record_image_use  # type: ignore[attr-defined]
    _sys.modules["core.db"] = proxy
    return shim
