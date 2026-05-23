"""Inspect live database academic context without printing credentials."""

import asyncio

from sqlalchemy import text

from app.database.session import async_session_factory


async def main() -> None:
    async with async_session_factory() as session:
        for table in ["groups", "topics", "courses", "academic_items", "reminders"]:
            result = await session.execute(text(f"select count(*) from {table}"))
            print(f"{table}: {result.scalar()}")

        result = await session.execute(
            text(
                """
                select
                    g.id,
                    g.chat_id,
                    g.department,
                    g.year,
                    g.section,
                    g.semester,
                    t.id as topic_id,
                    t.topic_name,
                    t.message_thread_id,
                    t.status as topic_status,
                    c.id as course_id,
                    c.course_name,
                    c.active as course_active
                from groups g
                left join topics t on t.group_id = g.id
                left join courses c on c.topic_id = t.id
                order by g.id, t.id
                limit 50
                """
            )
        )
        for row in result:
            print(tuple(row))


if __name__ == "__main__":
    asyncio.run(main())
