"""Auto-escalate the proxy on persistent connection failures mid-session.

The primary->backup->direct chain is applied when an account CONNECTS
(``app.core.accounts``). This mixin covers the second case: the proxy died
AFTER the connection was already established, in the middle of a transfer.

The trigger is deliberately conservative (the hot transfer path is left
untouched — there's no check on the success path, and the overhead only
happens on an operation that has already failed):

- only once tenacity has exhausted ALL retries, i.e. max_attempts failed in a row;
- only for connection-level errors: ``ConnectionError``/``TimeoutError``.
  ``RPCError`` doesn't count (the server responded, so the proxy is alive),
  and neither does a "wide" ``OSError`` (a disk error like ENOSPC is not a
  reason to move traffic off a working proxy);
- no more than once per client within ``_PROXY_ESCALATION_COOLDOWN_SEC``.

The failed job itself is NOT restarted — the next attempt/job will simply go
through the new level of the chain.
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)


class ProxyEscalationMixin:
    """Mixed into TgUploader/TgDownloader; see the module docstring."""

    # Set by the worker after construction: async callable(client) -> str
    # (usually AccountManager.escalate_proxy). None = feature disabled.
    proxy_escalator = None
    _PROXY_ESCALATION_COOLDOWN_SEC = 120.0

    async def _on_persistent_connection_failure(self, client, exc) -> None:
        """Call from a retry loop's ``except`` clause once retries are exhausted.

        Never raises: the original transfer error matters more than any
        escalation problems.
        """
        escalator = getattr(self, "proxy_escalator", None)
        if escalator is None or client is None:
            return
        if not isinstance(exc, (ConnectionError, TimeoutError)):
            return
        last_map = getattr(self, "_proxy_escalation_last", None)
        if last_map is None:
            last_map = {}
            self._proxy_escalation_last = last_map
        now = time.monotonic()
        last = last_map.get(id(client), float("-inf"))
        if now - last < self._PROXY_ESCALATION_COOLDOWN_SEC:
            return
        last_map[id(client)] = now
        try:
            label = await escalator(client)
        except Exception:  # noqa: BLE001 — don't mask the original error
            logger.exception("Proxy escalation attempt failed")
            return
        logger.warning(
            "Persistent connection failure (%s: %s) — proxy escalated to '%s'",
            type(exc).__name__,
            exc,
            label,
        )
