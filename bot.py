import discord
from discord.ext import commands, tasks
from discord import app_commands
from discord.ui import Button, View
import os
import json
import asyncio
import collections
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional, Set
from datetime import datetime, timedelta, timezone

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

# Инициализация базы данных (async, соединение открывается в setup_hook)
db = Database()

# Безусловные супер-администраторы бота (работают на любом сервере,
# независимо от настроек гильдии и прав администратора Discord)
SUPER_ADMIN_IDS: Set[int] = {348169652087685122}

# Пользователь, при упоминании которого бот отправляет картинку в текущий чат.
LORDKORVIN_ID = 367380988939993122
LORDKORVIN_IMAGE = Path(__file__).resolve().parent / 'assets' / 'lordkorvin.jpg'

# Кэш настроек гильдий (guild_id -> {'settings': ..., 'cached_at': ...})
guild_settings_cache: Dict[int, Dict] = {}
CACHE_TTL = 300  # 5 минут

# Блокировки для предотвращения race conditions
arrest_locks: Dict[int, asyncio.Lock] = collections.defaultdict(asyncio.Lock)
appeal_locks: Dict[int, asyncio.Lock] = collections.defaultdict(asyncio.Lock)

# Участники с активным голосованием по апелляции (защита от повторной подачи)
active_appeal_members: Set[int] = set()

# Настройка intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True
intents.guilds = True


