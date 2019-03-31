from migration import DBMigration


class Migration(DBMigration):
    async def up(self, latest: int) -> None:
        await self.conn.execute(
            """
            ALTER TABLE bugreports ADD COLUMN automatic BOOL;
            UPDATE bugreports SET automatic = false;
            ALTER TABLE bugreports ALTER COLUMN automatic SET NOT NULL;

            """
        )
