"""The game database, used only for PLAYER_COUNT_SQL and ONLINE_SQL. Opened with no connection at start-up (minsize 0), so the API starts and runs
whether the database is up or not."""

import aiomysql


async def create_db_pool(settings):
    if not (settings.features()["player_db"] or settings.features()["roster"]):
        return None
    return await aiomysql.create_pool(
        host=settings.mysql_host, port=settings.mysql_port, user=settings.mysql_user, password=settings.mysql_password,
        db=settings.mysql_db, minsize=0, maxsize=settings.mysql_pool_size, connect_timeout=5, autocommit=True,
    )
