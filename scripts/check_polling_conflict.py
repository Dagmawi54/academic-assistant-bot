"""Check whether another runtime is actively polling this Telegram bot."""

import asyncio

from app.bot import bot


async def main() -> None:
    try:
        updates = await bot.get_updates(timeout=1, limit=1)
        print(f"polling_available: true updates={len(updates)}")
    except Exception as exc:
        print(f"polling_available: false error={type(exc).__name__}: {exc}")
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
