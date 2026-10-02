import copy
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import aiosqlite

from database import Database


def create_old_database(path, *, voice_setting):
    """Схема прежних версий; нестандартные настройки нескольких серверов."""
    with sqlite3.connect(path) as conn:
        voice_column = 'voice_pull_enabled INTEGER NOT NULL DEFAULT 0,' if voice_setting else ''
        conn.executescript(f"""
            PRAGMA foreign_keys=ON;
            CREATE TABLE guild_settings (
                guild_id INTEGER PRIMARY KEY,
                jail_channel_id INTEGER DEFAULT 0,
                jail_role_id INTEGER DEFAULT 0,
                admin_role_ids TEXT DEFAULT '[]',
                {voice_column}
                arrest_notification_channel_id INTEGER DEFAULT 0,
                appeal_voting_channel_id INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE arrest_durations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL, label TEXT NOT NULL,
                seconds INTEGER NOT NULL, position INTEGER DEFAULT 0,
                FOREIGN KEY (guild_id) REFERENCES guild_settings(guild_id) ON DELETE CASCADE
            );
            CREATE TABLE appeal_voting_durations (
                guild_id INTEGER NOT NULL, arrest_seconds INTEGER NOT NULL,
                voting_seconds INTEGER NOT NULL,
                PRIMARY KEY (guild_id, arrest_seconds),
                FOREIGN KEY (guild_id) REFERENCES guild_settings(guild_id) ON DELETE CASCADE
            );
            CREATE TABLE active_arrests (
                member_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL,
                original_channel_id INTEGER, original_role_ids TEXT NOT NULL,
                jail_role_id INTEGER NOT NULL, arrest_duration INTEGER NOT NULL,
                arrest_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                release_timestamp TIMESTAMP NOT NULL,
                FOREIGN KEY (guild_id) REFERENCES guild_settings(guild_id) ON DELETE CASCADE
            );
        """)
        for guild_id in (100, 200):
            conn.execute("""
                INSERT INTO guild_settings
                (guild_id, jail_channel_id, jail_role_id, admin_role_ids,
                 arrest_notification_channel_id, appeal_voting_channel_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (guild_id, guild_id + 1, guild_id + 2, f'[{guild_id + 3}, {guild_id + 4}]',
                  guild_id + 5, guild_id + 6, '2025-01-01 00:00:00', '2025-02-02 12:34:56'))
            if voice_setting:
                conn.execute('UPDATE guild_settings SET voice_pull_enabled=? WHERE guild_id=?',
                             (int(guild_id == 100), guild_id))
            conn.executemany('INSERT INTO arrest_durations (guild_id,label,seconds,position) VALUES (?,?,?,?)',
                             [(guild_id, 'Своя длительность', 42, 0), (guild_id, 'Долгий арест', 1234, 1)])
            conn.executemany('INSERT INTO appeal_voting_durations VALUES (?,?,?)',
                             [(guild_id, 42, 0), (guild_id, 1234, 79)])
            conn.execute('INSERT INTO active_arrests VALUES (?,?,?,?,?,?,?,?)',
                         (guild_id + 7, guild_id, guild_id + 8, '[901, 902]', guild_id + 2,
                          1234, '2025-02-02 12:34:56', '2030-01-01 00:00:00'))


def snapshot(path):
    """Снимок всех исходных столбцов, включая ID, даты и sqlite_sequence."""
    with sqlite3.connect(path) as conn:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {
            table: ([r[1] for r in conn.execute(f'PRAGMA table_info({table})')],
                    list(conn.execute(f'SELECT * FROM {table} ORDER BY rowid')))
            for table in tables
        }


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    def assert_old_data_preserved(self, before, after):
        self.assertEqual(set(before), set(after))
        for table, (columns, rows) in before.items():
            new_columns, new_rows = after[table]
            indices = [new_columns.index(column) for column in columns]
            self.assertEqual(rows, [tuple(row[i] for i in indices) for row in new_rows], table)

    async def test_old_schemas_preserve_every_row_and_repeat_safely(self):
        for voice_setting in (False, True):
            with self.subTest(voice_setting=voice_setting), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'legacy.db'
                create_old_database(path, voice_setting=voice_setting)
                before = snapshot(path)
                db = Database(str(path))
                await db.connect()
                try:
                    for guild_id in (100, 200):
                        settings = await db.get_or_create_guild_settings(guild_id)
                        self.assertEqual(settings['moderator_role_ids'], [])
                        self.assertEqual(settings['admin_role_ids'], [guild_id + 3, guild_id + 4])
                        self.assertEqual(settings['voice_pull_enabled'], voice_setting and guild_id == 100)
                        self.assertEqual(settings['arrest_durations'], [
                            {'label': 'Своя длительность', 'seconds': 42},
                            {'label': 'Долгий арест', 'seconds': 1234},
                        ])
                        self.assertEqual(settings['appeal_voting_durations'], {'42': 0, '1234': 79})
                    self.assertEqual(len(await db.get_all_active_arrests()), 2)
                    async with db.conn.execute('PRAGMA foreign_key_check') as cursor:
                        self.assertEqual(await cursor.fetchall(), [])
                finally:
                    await db.close()
                after = snapshot(path)
                self.assert_old_data_preserved(before, after)
                await db.connect()
                await db.close()
                self.assertEqual(snapshot(path), after)

    async def test_moderators_save_clear_and_survive_missing_field_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'legacy.db'
            create_old_database(path, voice_setting=True)
            db = Database(str(path))
            await db.connect()
            try:
                other_guild = await db.get_guild_settings(200)
                settings = await db.get_guild_settings(100)
                settings['moderator_role_ids'] = [301, 302]
                await db.update_guild_settings(100, settings)
                saved = await db.get_guild_settings(100)
                self.assertEqual(saved['moderator_role_ids'], [301, 302])
                legacy_draft = copy.deepcopy(saved)
                legacy_draft.pop('moderator_role_ids')
                await db.update_guild_settings(100, legacy_draft)
                self.assertEqual((await db.get_guild_settings(100))['moderator_role_ids'], [301, 302])
                self.assertEqual(await db.get_guild_settings(200), other_guild)
                self.assertEqual(len(await db.get_all_active_arrests()), 2)
            finally:
                await db.close()
            await db.connect()
            try:
                saved = await db.get_guild_settings(100)
                self.assertEqual(saved['moderator_role_ids'], [301, 302])
                saved['moderator_role_ids'] = []
                await db.update_guild_settings(100, saved)
                self.assertEqual((await db.get_guild_settings(100))['moderator_role_ids'], [])
            finally:
                await db.close()

    async def test_failure_rolls_back_schema_and_data_then_allows_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'legacy.db'
            create_old_database(path, voice_setting=False)
            before = snapshot(path)
            db = Database(str(path))
            original_execute = aiosqlite.Connection.execute

            def fail_after_alter(conn, sql, *args, **kwargs):
                if 'CREATE INDEX' in sql:
                    raise RuntimeError('Simulated migration failure')
                return original_execute(conn, sql, *args, **kwargs)

            with patch.object(aiosqlite.Connection, 'execute', fail_after_alter):
                with self.assertRaisesRegex(RuntimeError, 'Simulated migration failure'):
                    await db.connect()
            self.assertIsNone(db._conn)
            self.assertEqual(snapshot(path), before)
            await db.connect()
            await db.close()
            self.assert_old_data_preserved(before, snapshot(path))

    async def test_new_database_defaults(self):
        db = Database(':memory:')
        await db.connect()
        try:
            settings = await db.get_or_create_guild_settings(300)
            self.assertEqual(settings['moderator_role_ids'], [])
            self.assertEqual(settings['admin_role_ids'], [])
            self.assertEqual(len(settings['arrest_durations']), 6)
        finally:
            await db.close()
