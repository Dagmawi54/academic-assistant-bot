"""Check whether the configured Telegram bot can access a chat."""

import asyncio
import sys

from app.bot import bot


async def main() -> None:
    chat_id = int(sys.argv[1])
    try:
        chat = await bot.get_chat(chat_id)
        print(f"chat_ok: id={chat.id} type={chat.type} title={chat.title}")
    except Exception as exc:
        print(f"chat_error: {type(exc).__name__}: {exc}")
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