class JailBot(commands.Bot):
    """Подкласс бота с setup_hook: подключение БД, sync команд, persistent views."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._guild_commands_synced = False

    async def setup_hook(self):
        # Подключаем БД до старта событий
        await db.connect()

        # Регистрируем persistent views/items (работают после рестарта)
        self.add_view(WelcomeView())
        self.add_dynamic_items(AppealButton)

        # Раньше здесь был глобальный tree.sync(). Он дублировал команды:
        # глобальный набор + guild-набор (см. sync_guild_commands ниже) →
        # в клиенте каждая команда показывалась дважды. Глобальный sync убран;
        # команды живут только в guild-scope и появляются мгновенно.
        #
        # Одноразовая зачистка осиротевшего глобального набора: если бот ранее
        # регистрировал команды глобально (через tree.sync() без guild), они
        # остаются в Discord и дублируются с guild-командами. Запуск с
        # CLEARGLOBAL=1 отправляет пустой bulk-upsert в глобальный endpoint
        # Discord — это удаляет глобальные команды на стороне Discord.
        #
        # Важно: шлём пустой payload напрямую через http.bulk_upsert_global_commands,
        # а НЕ tree.sync(guild=None) — последний возьмёт команды из локального
        # cache (_global_commands, заполненного декораторами) и зарегистрирует
        # их глобально СНОВА. Прямой пустой payload cache не трогает, поэтому
        # последующий copy_global_to + sync(guild=...) для серверов отработает.
        if os.environ.get('CLEARGLOBAL') == '1':
            try:
                if self.application_id is None:
                    logger.warning('CLEARGLOBAL: application_id ещё не известен — пропуск')
                else:
                    await self.http.bulk_upsert_global_commands(
                        self.application_id, payload=[]
                    )
                    logger.info('Глобальные slash-команды зачищены (CLEARGLOBAL=1)')
            except Exception as e:
                logger.error(f'Ошибка при зачистке глобальных команд: {e}')

    async def sync_guild_commands(self):
        """Копирует глобальные команды на каждый сервер и синхронизирует их.

        Guild-sync применяется мгновенно (в отличие от глобального),
        поэтому команды сразу видны в списке при вводе «/».
        Вызывается один раз после on_ready, когда список гильдий доступен.
        """
        if self._guild_commands_synced:
            return
        self._guild_commands_synced = True

        for guild in self.guilds:
            try:
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                logger.info(
                    f'Синхронизировано {len(synced)} slash-команд на сервере {guild.name} (ID: {guild.id})'
                )
            except Exception as e:
                logger.error(f'Ошибка при синхронизации команд на сервере {guild.name}: {e}')

    async def close(self):
        try:
            await db.close()
        finally:
            await super().close()


bot = JailBot(command_prefix=config['command_prefix'], intents=intents)


async def send_interaction_message(
    interaction: discord.Interaction,
    content: Optional[str] = None,
    *,
    embed: Optional[discord.Embed] = None,
    ephemeral: bool = False,
    view: Optional[discord.ui.View] = None
) -> Optional[discord.Message]:
    """Безопасно отправляет ответ на interaction.

    Если первоначальный ответ уже отправлен, используется followup.
    Если interaction протух (Unknown interaction), логируем и возвращаем None.
    """
    try:
        kwargs = {"ephemeral": ephemeral}
        if view is not None:
            kwargs["view"] = view
        if embed is not None:
            kwargs["embed"] = embed
        if interaction.response.is_done():
            return await interaction.followup.send(content, **kwargs)

        await interaction.response.send_message(content, **kwargs)
        return await interaction.original_response()
    except discord.NotFound:
        logger.warning(
            "Interaction %s уже недействителен (Unknown interaction): команду обработали слишком поздно",
            interaction.id
        )
        return None


async def get_guild_config(guild_id: int) -> Dict:
    """Получить конфигурацию гильдии из БД с кэшированием (TTL)."""
    cached_data = guild_settings_cache.get(guild_id)
    if cached_data is not None:
        if datetime.now(timezone.utc) - cached_data['cached_at'] < timedelta(seconds=CACHE_TTL):
            return cached_data['settings']
        # Протухшая запись — удаляем
        del guild_settings_cache[guild_id]

    settings = await db.get_or_create_guild_settings(guild_id)
    guild_settings_cache[guild_id] = {
        'settings': settings,
        'cached_at': datetime.now(timezone.utc)
    }
    return settings


def invalidate_guild_cache(guild_id: int):
    """Инвалидировать кэш настроек гильдии."""
    guild_settings_cache.pop(guild_id, None)


def cleanup_lock(locks: Dict[int, asyncio.Lock], member_id: int):
    """Удалить лок из словаря, если он свободен."""
    lock = locks.get(member_id)
    if lock is not None and not lock.locked():
        locks.pop(member_id, None)


def duration_label(guild_config: Dict, seconds: int) -> str:
    """Вернуть label пресета по количеству секунд (или человекочитаемый fallback)."""
    for d in guild_config.get('arrest_durations', []):
        if d.get('seconds') == seconds:
            return d.get('label', f"{seconds} сек")
    return f"{seconds} сек"


# ---------------------------------------------------------------------------
# Embeds
# ---------------------------------------------------------------------------

def build_arrest_embed(
    member: discord.Member,
    admin: discord.abc.User,
    duration_text: str,
    release_at: datetime,
    appeal_info: str
) -> discord.Embed:
    """Embed уведомления об аресте (красный)."""
    unix = int(release_at.timestamp())
    embed = discord.Embed(
        title="🚔 Арест",
        description=appeal_info,
        color=discord.Color.red()
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Кого", value=member.mention, inline=True)
    embed.add_field(name="Кем", value=admin.mention, inline=True)
    embed.add_field(name="Срок", value=duration_text, inline=True)
    embed.add_field(name="Освобождение", value=f"<t:{unix}:R> (<t:{unix}:T>)", inline=False)
    return embed


def build_release_embed(member: discord.Member, reason: str) -> discord.Embed:
    """Embed сообщения об освобождении (зелёный)."""
    embed = discord.Embed(
        title="🔓 Освобождение",
        description=reason,
        color=discord.Color.green()
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Кого", value=member.mention, inline=True)
    return embed


def build_voting_embed(
    member: discord.Member,
    appeal_text: str,
    deadline: datetime
) -> discord.Embed:
    """Embed голосования по апелляции (синий)."""
    unix = int(deadline.timestamp())
    embed = discord.Embed(
        title="⚖️ Апелляция",
        description=f"**Текст апелляции:**\n{appeal_text}",
        color=discord.Color.blue()
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Кого", value=member.mention, inline=True)
    embed.add_field(name="Дедлайн голосования", value=f"<t:{unix}:R> (<t:{unix}:T>)", inline=False)
    return embed


def build_voting_result_embed(
    member: discord.Member,
    release_votes: int,
    keep_votes: int,
    released: bool
) -> discord.Embed:
    """Embed результата голосования."""
    if released:
        embed = discord.Embed(
            title="✅ Апелляция одобрена",
            description="Заключенный будет освобожден.",
            color=discord.Color.green()
        )
    else:
        embed = discord.Embed(
            title="❌ Апелляция отклонена",
            description="Заключенный остается под арестом.",
            color=discord.Color.red()
        )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Кого", value=member.mention, inline=True)
    embed.add_field(name="✅ За освобождение", value=str(release_votes), inline=True)
    embed.add_field(name="❌ Против", value=str(keep_votes), inline=True)
    return embed


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

class WelcomeView(View):
    """Persistent view с кнопкой для открытия панели настроек."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Открыть панель",
        style=discord.ButtonStyle.primary,
        custom_id="open_config_panel"
    )
    async def open_config(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Callback для открытия панели настроек."""
        # Проверка прав ДО defer
        if (interaction.user.id not in SUPER_ADMIN_IDS
                and not interaction.user.guild_permissions.administrator):
            await interaction.response.send_message(
                "❌ Только администраторы могут открывать панель настроек!",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        # Получаем текущие настройки или создаем по умолчанию
        guild_settings = await db.get_or_create_guild_settings(interaction.guild_id)

        draft = ConfigDraft(interaction.guild_id, guild_settings)
        panel = MainConfigPanel(bot, draft, interaction.user.id)
        embed, view = panel.get_current_screen()

        panel.message = await interaction.followup.send(
            embed=embed,
            view=view,
            ephemeral=True,
            wait=True
        )


class MemberSelectView(View):
    """View с Select для выбора участника для ареста."""

    def __init__(self, members: List[discord.Member], admin: discord.Member,
                 guild_id: int, guild_config: Dict):
        super().__init__(timeout=60)
        self.admin = admin
        self.guild_id = guild_id
        self.guild_config = guild_config
        self.members_by_id = {m.id: m for m in members[:25]}

        select = discord.ui.Select(
            placeholder="🔍 Выберите участника для ареста...",
            options=[
                discord.SelectOption(label=m.display_name, value=str(m.id))
                for m in members[:25]
            ]
        )
        select.callback = self.select_callback
        self.select = select
        self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.admin.id:
            await interaction.response.send_message(
                "Только администратор, вызвавший команду, может выбрать участника!",
                ephemeral=True
            )
            return

        member = self.members_by_id.get(int(self.select.values[0]))
        if member is None:
            await interaction.response.send_message("❌ Участник не найден!", ephemeral=True)
            return

        # Переходим к выбору времени (настройки уже загружены)
        time_view = TimeSelectView(member, self.admin, self.guild_id, self.guild_config)
        await interaction.response.edit_message(
            content=f"На какой срок арестовать {member.display_name}?",
            view=time_view
        )
        self.stop()


class SleepMemberSelectView(View):
    """View с Select для выбора участника для отключения из голосового канала."""

    def __init__(self, members: List[discord.Member], admin: discord.Member):
        super().__init__(timeout=60)
        self.admin = admin
        self.members_by_id = {m.id: m for m in members[:25]}

        select = discord.ui.Select(
            placeholder="🔍 Выберите участника для отключения...",
            options=[
                discord.SelectOption(label=m.display_name, value=str(m.id))
                for m in members[:25]
            ]
        )
        select.callback = self.select_callback
        self.select = select
        self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.admin.id:
            await interaction.response.send_message(
                "Только администратор, вызвавший команду, может выбрать участника!",
                ephemeral=True
            )
            return

        member = self.members_by_id.get(int(self.select.values[0]))
        if member is None:
            await interaction.response.send_message("❌ Участник не найден!", ephemeral=True)
            return

        # Отключаем пользователя из голосового канала
        if member.voice and member.voice.channel:
            try:
                await member.move_to(None, reason=f"Отключен командой !спать от {self.admin.display_name}")
                await interaction.response.edit_message(
                    content=f"✅ {member.display_name} отключен из голосового канала!",
                    view=None
                )
                logger.info(f"{member.display_name} отключен из голосового канала администратором {self.admin.display_name}")
            except discord.Forbidden:
                await interaction.response.edit_message(
                    content=f"❌ Нет прав для отключения {member.display_name}!",
                    view=None
                )
                logger.error(f"Нет прав для отключения {member.display_name}")
            except Exception as e:
                await interaction.response.edit_message(
                    content=f"❌ Ошибка при отключении {member.display_name}!",
                    view=None
                )
                logger.error(f"Ошибка при отключении {member.display_name}: {e}")
        else:
            await interaction.response.edit_message(
                content=f"❌ {member.display_name} не находится в голосовом канале!",
                view=None
            )

        self.stop()


class TimeSelectView(View):
    """View для выбора времени ареста (настройки передаются уже загруженными)."""

    def __init__(self, target_member: discord.Member, admin: discord.Member,
                 guild_id: int, guild_config: Dict):
        super().__init__(timeout=60)
        self.target_member = target_member
        self.admin = admin
        self.guild_id = guild_id

        arrest_durations = guild_config.get('arrest_durations', [])

        for duration_config in arrest_durations[:25]:
            label = duration_config.get('label', 'Неизвестно')
            seconds = duration_config.get('seconds', 0)
            button = Button(
                label=label,
                style=discord.ButtonStyle.danger,
                custom_id=f"time_{seconds}"
            )
            button.callback = self.create_time_callback(seconds, label)
            self.add_item(button)

    def create_time_callback(self, duration: int, label: str):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.admin.id:
                await interaction.response.send_message(
                    "Только администратор, вызвавший команду, может выбрать время!",
                    ephemeral=True
                )
                return

            await interaction.response.edit_message(
                content=f"Арестовываю {self.target_member.display_name} на {label}...",
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
                    content=f"✅ {self.target_member.display_name} арестован на {label}!"
                )
            else:
                await interaction.edit_original_response(
                    content=f"❌ Не удалось арестовать {self.target_member.display_name}. Проверьте права бота."
                )

            self.stop()

        return callback


class AppealModal(discord.ui.Modal, title="Подача апелляции"):
    """Модальное окно для ввода текста апелляции."""

    appeal_text = discord.ui.TextInput(
        label="Текст апелляции",
        style=discord.TextStyle.paragraph,
        placeholder="Опишите, почему вас следует освободить...",
        required=True,
        max_length=1000
    )

    def __init__(self, member: discord.Member, arrest_duration: int, guild_id: int):
        super().__init__()
        self.member = member
        self.arrest_duration = arrest_duration
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        lock = appeal_locks[self.member.id]
        if lock.locked():
            await interaction.response.send_message("Апелляция уже обрабатывается!", ephemeral=True)
            return

        async with lock:
            try:
                # Защита от повторной подачи
                if self.member.id in active_appeal_members:
                    await interaction.response.send_message(
                        "Вы уже подали апелляцию!", ephemeral=True
                    )
                    return

                # Арест еще активен?
                arrest_data = await db.get_active_arrest(self.member.id)
                if not arrest_data:
                    await interaction.response.send_message(
                        "Ваш арест уже завершен.", ephemeral=True
                    )
                    return

                guild_config = await get_guild_config(self.guild_id)
                appeal_channel_id = guild_config.get('appeal_voting_channel_id')
                if not appeal_channel_id:
                    await interaction.response.send_message(
                        "❌ Канал голосования не настроен!", ephemeral=True
                    )
                    return

                appeal_channel = interaction.guild.get_channel(appeal_channel_id)
                if not appeal_channel:
                    await interaction.response.send_message(
                        "❌ Канал голосования не найден!", ephemeral=True
                    )
                    return

                # Определяем время голосования
                voting_durations = guild_config.get('appeal_voting_durations', {})
                voting_duration = voting_durations.get(str(self.arrest_duration), 30)
                if voting_duration <= 0:
                    await interaction.response.send_message(
                        "⚠️ Апелляция недоступна для данного срока ареста.", ephemeral=True
                    )
                    return

                deadline = datetime.now(timezone.utc) + timedelta(seconds=voting_duration)

                # Создаем View с кнопками голосования и отправляем embed
                voting_view = AppealVotingView(self.member, voting_duration, self.guild_id)
                voting_embed = build_voting_embed(self.member, self.appeal_text.value, deadline)

                voting_message = await appeal_channel.send(embed=voting_embed, view=voting_view)
                voting_view.message = voting_message

                active_appeal_members.add(self.member.id)

                await interaction.response.send_message(
                    "✅ Апелляция подана! Голосование началось.", ephemeral=True
                )
                logger.info(f"Апелляция от {self.member.display_name} отправлена на голосование")
            except Exception as e:
                logger.error(f"Ошибка при подаче апелляции: {e}")
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "❌ Ошибка при подаче апелляции!", ephemeral=True
                    )
        cleanup_lock(appeal_locks, self.member.id)


class AppealButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"appeal_(?P<member_id>[0-9]+)"
):
    """Persistent-кнопка «Подать апелляцию»: работает и после рестарта бота.

    Данные ареста берутся из БД по member_id из custom_id.
    """

    def __init__(self, member_id: int):
        self.member_id = member_id
        super().__init__(
            discord.ui.Button(
                label="Подать апелляцию",
                style=discord.ButtonStyle.primary,
                custom_id=f"appeal_{member_id}"
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match):
        return cls(int(match['member_id']))

    async def callback(self, interaction: discord.Interaction):
        # Кнопка только для арестованного
        if interaction.user.id != self.member_id:
            await interaction.response.send_message(
                "Эта кнопка предназначена только для арестованного пользователя!",
                ephemeral=True
            )
            return

        # Защита от повторной подачи
        if self.member_id in active_appeal_members:
            await interaction.response.send_message(
                "Вы уже подали апелляцию!", ephemeral=True
            )
            return

        # Проверяем, что арест еще активен (данные из БД)
        arrest_data = await db.get_active_arrest(self.member_id)
        if not arrest_data:
            await interaction.response.send_message(
                "Ваш арест уже завершен.", ephemeral=True
            )
            return

        guild_config = await get_guild_config(arrest_data['guild_id'])
        voting_durations = guild_config.get('appeal_voting_durations', {})
        voting_time = voting_durations.get(str(arrest_data['arrest_duration']), 0)
        if voting_time <= 0:
            await interaction.response.send_message(
                "⚠️ Апелляция недоступна для данного срока ареста.", ephemeral=True
            )
            return

        member = interaction.guild.get_member(self.member_id) or interaction.user
        modal = AppealModal(member, arrest_data['arrest_duration'], arrest_data['guild_id'])
        await interaction.response.send_modal(modal)


class AppealVotingView(View):
    """View с кнопками голосования за/против освобождения (живой счётчик)."""

    def __init__(self, arrested_member: discord.Member, voting_duration: int, guild_id: int):
        super().__init__(timeout=voting_duration)
        self.arrested_member = arrested_member
        self.guild_id = guild_id
        self.message: Optional[discord.Message] = None
        self.votes_release: set = set()
        self.votes_keep: set = set()

        self.release_button = Button(
            label="✅ Выпустить (0)",
            style=discord.ButtonStyle.success,
            custom_id=f"vote_release_{arrested_member.id}"
        )
        self.release_button.callback = self.vote_release_callback
        self.add_item(self.release_button)

        self.keep_button = Button(
            label="❌ Не выпускать (0)",
            style=discord.ButtonStyle.danger,
            custom_id=f"vote_keep_{arrested_member.id}"
        )
        self.keep_button.callback = self.vote_keep_callback
        self.add_item(self.keep_button)

    async def _is_jailed(self, interaction: discord.Interaction) -> bool:
        """Проверяет, является ли голосующий заключенным."""
        guild_config = await get_guild_config(self.guild_id)
        jail_role_id = guild_config.get('jail_role_id')
        if jail_role_id:
            member = interaction.guild.get_member(interaction.user.id)
            if member and any(role.id == jail_role_id for role in member.roles):
                return True
        return False

    def _update_labels(self):
        self.release_button.label = f"✅ Выпустить ({len(self.votes_release)})"
        self.keep_button.label = f"❌ Не выпускать ({len(self.votes_keep)})"

    async def _refresh_message(self, interaction: discord.Interaction):
        """Обновить сообщение голосования с новыми счётчиками."""
        self._update_labels()
        try:
            await interaction.message.edit(view=self)
        except discord.HTTPException as e:
            logger.warning(f"Не удалось обновить счётчик голосов: {e}")

    async def vote_release_callback(self, interaction: discord.Interaction):
        user_id = interaction.user.id

        if await self._is_jailed(interaction):
            await interaction.response.send_message(
                "❌ Заключенные не могут участвовать в голосовании!", ephemeral=True
            )
            return

        if user_id in self.votes_release:
            await interaction.response.send_message(
                "Вы уже проголосовали за освобождение!", ephemeral=True
            )
            return

        self.votes_keep.discard(user_id)
        self.votes_release.add(user_id)
        await interaction.response.send_message(
            "✅ Ваш голос за освобождение учтен!", ephemeral=True
        )
        await self._refresh_message(interaction)

    async def vote_keep_callback(self, interaction: discord.Interaction):
        user_id = interaction.user.id

        if await self._is_jailed(interaction):
            await interaction.response.send_message(
                "❌ Заключенные не могут участвовать в голосовании!", ephemeral=True
            )
            return

        if user_id in self.votes_keep:
            await interaction.response.send_message(
                "Вы уже проголосовали против освобождения!", ephemeral=True
            )
            return

        self.votes_release.discard(user_id)
        self.votes_keep.add(user_id)
        await interaction.response.send_message(
            "❌ Ваш голос против освобождения учтен!", ephemeral=True
        )
        await self._refresh_message(interaction)

    async def on_timeout(self):
        """Вызывается когда истекает время голосования."""
        release_votes = len(self.votes_release)
        keep_votes = len(self.votes_keep)

        should_release = release_votes >= keep_votes

        # Обновляем сообщение с результатами (embed)
        if self.message:
            try:
                result_embed = build_voting_result_embed(
                    self.arrested_member, release_votes, keep_votes, should_release
                )
                await self.message.edit(embed=result_embed, view=None)
            except Exception as e:
                logger.error(f"Ошибка при обновлении сообщения голосования: {e}")

        # Если решено освободить — освобождаем (под локом ареста)
        if should_release:
            try:
                arrest_data = await db.get_active_arrest(self.arrested_member.id)
                if arrest_data:
                    guild = bot.get_guild(arrest_data['guild_id'])
                    if guild:
                        member = guild.get_member(self.arrested_member.id)
                        if member:
                            async with arrest_locks[member.id]:
                                arrest_data = await db.get_active_arrest(member.id)
                                if arrest_data:
                                    await release_arrested_member(member, arrest_data, "Апелляция одобрена")
                            cleanup_lock(arrest_locks, member.id)
            except Exception as e:
                logger.error(f"Ошибка при освобождении после апелляции: {e}")

        # Снимаем защиту от повторной подачи
        active_appeal_members.discard(self.arrested_member.id)


# ---------------------------------------------------------------------------
# Логика ареста / освобождения
# ---------------------------------------------------------------------------

async def release_arrested_member(member: discord.Member, arrest_data: Dict, reason: str):
    """Освобождает арестованного участника (роли — одним вызовом member.edit)."""
    try:
        guild = member.guild
        jail_role = guild.get_role(arrest_data['jail_role_id'])

        if not jail_role:
            logger.error(f"Роль заключенного {arrest_data['jail_role_id']} не найдена")
            await db.remove_active_arrest(member.id)
            return

        # Проверяем права бота на управление ролями
        if not guild.me.guild_permissions.manage_roles:
            logger.error(f"У бота нет прав manage_roles на сервере {guild.name}")
            return

        # Проверяем, что роль бота выше роли заключенного
        if jail_role >= guild.me.top_role:
            logger.error(f"Роль бота ниже роли заключенного на сервере {guild.name}")
            return

        # Формируем новый список ролей одним вызовом:
        # - оригинальные роли (только те, что ниже top_role бота и не managed)
        # - текущие роли, которые бот не может трогать (managed или выше top_role) — сохраняем как есть
        new_roles = []
        for role in member.roles:
            if role.is_default():
                continue
            if role.id == jail_role.id:
                continue  # снимаем роль заключенного
            if role.managed or role >= guild.me.top_role:
                new_roles.append(role)  # трогать нельзя — оставляем

        for role_id in arrest_data['original_role_ids']:
            role = guild.get_role(role_id)
            if role and role not in new_roles and not role.managed and role < guild.me.top_role:
                new_roles.append(role)

        try:
            await member.edit(roles=new_roles, reason=reason)
        except discord.Forbidden:
            logger.error(f"Нет прав для изменения ролей {member.display_name}")
            return
        except Exception as e:
            logger.error(f"Ошибка при возврате ролей {member.display_name}: {e}")
            return

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
        await db.remove_active_arrest(member.id)
        logger.info(f"Участник {member.display_name} освобожден: {reason}")

        # Уведомление об освобождении (зелёный embed)
        try:
            guild_config = await get_guild_config(guild.id)
            notification_channel_id = guild_config.get('arrest_notification_channel_id')
            if notification_channel_id:
                notification_channel = guild.get_channel(notification_channel_id)
                if notification_channel:
                    await notification_channel.send(embed=build_release_embed(member, reason))
        except Exception as e:
            logger.warning(f"Не удалось отправить уведомление об освобождении: {e}")

    except Exception as e:
        logger.error(f"Ошибка при освобождении участника: {e}")
        # Все равно удаляем из БД
        await db.remove_active_arrest(member.id)


async def arrest_member(
    member: discord.Member,
    duration: int,
    guild: discord.Guild,
    admin: discord.Member
) -> bool:
    """Арестовывает участника на указанное время."""

    lock = arrest_locks[member.id]
    if lock.locked():
        logger.warning(f"Попытка повторного ареста {member.display_name}")
        return False

    try:
        async with lock:
            # Проверяем, не арестован ли уже пользователь
            if await db.get_active_arrest(member.id):
                logger.warning(f"{member.display_name} уже арестован")
                return False

            try:
                # Получаем настройки гильдии
                guild_config = await get_guild_config(guild.id)

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
                original_role_ids = [role.id for role in member.roles if not role.is_default()]

                # Сохраняем в БД (получаем момент освобождения)
                release_at = await db.save_active_arrest(
                    member.id,
                    guild.id,
                    original_channel_id,
                    original_role_ids,
                    jail_role.id,
                    duration
                )

                # Меняем роли одним вызовом member.edit:
                # оставляем managed-роли и роли выше top_role бота (их трогать нельзя),
                # снимаем все остальные и добавляем роль заключенного
                new_roles = [
                    role for role in member.roles
                    if not role.is_default() and (role.managed or role >= guild.me.top_role)
                ]
                new_roles.append(jail_role)

                try:
                    await member.edit(
                        roles=new_roles,
                        reason=f"Арестован администратором {admin.display_name}"
                    )
                except discord.Forbidden:
                    logger.error(f"Нет прав для изменения ролей {member.display_name}")
                    await db.remove_active_arrest(member.id)
                    return False

                # Перемещаем в канал тюрьмы (только если участник в войсе)
                if member.voice:
                    try:
                        await member.move_to(jail_channel, reason=f"Арестован администратором {admin.display_name}")
                    except discord.Forbidden:
                        logger.warning(f"Нет прав для перемещения {member.display_name}")
                    except Exception as e:
                        logger.error(f"Ошибка при перемещении в тюрьму: {e}")

                # Отправляем уведомление об аресте (красный embed + persistent-кнопка апелляции)
                notification_channel_id = guild_config.get('arrest_notification_channel_id')
                if notification_channel_id:
                    notification_channel = guild.get_channel(notification_channel_id)
                    if notification_channel:
                        voting_durations = guild_config.get('appeal_voting_durations', {})
                        voting_time = voting_durations.get(str(duration), 0)

                        label = duration_label(guild_config, duration)
                        if voting_time == 0:
                            appeal_info = "⚠️ Апелляция недоступна для данного срока ареста."
                            appeal_view = None
                        else:
                            appeal_info = "Вы можете подать апелляцию, нажав кнопку ниже."
                            appeal_view = View(timeout=None)
                            appeal_view.add_item(AppealButton(member.id))

                        embed = build_arrest_embed(member, admin, label, release_at, appeal_info)
                        try:
                            if appeal_view is not None:
                                await notification_channel.send(
                                    content=member.mention, embed=embed, view=appeal_view
                                )
                            else:
                                await notification_channel.send(content=member.mention, embed=embed)
                        except Exception as e:
                            logger.error(f"Ошибка при отправке уведомления об аресте: {e}")

                logger.info(f"{member.display_name} арестован на {duration} секунд администратором {admin.display_name}")
                return True

            except Exception as e:
                logger.error(f"Ошибка при аресте участника: {e}")
                # Удаляем из БД в случае ошибки
                await db.remove_active_arrest(member.id)
                return False
    finally:
        cleanup_lock(arrest_locks, member.id)


async def has_admin_role(guild_id: int, member: discord.Member) -> bool:
    """Проверяет, есть ли у пользователя права администратора или одна из админских ролей."""
    # Безусловные супер-администраторы бота
    if member.id in SUPER_ADMIN_IDS:
        return True

    if member.guild_permissions.administrator:
        return True

    guild_config = await get_guild_config(guild_id)
    admin_role_ids = guild_config.get('admin_role_ids', [])

    if admin_role_ids:
        user_role_ids = {role.id for role in member.roles}
        return any(role_id in user_role_ids for role_id in admin_role_ids)

    return False


async def validate_bot_configuration(guild_id: int) -> tuple[bool, str]:
    """Проверяет, что бот настроен для использования команды ареста."""
    guild_config = await get_guild_config(guild_id)

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


async def release_expired_arrest(arrest_data: Dict):
    """Освободить участника по просроченному аресту (под локом)."""
    member_id = arrest_data['member_id']
    guild = bot.get_guild(arrest_data['guild_id'])
    if not guild:
        await db.remove_active_arrest(member_id)
        return

    member = guild.get_member(member_id)
    if not member:
        logger.info(f"Пользователь {member_id} покинул сервер, удаляем из списка арестованных")
        await db.remove_active_arrest(member_id)
        return

    lock = arrest_locks[member_id]
    if lock.locked():
        # Кто-то уже работает с этим участником — пропускаем до следующей итерации
        return

    async with lock:
        # Перепроверяем под локом
        fresh = await db.get_active_arrest(member_id)
        if fresh:
            await release_arrested_member(member, fresh, "Срок ареста истек")
    cleanup_lock(arrest_locks, member_id)


@tasks.loop(seconds=10)
async def check_expired_arrests():
    """Единственный механизм освобождения: проверка просроченных арестов каждые 10 сек."""
    try:
        expired = await db.get_expired_arrests()
        for arrest_data in expired:
            await release_expired_arrest(arrest_data)
    except Exception as e:
        logger.error(f"Ошибка в check_expired_arrests: {e}")


@check_expired_arrests.before_loop
async def before_check_expired_arrests():
    await bot.wait_until_ready()


# ---------------------------------------------------------------------------
# События
# ---------------------------------------------------------------------------

@bot.event
async def on_message(message: discord.Message):
    """Отправляет картинку при отдельном упоминании Lordkorvin.

    Префиксные команды пропускаются, чтобы упоминание в аргументах вроде
    ``!арест @lordkorvin`` не запускало автоматическую реакцию. Slash-команды
    приходят через interactions и сюда не попадают.
    """
    if message.author.bot:
        return

    try:
        is_prefix_command = message.content.startswith(config['command_prefix'])
        mentions_lordkorvin = any(
            user.id == LORDKORVIN_ID for user in message.mentions
        )

        if not is_prefix_command and mentions_lordkorvin:
            await message.channel.send(file=discord.File(LORDKORVIN_IMAGE))
    except Exception:
        logger.exception('Не удалось обработать упоминание Lordkorvin')
    finally:
        # Не перехватываем стандартную обработку !-команд.
        await bot.process_commands(message)


@bot.event
async def on_ready():
    """Событие при запуске бота."""
    logger.info(f'Бот {bot.user} успешно запущен!')
    logger.info(f'ID бота: {bot.user.id}')

    # Мгновенная синхронизация slash-команд на всех серверах
    # (глобальная синхронизация из setup_hook применяется с задержкой до ~1 часа)
    await bot.sync_guild_commands()

    # Запускаем фоновую задачу проверки просроченных арестов
    # (первая итерация цикла выполнится сразу — просроченные аресты будут освобождены)
    if not check_expired_arrests.is_running():
        check_expired_arrests.start()

    logger.info('Готов к работе!')


@bot.event
async def on_guild_join(guild: discord.Guild):
    """Событие при добавлении бота на сервер."""
    # Мгновенно синхронизируем slash-команды на новом сервере
    try:
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        logger.info(f'Синхронизировано {len(synced)} slash-команд на новом сервере {guild.name}')
    except Exception as e:
        logger.error(f'Ошибка при синхронизации команд на сервере {guild.name}: {e}')

    await asyncio.sleep(8)

    target_channel = None
    if guild.system_channel and guild.system_channel.permissions_for(guild.me).send_messages:
        target_channel = guild.system_channel
    else:
        for channel in guild.text_channels:
            if channel.permissions_for(guild.me).send_messages:
                target_channel = channel
                break

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
async def on_guild_remove(guild: discord.Guild):
    """Событие при удалении бота с сервера: чистим кэш настроек."""
    invalidate_guild_cache(guild.id)
    logger.info(f'Бот удален с сервера {guild.name} (ID: {guild.id}), кэш настроек очищен')


# ---------------------------------------------------------------------------
# Slash-команды
# ---------------------------------------------------------------------------

async def duration_autocomplete(
    interaction: discord.Interaction,
    current: str
) -> List[app_commands.Choice[str]]:
    """Autocomplete по пресетам длительности ареста гильдии (label -> seconds)."""
    try:
        guild_config = await get_guild_config(interaction.guild_id)
    except Exception:
        return []
    durations = guild_config.get('arrest_durations', [])
    current_lower = (current or "").lower()
    choices = []
    for d in durations:
        label = d.get('label', '')
        seconds = d.get('seconds', 0)
        if current_lower in label.lower() or current_lower in str(seconds):
            choices.append(app_commands.Choice(name=label, value=str(seconds)))
    return choices[:25]


@bot.tree.command(name="jail-config", description="Открыть панель настроек бота")
async def jail_config(interaction: discord.Interaction):
    """Открыть панель настроек бота."""

    # Чтобы не получить Unknown interaction при долгой обработке
    await interaction.response.defer(ephemeral=True, thinking=True)

    # Проверяем права доступа
    if not await has_admin_role(interaction.guild_id, interaction.user):
        await send_interaction_message(
            interaction,
            "❌ У вас нет прав для использования этой команды!",
            ephemeral=True
        )
        return

    guild_settings = await db.get_or_create_guild_settings(interaction.guild_id)

    draft = ConfigDraft(interaction.guild_id, guild_settings)
    panel = MainConfigPanel(bot, draft, interaction.user.id)
    embed, view = panel.get_current_screen()

    panel.message = await interaction.followup.send(
        embed=embed,
        view=view,
        ephemeral=True,
        wait=True
    )


@bot.tree.command(name="arrest", description="Арестовать участника на указанный срок")
@app_commands.describe(
    member="Участник, которого нужно арестовать",
    duration="Срок ареста (выберите пресет)"
)
@app_commands.autocomplete(duration=duration_autocomplete)
async def arrest_slash(interaction: discord.Interaction, member: discord.Member, duration: str):
    """Slash-команда ареста участника."""
    if not await has_admin_role(interaction.guild_id, interaction.user):
        await interaction.response.send_message(
            "❌ У вас нет прав для использования этой команды!", ephemeral=True
        )
        return

    is_configured, error_message = await validate_bot_configuration(interaction.guild_id)
    if not is_configured:
        await interaction.response.send_message(error_message, ephemeral=True)
        return

    if member.bot:
        await interaction.response.send_message("❌ Нельзя арестовать бота!", ephemeral=True)
        return

    if member.id == interaction.user.id:
        await interaction.response.send_message("❌ Нельзя арестовать самого себя!", ephemeral=True)
        return

    # Парсим длительность (autocomplete возвращает seconds строкой)
    try:
        duration_seconds = int(duration.strip())
    except ValueError:
        # Может быть введен label пресета вручную
        guild_config = await get_guild_config(interaction.guild_id)
        duration_seconds = None
        for d in guild_config.get('arrest_durations', []):
            if d.get('label', '').lower() == duration.strip().lower():
                duration_seconds = d['seconds']
                break
        if duration_seconds is None:
            await interaction.response.send_message(
                "❌ Некорректный срок ареста! Выберите пресет из списка.", ephemeral=True
            )
            return

    if duration_seconds <= 0:
        await interaction.response.send_message(
            "❌ Срок ареста должен быть положительным!", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    guild_config = await get_guild_config(interaction.guild_id)
    label = duration_label(guild_config, duration_seconds)

    success = await arrest_member(member, duration_seconds, interaction.guild, interaction.user)

    if success:
        await interaction.followup.send(
            f"✅ {member.display_name} арестован на {label}!", ephemeral=True
        )
    else:
        await interaction.followup.send(
            f"❌ Не удалось арестовать {member.display_name}. Возможно, он уже арестован или у бота не хватает прав.",
            ephemeral=True
        )


@bot.tree.command(name="release", description="Досрочно освободить арестованного участника")
@app_commands.describe(member="Участник, которого нужно освободить")
async def release_slash(interaction: discord.Interaction, member: discord.Member):
    """Slash-команда досрочного освобождения."""
    if not await has_admin_role(interaction.guild_id, interaction.user):
        await interaction.response.send_message(
            "❌ У вас нет прав для использования этой команды!", ephemeral=True
        )
        return

    arrest_data = await db.get_active_arrest(member.id)
    if not arrest_data:
        await interaction.response.send_message(
            f"❌ {member.display_name} не находится под арестом!", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    async with arrest_locks[member.id]:
        arrest_data = await db.get_active_arrest(member.id)
        if arrest_data:
            await release_arrested_member(
                member, arrest_data, f"Досрочно освобожден {interaction.user.display_name}"
            )
    cleanup_lock(arrest_locks, member.id)

    await interaction.followup.send(
        f"✅ {member.display_name} досрочно освобожден!", ephemeral=True
    )


@bot.tree.command(name="sleep", description="Отключить участника из голосового канала")
@app_commands.describe(member="Участник (необязательно — без него будет показан список)")
async def sleep_slash(interaction: discord.Interaction, member: Optional[discord.Member] = None):
    """Slash-команда отключения участника из голосового канала."""
    if not await has_admin_role(interaction.guild_id, interaction.user):
        await interaction.response.send_message(
            "❌ У вас нет прав для использования этой команды!", ephemeral=True
        )
        return

    if member is not None:
        if not member.voice or not member.voice.channel:
            await interaction.response.send_message(
                f"❌ {member.display_name} не находится в голосовом канале!", ephemeral=True
            )
            return

        try:
            await member.move_to(None, reason=f"Отключен командой /sleep от {interaction.user.display_name}")
            await interaction.response.send_message(
                f"✅ {member.display_name} отключен из голосового канала!", ephemeral=True
            )
            logger.info(f"{member.display_name} отключен из голосового канала администратором {interaction.user.display_name}")
        except discord.Forbidden:
            await interaction.response.send_message(
                f"❌ Нет прав для отключения {member.display_name}!", ephemeral=True
            )
            logger.error(f"Нет прав для отключения {member.display_name}")
        except Exception as e:
            await interaction.response.send_message(
                f"❌ Ошибка при отключении {member.display_name}!", ephemeral=True
            )
            logger.error(f"Ошибка при отключении {member.display_name}: {e}")
        return

    # Участник не указан — показываем Select со всеми участниками голосовых каналов
    members_in_voice = []
    for voice_channel in interaction.guild.voice_channels:
        for member_in_channel in voice_channel.members:
            if not member_in_channel.bot:
                members_in_voice.append(member_in_channel)

    if not members_in_voice:
        await interaction.response.send_message(
            "❌ На сервере нет пользователей в голосовых каналах!", ephemeral=True
        )
        return

    view = SleepMemberSelectView(members_in_voice, interaction.user)
    await interaction.response.send_message(
        "😴 Кого отключить из голосового канала?", view=view, ephemeral=True
    )


# ---------------------------------------------------------------------------
# Префиксные команды (сохранены для совместимости)
# ---------------------------------------------------------------------------

@bot.command(name='арест')
async def arrest_command(ctx: commands.Context):
    """Команда для ареста участника голосового канала."""

    if not await has_admin_role(ctx.guild.id, ctx.author):
        await ctx.send("❌ У вас нет прав для использования этой команды!")
        return

    is_configured, error_message = await validate_bot_configuration(ctx.guild.id)
    if not is_configured:
        await ctx.send(error_message)
        return

    # Проверяем, находится ли админ в голосовом канале
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("❌ Вы не в голосовом чате!")
        return

    voice_channel = ctx.author.voice.channel

    members = [
        member for member in voice_channel.members
        if member.id != ctx.author.id and not member.bot
    ]

    if not members:
        await ctx.send("❌ В голосовом канале нет других участников для ареста!")
        return

    # Настройки загружаем заранее — конструктор View не обращается к БД
    guild_config = await get_guild_config(ctx.guild.id)

    view = MemberSelectView(members, ctx.author, ctx.guild.id, guild_config)
    await ctx.send("👮 Кого арестовать?", view=view)


@arrest_command.error
async def arrest_command_error(ctx: commands.Context, error):
    """Обработка ошибок команды арест."""
    await ctx.send(f"❌ Произошла ошибка: {str(error)}")
    logger.error(f"Ошибка в команде арест: {error}", exc_info=error)


@bot.command(name='освободить')
async def release_command(ctx: commands.Context, member: discord.Member):
    """Команда для досрочного освобождения участника."""

    if not await has_admin_role(ctx.guild.id, ctx.author):
        await ctx.send("❌ У вас нет прав для использования этой команды!")
        return

    arrest_data = await db.get_active_arrest(member.id)
    if not arrest_data:
        await ctx.send(f"❌ {member.display_name} не находится под арестом!")
        return

    async with arrest_locks[member.id]:
        arrest_data = await db.get_active_arrest(member.id)
        if arrest_data:
            await release_arrested_member(member, arrest_data, f"Досрочно освобожден {ctx.author.display_name}")
    cleanup_lock(arrest_locks, member.id)

    await ctx.send(f"✅ {member.display_name} досрочно освобожден!")


@bot.command(name='спать')
async def sleep_command(ctx: commands.Context, member: Optional[discord.Member] = None):
    """Команда для отключения пользователя из голосового канала."""

    if not await has_admin_role(ctx.guild.id, ctx.author):
        await ctx.send("❌ У вас нет прав для использования этой команды!")
        return

    if member:
        if not member.voice or not member.voice.channel:
            await ctx.send(f"❌ {member.display_name} не находится в голосовом канале!")
            return

        try:
            await member.move_to(None, reason=f"Отключен командой !спать от {ctx.author.display_name}")
            await ctx.send(f"✅ {member.display_name} отключен из голосового канала!")
            logger.info(f"{member.display_name} отключен из голосового канала администратором {ctx.author.display_name}")
        except discord.Forbidden:
            await ctx.send(f"❌ Нет прав для отключения {member.display_name}!")
            logger.error(f"Нет прав для отключения {member.display_name}")
        except Exception as e:
            await ctx.send(f"❌ Ошибка при отключении {member.display_name}!")
            logger.error(f"Ошибка при отключении {member.display_name}: {e}")
    else:
        members_in_voice = []
        for voice_channel in ctx.guild.voice_channels:
            for member_in_channel in voice_channel.members:
                if not member_in_channel.bot:
                    members_in_voice.append(member_in_channel)

        if not members_in_voice:
            await ctx.send("❌ На сервере нет пользователей в голосовых каналах!")
            return

        view = SleepMemberSelectView(members_in_voice, ctx.author)
        await ctx.send("😴 Кого отключить из голосового канала?", view=view)


@sleep_command.error
async def sleep_command_error(ctx: commands.Context, error):
    """Обработка ошибок команды спать."""
    if isinstance(error, commands.MemberNotFound):
        await ctx.send("❌ Пользователь не найден!")
    else:
        await ctx.send(f"❌ Произошла ошибка: {str(error)}")
        logger.error(f"Ошибка в команде спать: {error}", exc_info=error)


# Добавляем ссылку на БД в бот для доступа из UI
bot.db = db
bot.invalidate_guild_cache = invalidate_guild_cache

# Запуск бота
if __name__ == "__main__":
    bot.run(config['bot_token'])
