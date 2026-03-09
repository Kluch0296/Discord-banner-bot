import discord
from discord.ext import commands, tasks
from discord import app_commands
from discord.ui import Button, View
import json
import asyncio
import logging
from typing import Dict, List, Optional
from datetime import datetime, timedelta

from database import Database
from config_ui import MainConfigPanel, ConfigDraft

# Загрузка конфигурации (только токен и префикс)
with open('config.json', 'r', encoding='utf-8') as f:
    config = json.load(f)

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('jail_bot')

# Инициализация базы данных
db = Database()

# Кэш настроек гильдий (guild_id -> settings)
guild_settings_cache: Dict[int, Dict] = {}
CACHE_TTL = 300  # 5 минут

# Блокировки для предотвращения race conditions
arrest_locks: Dict[int, asyncio.Lock] = {}  # member_id -> Lock
appeal_locks: Dict[int, asyncio.Lock] = {}  # member_id -> Lock

# Настройка intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True
intents.guilds = True

# Создание бота
bot = commands.Bot(command_prefix=config['command_prefix'], intents=intents)

# Словарь для хранения активных апелляций (временно, пока пользователь вводит текст)
active_appeals: Dict[int, Dict] = {}


def get_guild_config(guild_id: int) -> Dict:
    """Получить конфигурацию гильдии из БД с кэшированием"""
    if guild_id in guild_settings_cache:
        cached_data = guild_settings_cache[guild_id]
        # Проверяем, не устарел ли кэш
        if datetime.utcnow() - cached_data['cached_at'] < timedelta(seconds=CACHE_TTL):
            return cached_data['settings']
    
    # Загружаем из БД
    settings = db.get_or_create_guild_settings(guild_id)
    guild_settings_cache[guild_id] = {
        'settings': settings,
        'cached_at': datetime.utcnow()
    }
    return settings


def invalidate_guild_cache(guild_id: int):
    """Инвалидировать кэш настроек гильдии"""
    if guild_id in guild_settings_cache:
        del guild_settings_cache[guild_id]


def get_arrest_lock(member_id: int) -> asyncio.Lock:
    """Получить блокировку для ареста пользователя"""
    if member_id not in arrest_locks:
        arrest_locks[member_id] = asyncio.Lock()
    return arrest_locks[member_id]


def get_appeal_lock(member_id: int) -> asyncio.Lock:
    """Получить блокировку для апелляции пользователя"""
    if member_id not in appeal_locks:
        appeal_locks[member_id] = asyncio.Lock()
    return appeal_locks[member_id]


class WelcomeView(View):
    """View с кнопкой для открытия панели настроек"""
    
    def __init__(self):
        super().__init__(timeout=3600)  # 1 час timeout
        
        config_button = Button(
            label="Открыть панель",
            style=discord.ButtonStyle.primary,
            custom_id="open_config_panel"
        )
        config_button.callback = self.open_config_callback
        self.add_item(config_button)
    
    async def open_config_callback(self, interaction: discord.Interaction):
        """Callback для открытия панели настроек"""
        # Проверяем права администратора
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "❌ Только администраторы могут открывать панель настроек!",
                ephemeral=True
            )
            return
        
        # Получаем текущие настройки или создаем по умолчанию
        guild_settings = db.get_or_create_guild_settings(interaction.guild_id)
        
        # Создаем черновик настроек
        draft = ConfigDraft(interaction.guild_id, guild_settings)
        
        # Создаем главную панель
        panel = MainConfigPanel(bot, draft, interaction.user.id)
        
        # Получаем начальный экран
        content, view = panel.get_current_screen()
        
        # Отправляем панель
        await interaction.response.send_message(
            content,
            view=view,
            ephemeral=True
        )
        
        # Сохраняем ссылку на сообщение
        panel.message = await interaction.original_response()


