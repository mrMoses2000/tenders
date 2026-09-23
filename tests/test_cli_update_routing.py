from __future__ import annotations

from unittest.mock import AsyncMock

from aiogram import Bot, Dispatcher
from aiogram.types import Chat, Message, Update, User


async def test_root_middleware_receives_update_before_aiogram_router() -> None:
    dispatcher = Dispatcher()
    accepted = AsyncMock()

    async def durable_ingress(_handler, update: Update, _data) -> bool:
        await accepted(update)
        return True

    dispatcher.update.outer_middleware.register(durable_ingress)
    bot = Bot("123456:abcdefghijklmnopqrstuvwxyzABCDE")
    update = Update(
        update_id=42,
        message=Message(
            message_id=7,
            date=1,
            chat=Chat(id=99, type="private"),
            from_user=User(id=99, is_bot=False, first_name="Owner"),
            text="/start",
        ),
    )
    try:
        result = await dispatcher.feed_update(bot, update)
    finally:
        await bot.session.close()

    assert result is True
    accepted.assert_awaited_once()
    assert accepted.await_args.args[0].update_id == 42
