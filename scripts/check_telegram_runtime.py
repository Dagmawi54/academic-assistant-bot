"""Check Telegram bot runtime state without printing token."""

import asyncio

from app.bot import bot


async def main() -> None:
    info = await bot.get_webhook_info()
    print(f"webhook_url_set: {bool(info.url)}")
    print(f"pending_update_count: {info.pending_update_count}")
    print(f"last_error_present: {bool(info.last_error_message)}")
    me = await bot.get_me()
    print(f"bot_username: {me.username}")
    await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