class MemberSelectView(View):
    """View для выбора участника для ареста"""
    
    def __init__(self, members: List[discord.Member], admin: discord.Member, guild_id: int):
        super().__init__(timeout=60)
        self.selected_member: Optional[discord.Member] = None
        self.admin = admin
        self.guild_id = guild_id
        
        # Создаем кнопки для каждого участника (максимум 25 кнопок в одном View)
        for i, member in enumerate(members[:25]):
            button = Button(
                label=member.display_name,
                style=discord.ButtonStyle.primary,
                custom_id=f"member_{member.id}"
            )
            button.callback = self.create_member_callback(member)
            self.add_item(button)
    
    def create_member_callback(self, member: discord.Member):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.admin.id:
                await interaction.response.send_message(
                    "Только администратор, вызвавший команду, может выбрать участника!",
                    ephemeral=True
                )
                return
            
            self.selected_member = member
            # Переходим к выбору времени
            time_view = TimeSelectView(member, self.admin, self.guild_id)
            await interaction.response.edit_message(
                content=f"На какой срок арестовать {member.display_name}?",
                view=time_view
            )
            self.stop()
        
        return callback


class TimeSelectView(View):
    """View для выбора времени ареста"""
    
    def __init__(self, target_member: discord.Member, admin: discord.Member, guild_id: int):
        super().__init__(timeout=60)
        self.target_member = target_member
        self.admin = admin
        self.guild_id = guild_id
        
        # Получаем варианты времени из БД
        guild_config = get_guild_config(guild_id)
        arrest_durations = guild_config.get('arrest_durations', [])
        
        for duration_config in arrest_durations:
            label = duration_config.get('label', 'Неизвестно')
            seconds = duration_config.get('seconds', 0)
            button = Button(
                label=label,
                style=discord.ButtonStyle.danger,
                custom_id=f"time_{seconds}"
            )
            button.callback = self.create_time_callback(seconds)
            self.add_item(button)
    
    def create_time_callback(self, duration: int):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.admin.id:
                await interaction.response.send_message(
                    "Только администратор, вызвавший команду, может выбрать время!",
                    ephemeral=True
                )
                return
            
            await interaction.response.edit_message(
                content=f"Арестовываю {self.target_member.display_name} на {duration} секунд...",
                view=None
            )
            
            # Выполняем арест
            success = await arrest_member(
                self.target_member,
                duration,
                interaction.guild,
                interaction.user
            )
            
            if success:
                await interaction.edit_original_response(
                    content=f"✅ {self.target_member.display_name} арестован на {duration} секунд!"
                )
            else:
                await interaction.edit_original_response(
                    content=f"❌ Не удалось арестовать {self.target_member.display_name}. Проверьте права бота."
                )
            
            self.stop()
        
        return callback


class AppealButtonView(View):
    """View с кнопкой 'Подать апелляцию' для арестованного"""
    
    def __init__(self, arrested_member: discord.Member, arrest_duration: int, guild_id: int):
        super().__init__(timeout=None)
        self.arrested_member = arrested_member
        self.arrest_duration = arrest_duration
        self.guild_id = guild_id
        
        # Проверяем, доступна ли апелляция для данного срока
        guild_config = get_guild_config(guild_id)
        voting_durations = guild_config.get('appeal_voting_durations', {})
        voting_time = voting_durations.get(str(arrest_duration), 0)
        if voting_time == 0:
            # Апелляция недоступна
            self.clear_items()
            return
        
        appeal_button = Button(
            label="Подать апелляцию",
            style=discord.ButtonStyle.primary,
            custom_id=f"appeal_{arrested_member.id}"
        )
        appeal_button.callback = self.appeal_callback
        self.add_item(appeal_button)
    
    async def appeal_callback(self, interaction: discord.Interaction):
        # Проверяем, что кнопку нажал арестованный
        if interaction.user.id != self.arrested_member.id:
            await interaction.response.send_message(
                "Эта кнопка предназначена только для арестованного пользователя!",
                ephemeral=True
            )
            return
        
        # Используем блокировку для предотвращения race condition
        lock = get_appeal_lock(self.arrested_member.id)
        if lock.locked():
            await interaction.response.send_message(
                "Апелляция уже обрабатывается!",
                ephemeral=True
            )
            return
        
        async with lock:
            # Проверяем, нет ли уже активной апелляции
            if self.arrested_member.id in active_appeals:
                await interaction.response.send_message(
                    "Вы уже подали апелляцию!",
                    ephemeral=True
                )
                return
            
            # Отправляем сообщение с просьбой ввести текст апелляции
            await interaction.response.edit_message(
                content=f"{self.arrested_member.mention}, введите текст апелляции:",
                view=None
            )
            
            # Сохраняем информацию о том, что ожидаем текст апелляции
            active_appeals[self.arrested_member.id] = {
                'status': 'awaiting_text',
                'message': interaction.message,
                'duration': self.arrest_duration,
                'guild_id': self.guild_id
            }


