"""check_channels_access must treat a Saved Messages (self-chat) target as a
clean, accessible endpoint instead of trying channel-only RPCs on a User
entity (which would raise and log a misleading '❌ UNAVAILABLE')."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.tg.client import check_channels_access


class _ExplodingClient:
    """Any RPC call means the self-chat short-circuit was NOT taken."""

    async def __call__(self, *_args, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("channel RPC must not be called for a self-chat")

    async def get_me(self):  # pragma: no cover - must not run
        raise AssertionError("get_me must not be called for a self-chat")


def test_self_chat_reported_clean_via_is_self_flag() -> None:
    self_user = SimpleNamespace(id=777, is_self=True)
    checks = asyncio.run(
        check_channels_access(
            client=_ExplodingClient(),
            chats=[self_user],
            chat_ids=["777"],
            targets=["me"],
        )
    )
    assert len(checks) == 1
    c = checks[0]
    assert c.accessible is True
    assert c.title == "Saved Messages"
    assert c.user_permissions == "admin"
    assert c.is_channel is False
    assert c.error is None


def test_self_chat_reported_clean_via_target_string() -> None:
    # Even if the entity doesn't carry is_self, the "me"/"self" target string
    # is enough to short-circuit.
    entity = SimpleNamespace(id=777)
    checks = asyncio.run(
        check_channels_access(
            client=_ExplodingClient(),
            chats=[entity],
            chat_ids=["777"],
            targets=["self"],
        )
    )
    assert checks[0].accessible is True
    assert checks[0].title == "Saved Messages"
