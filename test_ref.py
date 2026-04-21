import asyncio
from app.core.config import load_config
from app.core.database import get_session_factory
from app.repositories.law_repo import LawRepository

async def main():
    load_config("config.yaml")
    factory = get_session_factory()
    async with factory() as session:
        repo = LawRepository(session)
        cats = await repo.list_categories()
        count = await repo.count_laws()
        print(f"categories: {len(cats)}, laws: {count}")

asyncio.run(main())