class AppealVotingView(View):
    """View с кнопками голосования за/против освобождения"""
    
    def __init__(self, arrested_member: discord.Member, voting_duration: int, guild_id: int):
        super().__init__(timeout=voting_duration)
        self.arrested_member = arrested_member
        self.guild_id = guild_id
        self.votes_release: set = set()  # ID пользователей, проголосовавших за освобождение
        self.votes_keep: set = set()     # ID пользователей, проголосовавших против
        
        release_button = Button(
            label="Выпустить",
            style=discord.ButtonStyle.success,
            custom_id=f"vote_release_{arrested_member.id}"
        )
        release_button.callback = self.vote_release_callback
        self.add_item(release_button)
        
        keep_button = Button(
            label="Не выпускать",
            style=discord.ButtonStyle.danger,
            custom_id=f"vote_keep_{arrested_member.id}"
        )
        keep_button.callback = self.vote_keep_callback
        self.add_item(keep_button)
    
    async def vote_release_callback(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        
        # Проверяем, не является ли пользователь заключенным
        guild_config = get_guild_config(self.guild_id)
        jail_role_id = guild_config.get('jail_role_id')
        if jail_role_id:
            member = interaction.guild.get_member(user_id)
            if member and any(role.id == jail_role_id for role in member.roles):
                await interaction.response.send_message(
                    "❌ Заключенные не могут участвовать в голосовании!",
                    ephemeral=True
                )
                return
        
        # Убираем пользователя из противоположного списка, если он там есть
        if user_id in self.votes_keep:
            self.votes_keep.remove(user_id)
        
        # Добавляем в список за освобождение
        if user_id not in self.votes_release:
            self.votes_release.add(user_id)
            await interaction.response.send_message(
                "✅ Ваш голос за освобождение учтен!",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "Вы уже проголосовали за освобождение!",
                ephemeral=True
            )
    
    async def vote_keep_callback(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        
        # Проверяем, не является ли пользователь заключенным
        guild_config = get_guild_config(self.guild_id)
        jail_role_id = guild_config.get('jail_role_id')
        if jail_role_id:
            member = interaction.guild.get_member(user_id)
            if member and any(role.id == jail_role_id for role in member.roles):
                await interaction.response.send_message(
                    "❌ Заключенные не могут участвовать в голосовании!",
                    ephemeral=True
                )
                return
        
        # Убираем пользователя из противоположного списка, если он там есть
        if user_id in self.votes_release:
            self.votes_release.remove(user_id)
        
        # Добавляем в список против освобождения
        if user_id not in self.votes_keep:
            self.votes_keep.add(user_id)
            await interaction.response.send_message(
                "❌ Ваш голос против освобождения учтен!",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "Вы уже проголосовали против освобождения!",
                ephemeral=True
            )
    
    async def on_timeout(self):
        """Вызывается когда истекает время голосования"""
        # Подсчитываем голоса
        release_votes = len(self.votes_release)
        keep_votes = len(self.votes_keep)
        
        # Если голосов поровну или за освобождение больше - освобождаем
        if release_votes >= keep_votes:
            result = "✅ Апелляция одобрена! Заключенный будет освобожден."
            should_release = True
        else:
            result = "❌ Апелляция отклонена. Заключенный остается под арестом."
            should_release = False
        
        # Обновляем сообщение с результатами
        if hasattr(self, 'message') and self.message:
            try:
                await self.message.edit(
                    content=f"{self.message.content}\n\n**Голосование завершено!**\n"
                            f"За освобождение: {release_votes}\n"
                            f"Против освобождения: {keep_votes}\n\n{result}",
                    view=None
                )
            except Exception as e:
                logger.error(f"Ошибка при обновлении сообщения голосования: {e}")
        
        # Если решено освободить - освобождаем
        if should_release:
            arrest_data = db.get_active_arrest(self.arrested_member.id)
            if arrest_data:
                guild = bot.get_guild(arrest_data['guild_id'])
                if guild:
                    member = guild.get_member(self.arrested_member.id)
                    if member:
                        await release_arrested_member(member, arrest_data, "Апелляция одобрена")
        
        # Удаляем из активных апелляций
        if self.arrested_member.id in active_appeals:
            del active_appeals[self.arrested_member.id]


async def release_arrested_member(member: discord.Member, arrest_data: Dict, reason: str):
    """Освобождает арестованного участника"""
    try:
        guild = member.guild
        jail_role = guild.get_role(arrest_data['jail_role_id'])
        
        if not jail_role:
            logger.error(f"Роль заключенного {arrest_data['jail_role_id']} не найдена")
            db.remove_active_arrest(member.id)
            return
        
        # Проверяем права бота на управление ролями
        if not guild.me.guild_permissions.manage_roles:
            logger.error(f"У бота нет прав manage_roles на сервере {guild.name}")
            return
        
        # Проверяем, что роль бота выше роли заключенного
        if jail_role >= guild.me.top_role:
            logger.error(f"Роль бота ниже роли заключенного на сервере {guild.name}")
            return
        
        # Убираем роль заключенного
        try:
            await member.remove_roles(jail_role, reason=reason)
        except discord.Forbidden:
            logger.error(f"Нет прав для удаления роли заключенного у {member.display_name}")
            return
        except Exception as e:
            logger.error(f"Ошибка при удалении роли заключенного: {e}")
            return
        
        # Возвращаем оригинальные роли
        original_role_ids = arrest_data['original_role_ids']
        roles_to_add = []
        for role_id in original_role_ids:
            role = guild.get_role(role_id)
            if role and role < guild.me.top_role:
                roles_to_add.append(role)
        
        if roles_to_add:
            try:
                await member.add_roles(*roles_to_add, reason=reason)
            except discord.Forbidden:
                logger.error(f"Нет прав для возврата ролей {member.display_name}")
            except Exception as e:
                logger.error(f"Ошибка при возврате ролей: {e}")
        
        # Перемещаем обратно в оригинальный канал
        if member.voice and arrest_data['original_channel_id']:
            original_channel = guild.get_channel(arrest_data['original_channel_id'])
            if original_channel:
                try:
                    await member.move_to(original_channel, reason=reason)
                    logger.info(f"Участник {member.display_name} перемещен в {original_channel.name}")
                except discord.Forbidden:
                    logger.warning(f"Нет прав для перемещения {member.display_name}")
                except Exception as e:
                    logger.error(f"Ошибка при перемещении {member.display_name}: {e}")
        
        # Удаляем из БД
        db.remove_active_arrest(member.id)
        logger.info(f"Участник {member.display_name} освобожден: {reason}")
        
    except Exception as e:
        logger.error(f"Ошибка при освобождении участника: {e}")
        # Все равно удаляем из БД
        db.remove_active_arrest(member.id)


async def arrest_member(
    member: discord.Member,
    duration: int,
    guild: discord.Guild,
    admin: discord.Member
) -> bool:
    """Арестовывает участника на указанное время"""
    
    # Используем блокировку для предотвращения одновременных арестов
    lock = get_arrest_lock(member.id)
    if lock.locked():
        logger.warning(f"Попытка повторного ареста {member.display_name}")
        return False
    
    async with lock:
        # Проверяем, не арестован ли уже пользователь
        if db.get_active_arrest(member.id):
            logger.warning(f"{member.display_name} уже арестован")
            return False
        
        try:
            # Получаем настройки гильдии
            guild_config = get_guild_config(guild.id)
            
            # Получаем канал тюрьмы и роль заключенного
            jail_channel = guild.get_channel(guild_config['jail_channel_id'])
            jail_role = guild.get_role(guild_config['jail_role_id'])
            
            if not jail_channel or not jail_role:
                logger.error("Канал тюрьмы или роль заключенного не найдены в настройках")
                return False
            
            # Проверяем права бота
            if not guild.me.guild_permissions.manage_roles:
                logger.error(f"У бота нет прав manage_roles на сервере {guild.name}")
                return False
            
            if not guild.me.guild_permissions.move_members:
                logger.error(f"У бота нет прав move_members на сервере {guild.name}")
                return False
            
            # Проверяем, что роль бота выше роли заключенного
            if jail_role >= guild.me.top_role:
                logger.error(f"Роль бота ниже роли заключенного на сервере {guild.name}")
                return False
            
            # Сохраняем текущий голосовой канал
            original_channel_id = member.voice.channel.id if member.voice else None
            
            # Сохраняем текущие роли (кроме @everyone)
            original_role_ids = [role.id for role in member.roles if role.name != "@everyone"]
            
            # Сохраняем в БД
            db.save_active_arrest(
                member.id,
                guild.id,
                original_channel_id,
                original_role_ids,
                jail_role.id,
                duration
            )
            
            # Убираем все роли
            roles_to_remove = [role for role in member.roles if role.name != "@everyone" and role < guild.me.top_role]
            if roles_to_remove:
                try:
                    await member.remove_roles(*roles_to_remove, reason=f"Арестован администратором {admin.display_name}")
                except discord.Forbidden:
                    logger.error(f"Нет прав для удаления ролей у {member.display_name}")
                    db.remove_active_arrest(member.id)
                    return False
            
            # Добавляем роль заключенного
            try:
                await member.add_roles(jail_role, reason=f"Арестован администратором {admin.display_name}")
            except discord.Forbidden:
                logger.error(f"Нет прав для добавления роли заключенного {member.display_name}")
                # Возвращаем роли обратно
                if roles_to_remove:
                    await member.add_roles(*roles_to_remove, reason="Откат ареста")
                db.remove_active_arrest(member.id)
                return False
            
            # Перемещаем в канал тюрьмы
            if member.voice:
                try:
                    await member.move_to(jail_channel, reason=f"Арестован администратором {admin.display_name}")
                except discord.Forbidden:
                    logger.warning(f"Нет прав для перемещения {member.display_name}")
                except Exception as e:
                    logger.error(f"Ошибка при перемещении в тюрьму: {e}")
            
            # Отправляем уведомление об аресте в текстовый канал
            notification_channel_id = guild_config.get('arrest_notification_channel_id')
            if notification_channel_id:
                notification_channel = guild.get_channel(notification_channel_id)
                if notification_channel:
                    # Создаем View с кнопкой апелляции
                    appeal_view = AppealButtonView(member, duration, guild.id)
                    
                    # Формируем сообщение
                    voting_durations = guild_config.get('appeal_voting_durations', {})
                    voting_time = voting_durations.get(str(duration), 0)
                    if voting_time == 0:
                        appeal_info = "\n\n⚠️ Апелляция недоступна для данного срока ареста."
                    else:
                        appeal_info = f"\n\nВы можете подать апелляцию. Время голосования: {voting_time} секунд."
                    
                    try:
                        await notification_channel.send(
                            f"{member.mention}, вас арестовали по решению {admin.mention}.{appeal_info}",
                            view=appeal_view
                        )
                    except Exception as e:
                        logger.error(f"Ошибка при отправке уведомления об аресте: {e}")
            
            # Запускаем таймер освобождения
            asyncio.create_task(release_member_after_timeout(member.id, duration))
            
            logger.info(f"{member.display_name} арестован на {duration} секунд администратором {admin.display_name}")
            return True
            
        except Exception as e:
            logger.error(f"Ошибка при аресте участника: {e}")
            # Удаляем из БД в случае ошибки
            db.remove_active_arrest(member.id)
            return False


async def release_member_after_timeout(member_id: int, duration: int):
    """Освобождает участника после истечения времени"""
    
    await asyncio.sleep(duration)
    
    arrest_data = db.get_active_arrest(member_id)
    if not arrest_data:
        return
    
    guild = bot.get_guild(arrest_data['guild_id'])
    if not guild:
        logger.error(f"Гильдия {arrest_data['guild_id']} не найдена")
        db.remove_active_arrest(member_id)
        return
    
    member = guild.get_member(member_id)
    if not member:
        logger.info(f"Пользователь {member_id} покинул сервер, удаляем из списка арестованных")
        db.remove_active_arrest(member_id)
        return
    
    await release_arrested_member(member, arrest_data, "Срок ареста истек")


def has_admin_role(guild_id: int, member: discord.Member) -> bool:
    """Проверяет, есть ли у пользователя права администратора или одна из админских ролей"""
    # Сначала проверяем права администратора сервера
    if member.guild_permissions.administrator:
        return True
    
    # Затем проверяем дополнительные админские роли (если настроены)
    guild_config = get_guild_config(guild_id)
    admin_role_ids = guild_config.get('admin_role_ids', [])
    
    if admin_role_ids:
        user_role_ids = [role.id for role in member.roles]
        return any(role_id in user_role_ids for role_id in admin_role_ids)
    
    return False


def validate_bot_configuration(guild_id: int) -> tuple[bool, str]:
    """Проверяет, что бот настроен для использования команды ареста"""
    guild_config = get_guild_config(guild_id)
    
    # Проверяем обязательные настройки
    if guild_config.get('jail_channel_id', 0) == 0:
        return False, "❌ **Бот не настроен!**\nНе указан канал тюрьмы. Используйте команду `/jail-config` для настройки."
    
    if guild_config.get('jail_role_id', 0) == 0:
        return False, "❌ **Бот не настроен!**\nНе указана роль заключенного. Используйте команду `/jail-config` для настройки."
    
    if guild_config.get('arrest_notification_channel_id', 0) == 0:
        return False, "❌ **Бот не настроен!**\nНе указан канал для подачи апелляций. Используйте команду `/jail-config` для настройки."
    
    if guild_config.get('appeal_voting_channel_id', 0) == 0:
        return False, "❌ **Бот не настроен!**\nНе указан канал голосования по апелляциям. Используйте команду `/jail-config` для настройки."
    
    if not guild_config.get('arrest_durations'):
        return False, "❌ **Бот не настроен!**\nНе настроены пресеты времени ареста. Используйте команду `/jail-config` для настройки."
    
    return True, ""


@tasks.loop(minutes=1)
async def check_expired_arrests():
    """Фоновая задача для проверки просроченных арестов"""
    try:
        expired = db.get_expired_arrests()
        for arrest_data in expired:
            guild = bot.get_guild(arrest_data['guild_id'])
            if guild:
                member = guild.get_member(arrest_data['member_id'])
                if member:
                    await release_arrested_member(member, arrest_data, "Срок ареста истек")
                else:
                    db.remove_active_arrest(arrest_data['member_id'])
            else:
                db.remove_active_arrest(arrest_data['member_id'])
    except Exception as e:
        logger.error(f"Ошибка в check_expired_arrests: {e}")


async def restore_active_arrests():
    """Восстанавливает таймеры для активных арестов после перезапуска бота"""
    try:
        active_arrests = db.get_all_active_arrests()
        logger.info(f"Восстановление {len(active_arrests)} активных арестов")
        
        for arrest_data in active_arrests:
            # Вычисляем оставшееся время
            from datetime import datetime
            release_time = datetime.fromisoformat(arrest_data['release_timestamp'])
            now = datetime.utcnow()
            remaining_seconds = (release_time - now).total_seconds()
            
            if remaining_seconds <= 0:
                # Арест уже должен был закончиться
                guild = bot.get_guild(arrest_data['guild_id'])
                if guild:
                    member = guild.get_member(arrest_data['member_id'])
                    if member:
                        await release_arrested_member(member, arrest_data, "Срок ареста истек")
                    else:
                        db.remove_active_arrest(arrest_data['member_id'])
                else:
                    db.remove_active_arrest(arrest_data['member_id'])
            else:
                # Запускаем таймер на оставшееся время
                asyncio.create_task(release_member_after_timeout(arrest_data['member_id'], int(remaining_seconds)))
                logger.info(f"Восстановлен таймер для {arrest_data['member_id']}: {int(remaining_seconds)} сек")
    except Exception as e:
        logger.error(f"Ошибка при восстановлении арестов: {e}")


@bot.event
async def on_ready():
    """Событие при запуске бота"""
    logger.info(f'Бот {bot.user} успешно запущен!')
    logger.info(f'ID бота: {bot.user.id}')
    
    # Синхронизируем slash-команды
    try:
        synced = await bot.tree.sync()
        logger.info(f'Синхронизировано {len(synced)} slash-команд')
    except Exception as e:
        logger.error(f'Ошибка при синхронизации команд: {e}')
    
    # Восстанавливаем активные аресты
    await restore_active_arrests()
    
    # Запускаем фоновую задачу проверки просроченных арестов
    if not check_expired_arrests.is_running():
        check_expired_arrests.start()
    
    logger.info('Готов к работе!')


@bot.event
async def on_guild_join(guild: discord.Guild):
    """Событие при добавлении бота на сервер"""
    # Ждем 8 секунд перед отправкой приветственного сообщения
    await asyncio.sleep(8)
    
    # Определяем канал для отправки сообщения
    target_channel = None
    
    # Пытаемся использовать system channel
    if guild.system_channel and guild.system_channel.permissions_for(guild.me).send_messages:
        target_channel = guild.system_channel
    else:
        # Ищем первый текстовый канал, в который можем писать
        for channel in guild.text_channels:
            if channel.permissions_for(guild.me).send_messages:
                target_channel = channel
                break
    
    # Если нашли подходящий канал, отправляем приветственное сообщение
    if target_channel:
        try:
            welcome_view = WelcomeView()
            await target_channel.send(
                "Для первичной настройки используйте команду `/jail-config`\n"
                "или нажмите кнопку \"Открыть панель\""
                
                "\n\nРоль бота должна быть выше остальных ролей на сервере (кроме админских, если он не должен сажать и их)",
                view=welcome_view
            )
            logger.info(f'Приветственное сообщение отправлено на сервер {guild.name} (ID: {guild.id})')
        except Exception as e:
            logger.error(f'Ошибка при отправке приветственного сообщения на сервер {guild.name}: {e}')
    else:
        logger.warning(f'Не удалось найти подходящий канал для приветственного сообщения на сервере {guild.name}')


@bot.event
async def on_message(message):
    """Обработка сообщений для системы апелляций"""
    
    # Игнорируем сообщения от ботов
    if message.author.bot:
        await bot.process_commands(message)
        return
    
    # Проверяем, ожидается ли текст апелляции от этого пользователя
    if message.author.id in active_appeals:
        appeal_data = active_appeals[message.author.id]
        
        if appeal_data.get('status') == 'awaiting_text':
            # Получаем текст апелляции
            appeal_text = message.content
            
            # Удаляем сообщение пользователя
            try:
                await message.delete()
            except discord.Forbidden:
                logger.warning(f"Нет прав для удаления сообщения от {message.author.display_name}")
            except Exception as e:
                logger.error(f"Ошибка при удалении сообщения апелляции: {e}")
            
            # Обновляем статус в уведомлении
            try:
                await appeal_data['message'].edit(
                    content=f"{message.author.mention}, апелляция на рассмотрении...",
                    view=None
                )
            except discord.NotFound:
                logger.warning("Сообщение с апелляцией не найдено")
            except Exception as e:
                logger.error(f"Ошибка при обновлении сообщения апелляции: {e}")
            
            # Отправляем апелляцию в канал голосования
            guild_config = get_guild_config(appeal_data['guild_id'])
            appeal_channel_id = guild_config.get('appeal_voting_channel_id')
            if appeal_channel_id:
                appeal_channel = message.guild.get_channel(appeal_channel_id)
                if appeal_channel:
                    # Определяем время голосования
                    voting_durations = guild_config.get('appeal_voting_durations', {})
                    voting_duration = voting_durations.get(str(appeal_data['duration']), 30)
                    
                    # Создаем View с кнопками голосования
                    voting_view = AppealVotingView(message.author, voting_duration, appeal_data['guild_id'])
                    
                    # Отправляем сообщение с апелляцией
                    try:
                        voting_message = await appeal_channel.send(
                            f"**Апелляция от {message.author.mention}**\n\n"
                            f"**Текст апелляции:**\n{appeal_text}\n\n"
                            f"**Примите решение** (голосование: {voting_duration} сек):",
                            view=voting_view
                        )
                        
                        # Сохраняем ссылку на сообщение для обновления после голосования
                        voting_view.message = voting_message
                        
                        # Обновляем статус апелляции
                        active_appeals[message.author.id]['status'] = 'voting'
                        
                    except Exception as e:
                        logger.error(f"Ошибка при отправке апелляции в канал голосования: {e}")
                        # Удаляем из активных апелляций при ошибке
                        del active_appeals[message.author.id]
            
            return
    
    # Обрабатываем команды
    await bot.process_commands(message)


# Slash-команда для настройки бота
@bot.tree.command(name="jail-config", description="Открыть панель настроек бота")
async def jail_config(interaction: discord.Interaction):
    """Открыть панель настроек бота"""
    
    # Проверяем права доступа
    if not has_admin_role(interaction.guild_id, interaction.user):
        await interaction.response.send_message(
            "❌ У вас нет прав для использования этой команды!",
            ephemeral=True
        )
        return
    
    # Получаем текущие настройки или создаем по умолчанию
    guild_settings = db.get_or_create_guild_settings(interaction.guild_id)
    
    # Создаем черновик настроек
    draft = ConfigDraft(interaction.guild_id, guild_settings)
    
    # Создаем главную панель
    panel = MainConfigPanel(bot, draft, interaction.user.id)
    
    # Получаем начальный экран
    content, view = panel.get_current_screen()
    
    # Отправляем панель
    await interaction.response.send_message(
        content,
        view=view,
        ephemeral=True
    )
    
    # Сохраняем ссылку на сообщение
    panel.message = await interaction.original_response()


@bot.command(name='арест')
async def arrest_command(ctx: commands.Context):
    """Команда для ареста участника голосового канала"""
    
    # Проверяем права доступа
    if not has_admin_role(ctx.guild.id, ctx.author):
        await ctx.send("❌ У вас нет прав для использования этой команды!")
        return
    
    # Проверяем конфигурацию бота
    is_configured, error_message = validate_bot_configuration(ctx.guild.id)
    if not is_configured:
        await ctx.send(error_message)
        return
    
    # Проверяем, находится ли админ в голосовом канале
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("❌ Вы не в голосовом чате!")
        return
    
    # Получаем голосовой канал админа
    voice_channel = ctx.author.voice.channel
    
    # Получаем список участников канала (кроме админа и ботов)
    members = [
        member for member in voice_channel.members
        if member.id != ctx.author.id and not member.bot
    ]
    
    if not members:
        await ctx.send("❌ В голосовом канале нет других участников для ареста!")
        return
    
    # Создаем View с кнопками участников
    view = MemberSelectView(members, ctx.author, ctx.guild.id)
    await ctx.send("👮 Кого арестовать?", view=view)


@arrest_command.error
async def arrest_command_error(ctx: commands.Context, error):
    """Обработка ошибок команды арест"""
    await ctx.send(f"❌ Произошла ошибка: {str(error)}")
    logger.error(f"Ошибка в команде арест: {error}", exc_info=error)


@bot.command(name='освободить')
async def release_command(ctx: commands.Context, member: discord.Member):
    """Команда для досрочного освобождения участника"""
    
    # Проверяем права доступа
    if not has_admin_role(ctx.guild.id, ctx.author):
        await ctx.send("❌ У вас нет прав для использования этой команды!")
        return
    
    arrest_data = db.get_active_arrest(member.id)
    if not arrest_data:
        await ctx.send(f"❌ {member.display_name} не находится под арестом!")
        return
    
    await release_arrested_member(member, arrest_data, f"Досрочно освобожден {ctx.author.display_name}")
    await ctx.send(f"✅ {member.display_name} досрочно освобожден!")


# Добавляем ссылку на БД в бот для доступа из UI
bot.db = db

# Запуск бота
if __name__ == "__main__":
    bot.run(config['bot_token'])