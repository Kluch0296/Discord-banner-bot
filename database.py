import sqlite3
import json
from typing import Dict, List, Optional, Any
from contextlib import contextmanager

class Database:
    """Класс для работы с базой данных настроек серверов"""
    
    def __init__(self, db_path: str = "jail_bot.db"):
        self.db_path = db_path
        self.init_database()
    
    @contextmanager
    def get_connection(self):
        """Контекстный менеджер для работы с подключением к БД"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()
    
    def init_database(self):
        """Инициализация структуры базы данных"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Таблица настроек гильдий
            cursor.execute("""
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
            cursor.execute("""
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
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS appeal_voting_durations (
                    guild_id INTEGER NOT NULL,
                    arrest_seconds INTEGER NOT NULL,
                    voting_seconds INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, arrest_seconds),
                    FOREIGN KEY (guild_id) REFERENCES guild_settings(guild_id) ON DELETE CASCADE
                )
            """)
            
            # Индексы для оптимизации
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_arrest_durations_guild 
                ON arrest_durations(guild_id)
            """)
            
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_appeal_voting_guild 
                ON appeal_voting_durations(guild_id)
            """)
    
    def get_guild_settings(self, guild_id: int) -> Optional[Dict[str, Any]]:
        """Получить настройки гильдии"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM guild_settings WHERE guild_id = ?
            """, (guild_id,))
            row = cursor.fetchone()
            
            if not row:
                return None
            
            settings = dict(row)
            settings['admin_role_ids'] = json.loads(settings['admin_role_ids'])
            
            # Получаем пресеты времени ареста
            cursor.execute("""
                SELECT label, seconds FROM arrest_durations 
                WHERE guild_id = ? ORDER BY position
            """, (guild_id,))
            settings['arrest_durations'] = [
                {'label': row['label'], 'seconds': row['seconds']}
                for row in cursor.fetchall()
            ]
            
            # Получаем настройки времени голосования
            cursor.execute("""
                SELECT arrest_seconds, voting_seconds FROM appeal_voting_durations
                WHERE guild_id = ?
            """, (guild_id,))
            settings['appeal_voting_durations'] = {
                str(row['arrest_seconds']): row['voting_seconds']
                for row in cursor.fetchall()
            }
            
            return settings
    
    def create_default_guild_settings(self, guild_id: int) -> Dict[str, Any]:
        """Создать настройки по умолчанию для новой гильдии"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Создаем базовые настройки
            cursor.execute("""
                INSERT OR IGNORE INTO guild_settings (guild_id)
                VALUES (?)
            """, (guild_id,))
            
            # Добавляем пресеты по умолчанию
            default_durations = [
                ("30 секунд", 30, 0),
                ("60 секунд", 60, 1),
                ("3 минуты", 180, 2),
                ("5 минут", 300, 3),
                ("15 минут", 900, 4),
                ("1 час", 3600, 5)
            ]
            
            for label, seconds, position in default_durations:
                cursor.execute("""
                    INSERT OR IGNORE INTO arrest_durations (guild_id, label, seconds, position)
                    VALUES (?, ?, ?, ?)
                """, (guild_id, label, seconds, position))
            
            # Добавляем настройки голосования по умолчанию
            default_voting = [
                (30, 0),
                (60, 15),
                (180, 20),
                (300, 30),
                (900, 50),
                (3600, 120)
            ]
            
            for arrest_sec, voting_sec in default_voting:
                cursor.execute("""
                    INSERT OR IGNORE INTO appeal_voting_durations 
                    (guild_id, arrest_seconds, voting_seconds)
                    VALUES (?, ?, ?)
                """, (guild_id, arrest_sec, voting_sec))
        
        return self.get_guild_settings(guild_id)
    
    def update_guild_settings(self, guild_id: int, settings: Dict[str, Any]):
        """Обновить настройки гильдии"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Обновляем базовые настройки
            cursor.execute("""
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
                # Удаляем старые
                cursor.execute("DELETE FROM arrest_durations WHERE guild_id = ?", (guild_id,))
                
                # Добавляем новые
                for position, duration in enumerate(settings['arrest_durations']):
                    cursor.execute("""
                        INSERT INTO arrest_durations (guild_id, label, seconds, position)
                        VALUES (?, ?, ?, ?)
                    """, (guild_id, duration['label'], duration['seconds'], position))
            
            # Обновляем настройки голосования
            if 'appeal_voting_durations' in settings:
                # Удаляем старые
                cursor.execute("DELETE FROM appeal_voting_durations WHERE guild_id = ?", (guild_id,))
                
                # Добавляем новые
                for arrest_sec_str, voting_sec in settings['appeal_voting_durations'].items():
                    cursor.execute("""
                        INSERT INTO appeal_voting_durations 
                        (guild_id, arrest_seconds, voting_seconds)
                        VALUES (?, ?, ?)
                    """, (guild_id, int(arrest_sec_str), voting_sec))
    
    def delete_guild_settings(self, guild_id: int):
        """Удалить все настройки гильдии"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM guild_settings WHERE guild_id = ?", (guild_id,))
            cursor.execute("DELETE FROM arrest_durations WHERE guild_id = ?", (guild_id,))
            cursor.execute("DELETE FROM appeal_voting_durations WHERE guild_id = ?", (guild_id,))
    
    def get_or_create_guild_settings(self, guild_id: int) -> Dict[str, Any]:
        """Получить настройки гильдии или создать по умолчанию"""
        settings = self.get_guild_settings(guild_id)
        if settings is None:
            settings = self.create_default_guild_settings(guild_id)
        return settings