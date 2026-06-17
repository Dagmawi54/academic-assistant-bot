import asyncio
from app.database.session import async_session_factory, engine
from sqlalchemy import text

async def main():
    print("ENGINE URL:", engine.url)
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table';"))
        tables = result.fetchall()
        print("TABLES:", tables)
        if tables:
            for t in tables:
                if t[0] == 'academic_items':
                    res = await conn.execute(text("PRAGMA table_info(academic_items);"))
                    cols = res.fetchall()
                    print("COLS:", cols)

if __name__ == "__main__":
    asyncio.run(main())
