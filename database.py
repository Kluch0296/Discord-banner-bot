import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import aiosqlite

logger = logging.getLogger('jail_bot.database')


def _parse_timestamp(value: Any) -> Optional[datetime]:
    """Разобрать timestamp из БД.

    Поддерживает как новые ISO-строки с таймзоной, так и старые записи
    без tzinfo (считаются UTC — совместимость со старой схемой).
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except ValueError:
            logger.warning("Не удалось разобрать timestamp: %r", value)
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class Database:
    """Класс для асинхронной работы с базой данных настроек серверов (aiosqlite)."""

    def __init__(self, db_path: str = "jail_bot.db"):
        self.db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self):
        """Открыть постоянное соединение с БД и инициализировать схему."""
        if self._conn is not None:
            return
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._init_database()
        logger.info("Соединение с БД %s установлено (WAL)", self.db_path)

    async def close(self):
        """Закрыть соединение с БД."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
            logger.info("Соединение с БД закрыто")

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("База данных не подключена: вызовите await db.connect()")
        return self._conn

    async def _init_database(self):
        """Инициализация структуры базы данных."""
        # Таблица настроек гильдий
        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY,
                jail_channel_id INTEGER DEFAULT 0,
                jail_role_id INTEGER DEFAULT 0,
                admin_role_ids TEXT DEFAULT '[]',
                arrest_notification_channel_id INTEGER DEFAULT 0,
                appeal_voting_channel_id INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Таблица пресетов времени ареста
        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS arrest_durations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                label TEXT NOT NULL,
                seconds INTEGER NOT NULL,
                position INTEGER DEFAULT 0,
                FOREIGN KEY (guild_id) REFERENCES guild_settings(guild_id) ON DELETE CASCADE
            )
        """)

        # Таблица настроек времени голосования по апелляциям
        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS appeal_voting_durations (
                guild_id INTEGER NOT NULL,
                arrest_seconds INTEGER NOT NULL,
                voting_seconds INTEGER NOT NULL,
                PRIMARY KEY (guild_id, arrest_seconds),
                FOREIGN KEY (guild_id) REFERENCES guild_settings(guild_id) ON DELETE CASCADE
            )
        """)

        # Таблица активных арестов
        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS active_arrests (
                member_id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                original_channel_id INTEGER,
                original_role_ids TEXT NOT NULL,
                jail_role_id INTEGER NOT NULL,
                arrest_duration INTEGER NOT NULL,
                arrest_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                release_timestamp TIMESTAMP NOT NULL,
                FOREIGN KEY (guild_id) REFERENCES guild_settings(guild_id) ON DELETE CASCADE
            )
        """)

        # Индексы для оптимизации
        await self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_arrest_durations_guild
            ON arrest_durations(guild_id)
        """)
        await self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_appeal_voting_guild
            ON appeal_voting_durations(guild_id)
        """)
        await self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_active_arrests_guild
            ON active_arrests(guild_id)
        """)
        await self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_active_arrests_release
            ON active_arrests(release_timestamp)
        """)

        await self.conn.commit()

    async def get_guild_settings(self, guild_id: int) -> Optional[Dict[str, Any]]:
        """Получить настройки гильдии."""
        async with self.conn.execute(
            "SELECT * FROM guild_settings WHERE guild_id = ?", (guild_id,)
        ) as cursor:
            row = await cursor.fetchone()

        if not row:
            return None

        settings = dict(row)
        settings['admin_role_ids'] = json.loads(settings['admin_role_ids'])

        # Получаем пресеты времени ареста и настройки голосования одним запросом
        async with self.conn.execute("""
            SELECT
                ad.label,
                ad.seconds,
                avd.voting_seconds
            FROM arrest_durations ad
            LEFT JOIN appeal_voting_durations avd
                ON ad.guild_id = avd.guild_id AND ad.seconds = avd.arrest_seconds
            WHERE ad.guild_id = ?
            ORDER BY ad.position
        """, (guild_id,)) as cursor:
            rows = await cursor.fetchall()

        arrest_durations = []
        appeal_voting_durations = {}

        for r in rows:
            arrest_durations.append({
                'label': r['label'],
                'seconds': r['seconds']
            })
            if r['voting_seconds'] is not None:
                appeal_voting_durations[str(r['seconds'])] = r['voting_seconds']

        settings['arrest_durations'] = arrest_durations
        settings['appeal_voting_durations'] = appeal_voting_durations

        return settings

    async def create_default_guild_settings(self, guild_id: int) -> Dict[str, Any]:
        """Создать настройки по умолчанию для новой гильдии."""
        await self.conn.execute("""
            INSERT OR IGNORE INTO guild_settings (guild_id)
            VALUES (?)
        """, (guild_id,))

        # Пресеты по умолчанию
        default_durations = [
            ("30 секунд", 30, 0),
            ("60 секунд", 60, 1),
            ("3 минуты", 180, 2),
            ("5 минут", 300, 3),
            ("15 минут", 900, 4),
            ("1 час", 3600, 5)
        ]
        for label, seconds, position in default_durations:
            await self.conn.execute("""
                INSERT OR IGNORE INTO arrest_durations (guild_id, label, seconds, position)
                VALUES (?, ?, ?, ?)
            """, (guild_id, label, seconds, position))

        # Настройки голосования по умолчанию
        default_voting = [
            (30, 0),
            (60, 15),
            (180, 20),
            (300, 30),
            (900, 50),
            (3600, 120)
        ]
        for arrest_sec, voting_sec in default_voting:
            await self.conn.execute("""
                INSERT OR IGNORE INTO appeal_voting_durations
                (guild_id, arrest_seconds, voting_seconds)
                VALUES (?, ?, ?)
            """, (guild_id, arrest_sec, voting_sec))

        await self.conn.commit()
        return await self.get_guild_settings(guild_id)

    async def update_guild_settings(self, guild_id: int, settings: Dict[str, Any]):
        """Обновить настройки гильдии."""
        await self.conn.execute("""
            UPDATE guild_settings SET
                jail_channel_id = ?,
                jail_role_id = ?,
                admin_role_ids = ?,
                arrest_notification_channel_id = ?,
                appeal_voting_channel_id = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE guild_id = ?
        """, (
            settings.get('jail_channel_id', 0),
            settings.get('jail_role_id', 0),
            json.dumps(settings.get('admin_role_ids', [])),
            settings.get('arrest_notification_channel_id', 0),
            settings.get('appeal_voting_channel_id', 0),
            guild_id
        ))

        # Обновляем пресеты времени ареста
        if 'arrest_durations' in settings:
            await self.conn.execute(
                "DELETE FROM arrest_durations WHERE guild_id = ?", (guild_id,)
            )
            for position, duration in enumerate(settings['arrest_durations']):
                await self.conn.execute("""
                    INSERT INTO arrest_durations (guild_id, label, seconds, position)
                    VALUES (?, ?, ?, ?)
                """, (guild_id, duration['label'], duration['seconds'], position))

        # Обновляем настройки голосования
        if 'appeal_voting_durations' in settings:
            await self.conn.execute(
                "DELETE FROM appeal_voting_durations WHERE guild_id = ?", (guild_id,)
            )
            for arrest_sec_str, voting_sec in settings['appeal_voting_durations'].items():
                await self.conn.execute("""
                    INSERT INTO appeal_voting_durations
                    (guild_id, arrest_seconds, voting_seconds)
                    VALUES (?, ?, ?)
                """, (guild_id, int(arrest_sec_str), voting_sec))

        await self.conn.commit()

    async def delete_guild_settings(self, guild_id: int):
        """Удалить все настройки гильдии."""
        # FK включены (ON DELETE CASCADE), но явные DELETE надежнее
        await self.conn.execute(
            "DELETE FROM guild_settings WHERE guild_id = ?", (guild_id,)
        )
        await self.conn.execute(
            "DELETE FROM arrest_durations WHERE guild_id = ?", (guild_id,)
        )
        await self.conn.execute(
            "DELETE FROM appeal_voting_durations WHERE guild_id = ?", (guild_id,)
        )
        await self.conn.commit()

    async def get_or_create_guild_settings(self, guild_id: int) -> Dict[str, Any]:
        """Получить настройки гильдии или создать по умолчанию."""
        settings = await self.get_guild_settings(guild_id)
        if settings is None:
            settings = await self.create_default_guild_settings(guild_id)
        return settings

    async def save_active_arrest(
        self,
        member_id: int,
        guild_id: int,
        original_channel_id: Optional[int],
        original_role_ids: List[int],
        jail_role_id: int,
        arrest_duration: int
    ) -> datetime:
        """Сохранить информацию об активном аресте.

        Возвращает момент освобождения (aware datetime, UTC).
        """
        release_timestamp = datetime.now(timezone.utc) + timedelta(seconds=arrest_duration)
        await self.conn.execute("""
            INSERT OR REPLACE INTO active_arrests
            (member_id, guild_id, original_channel_id, original_role_ids, jail_role_id,
             arrest_duration, arrest_timestamp, release_timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            member_id, guild_id, original_channel_id, json.dumps(original_role_ids),
            jail_role_id, arrest_duration,
            datetime.now(timezone.utc).isoformat(),
            release_timestamp.isoformat()
        ))
        await self.conn.commit()
        return release_timestamp

    @staticmethod
    def _row_to_arrest(row: aiosqlite.Row) -> Dict[str, Any]:
        arrest = dict(row)
        arrest['original_role_ids'] = json.loads(arrest['original_role_ids'])
        arrest['release_datetime'] = _parse_timestamp(arrest.get('release_timestamp'))
        return arrest

    async def get_active_arrest(self, member_id: int) -> Optional[Dict[str, Any]]:
        """Получить информацию об активном аресте."""
        async with self.conn.execute(
            "SELECT * FROM active_arrests WHERE member_id = ?", (member_id,)
        ) as cursor:
            row = await cursor.fetchone()

        if not row:
            return None
        return self._row_to_arrest(row)

    async def remove_active_arrest(self, member_id: int):
        """Удалить информацию об активном аресте."""
        await self.conn.execute(
            "DELETE FROM active_arrests WHERE member_id = ?", (member_id,)
        )
        await self.conn.commit()

    async def get_all_active_arrests(self, guild_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """Получить все активные аресты (опционально для конкретной гильдии)."""
        if guild_id:
            query = "SELECT * FROM active_arrests WHERE guild_id = ?"
            params = (guild_id,)
        else:
            query = "SELECT * FROM active_arrests"
            params = ()

        async with self.conn.execute(query, params) as cursor:
            rows = await cursor.fetchall()

        return [self._row_to_arrest(row) for row in rows]

    async def get_expired_arrests(self) -> List[Dict[str, Any]]:
        """Получить все просроченные аресты.

        Фильтрация выполняется в Python, чтобы корректно обрабатывать
        как старые naive-timestamp'ы (UTC), так и новые ISO-строки с таймзоной.
        """
        arrests = await self.get_all_active_arrests()
        now = datetime.now(timezone.utc)
        expired = []
        for arrest in arrests:
            release_dt = arrest.get('release_datetime')
            if release_dt is None or release_dt <= now:
                expired.append(arrest)
        return expired
