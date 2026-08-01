
import discord
from discord import ui
from discord.ui import Button, Select, Modal, TextInput, View
from typing import Dict, List, Optional, Any, Tuple
import asyncio
import copy
import logging

logger = logging.getLogger('jail_bot.config_ui')


class ConfigDraft:
    """Класс для хранения черновика настроек"""

    def __init__(self, guild_id: int, settings: Dict[str, Any]):
        self.guild_id = guild_id
        self.draft = copy.deepcopy(settings)
        self.original = copy.deepcopy(settings)

    def update(self, key: str, value: Any):
        """Обновить значение в черновике"""
        self.draft[key] = value

    def reset(self):
        """Сбросить черновик к оригиналу"""
        self.draft = copy.deepcopy(self.original)

    def get_draft(self) -> Dict[str, Any]:
        """Получить текущий черновик"""
        return self.draft

    def has_changes(self) -> bool:
        """Проверить, есть ли изменения"""
        return self.draft != self.original


class NavigationState:
    """Класс для управления навигацией между экранами"""

    def __init__(self):
        self.history: List[str] = ['main']  # История переходов
        self.current_screen = 'main'

    def navigate_to(self, screen: str):
        """Перейти на новый экран"""
        self.history.append(screen)
        self.current_screen = screen

    def go_back(self) -> str:
        """Вернуться на предыдущий экран"""
        if len(self.history) > 1:
            self.history.pop()
            self.current_screen = self.history[-1]
        return self.current_screen

    def reset(self):
        """Сбросить навигацию на главный экран"""
        self.history = ['main']
        self.current_screen = 'main'


class PanelView(View):
    """Общий класс View панели: on_timeout дизейблит все элементы."""

    def __init__(self, panel: 'MainConfigPanel', timeout: float = 300):
        super().__init__(timeout=timeout)
        self.panel = panel

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        message = getattr(self.panel, 'message', None)
        if message:
            try:
                await message.edit(view=self)
            except (discord.NotFound, discord.HTTPException) as e:
                logger.warning("Не удалось задизейблить панель по таймауту: %s", e)


class MainConfigPanel:
    """Главная панель настроек с навигацией"""

    def __init__(self, bot, draft: ConfigDraft, admin_id: int):
        self.bot = bot
        self.draft = draft
        self.admin_id = admin_id
        self.message: Optional[discord.Message] = None
        self.navigation = NavigationState()

    def format_summary(self) -> discord.Embed:
        """Сводка текущих настроек в виде Embed"""
        settings = self.draft.get_draft()
        guild = self.bot.get_guild(self.draft.guild_id)

        if not guild:
            return discord.Embed(
                title="❌ Ошибка",
                description="Сервер не найден",
                color=discord.Color.red()
            )

        # Форматируем каналы
        jail_channel = guild.get_channel(settings.get('jail_channel_id', 0))
        jail_channel_str = jail_channel.mention if jail_channel else "❌ Не настроен"

        notif_channel = guild.get_channel(settings.get('arrest_notification_channel_id', 0))
        notif_channel_str = notif_channel.mention if notif_channel else "❌ Не настроен"

        appeal_channel = guild.get_channel(settings.get('appeal_voting_channel_id', 0))
        appeal_channel_str = appeal_channel.mention if appeal_channel else "❌ Не настроен"

        # Форматируем роли
        jail_role = guild.get_role(settings.get('jail_role_id', 0))
        jail_role_str = jail_role.mention if jail_role else "❌ Не настроена"

        admin_roles = []
        for role_id in settings.get('admin_role_ids', []):
            role = guild.get_role(role_id)
            if role:
                admin_roles.append(role.mention)
        admin_roles_str = ", ".join(admin_roles) if admin_roles else "⚙️ Только администраторы сервера"

        presets_count = len(settings.get('arrest_durations', []))
        appeals_count = len(settings.get('appeal_voting_durations', {}))

        embed = discord.Embed(
            title="📋 Текущие настройки сервера",
            color=discord.Color.blue()
        )
        embed.add_field(
            name="Каналы",
            value=(
                f"🔒 Канал тюрьмы: {jail_channel_str}\n"
                f"📢 Канал для подачи апелляций: {notif_channel_str}\n"
                f"⚖️ Голосование по апелляциям: {appeal_channel_str}"
            ),
            inline=False
        )
        embed.add_field(
            name="Роли",
            value=(
                f"👤 Роль заключенного: {jail_role_str}\n"
                f"👮 Дополнительные админские роли: {admin_roles_str}"
            ),
            inline=False
        )
        embed.add_field(
            name="Настройки",
            value=(
                f"⏱️ Пресетов времени ареста: {presets_count}\n"
                f"🗳️ Настроек апелляций: {appeals_count}\n"
                f"🎙️ «Подтянись-ка»: {'✅ включена' if settings.get('voice_pull_enabled', False) else '⚪ выключена'}"
            ),
            inline=False
        )
        embed.set_footer(
            text="✏️ Есть несохраненные изменения!" if self.draft.has_changes()
            else "✅ Все изменения сохранены"
        )
        return embed

    def format_durations_list(self) -> discord.Embed:
        """Список пресетов в виде Embed"""
        durations = self.draft.get_draft().get('arrest_durations', [])

        embed = discord.Embed(
            title="⏱️ Сроки ареста",
            color=discord.Color.blue()
        )

        if not durations:
            embed.description = "❌ Пресеты не настроены"
            return embed

        lines = []
        for i, duration in enumerate(durations, 1):
            lines.append(f"{i}. **{duration['label']}** → {duration['seconds']} сек")
        embed.description = "\n".join(lines)
        return embed

    def format_appeals_list(self) -> discord.Embed:
        """Список настроек апелляций в виде Embed"""
        durations = self.draft.get_draft().get('arrest_durations', [])
        appeals = self.draft.get_draft().get('appeal_voting_durations', {})

        embed = discord.Embed(
            title="⚖️ Настройки апелляций",
            color=discord.Color.blue()
        )

        if not durations:
            embed.description = "❌ Сначала настройте пресеты времени ареста"
            return embed

        lines = []
        for duration in durations:
            seconds = duration['seconds']
            voting_time = appeals.get(str(seconds), 0)

            if voting_time == 0:
                status = "❌ Апелляция недоступна"
            else:
                status = f"✅ Голосование: {voting_time} сек"

            lines.append(f"• **{duration['label']}** ({seconds} сек) → {status}")

        embed.description = "\n".join(lines)
        return embed

    async def update_display(self, interaction: discord.Interaction):
        """Обновить отображение панели"""
        embed, view = self.get_current_screen()
        try:
            await interaction.response.edit_message(content=None, embed=embed, view=view)
        except (discord.InteractionResponded, discord.HTTPException) as e:
            logger.debug("edit_message через interaction не удался (%s), пробуем message.edit", e)
            if self.message:
                try:
                    await self.message.edit(content=None, embed=embed, view=view)
                except discord.HTTPException as edit_error:
                    logger.warning("Не удалось обновить панель настроек: %s", edit_error)

    async def refresh_panel_message(self):
        """Обновить сохранённое сообщение панели (после модалок/followup)."""
        if self.message:
            embed, view = self.get_current_screen()
            try:
                await self.message.edit(content=None, embed=embed, view=view)
            except discord.HTTPException as e:
                logger.warning("Не удалось обновить панель настроек: %s", e)

    def get_current_screen(self) -> Tuple[discord.Embed, View]:
        """Получить embed и view для текущего экрана"""
        screen = self.navigation.current_screen

        if screen == 'main':
            return self.format_summary(), self.get_main_view()
        elif screen == 'channels':
            embed = discord.Embed(
                title="📺 Настройка каналов",
                description="Выберите канал для настройки:",
                color=discord.Color.blue()
            )
            return embed, self.get_channels_view()
        elif screen == 'roles':
            embed = discord.Embed(
                title="👥 Настройка ролей",
                description="Выберите роль для настройки:",
                color=discord.Color.blue()
            )
            return embed, self.get_roles_view()
        elif screen == 'arrest_durations':
            return self.format_durations_list(), self.get_arrest_durations_view()
        elif screen == 'appeals':
            return self.format_appeals_list(), self.get_appeals_view()
        elif screen == 'factory_reset_confirm':
            embed = discord.Embed(
                title="🏭 Сброс к заводским настройкам",
                description=(
                    "⚠️ **Все настройки сервера будут удалены и заменены значениями по умолчанию!**\n\n"
                    "Это действие нельзя отменить. Продолжить?"
                ),
                color=discord.Color.orange()
            )
            return embed, self.get_factory_reset_confirm_view()
        else:
            return self.format_summary(), self.get_main_view()

    def get_main_view(self) -> View:
        """Получить главное меню"""
        view = PanelView(self)

        channels_btn = Button(label="📺 Каналы", style=discord.ButtonStyle.primary, row=0)
        channels_btn.callback = self.create_navigation_callback('channels')
        view.add_item(channels_btn)

        roles_btn = Button(label="👥 Роли", style=discord.ButtonStyle.primary, row=0)
        roles_btn.callback = self.create_navigation_callback('roles')
        view.add_item(roles_btn)

        durations_btn = Button(label="⏱️ Сроки ареста", style=discord.ButtonStyle.primary, row=1)
        durations_btn.callback = self.create_navigation_callback('arrest_durations')
        view.add_item(durations_btn)

        appeals_btn = Button(label="⚖️ Апелляции", style=discord.ButtonStyle.primary, row=1)
        appeals_btn.callback = self.create_navigation_callback('appeals')
        view.add_item(appeals_btn)

        voice_pull_enabled = self.draft.get_draft().get('voice_pull_enabled', False)
        voice_pull_btn = Button(
            label=f"🎙️ Подтянись-ка: {'ВКЛ' if voice_pull_enabled else 'ВЫКЛ'}",
            style=discord.ButtonStyle.success if voice_pull_enabled else discord.ButtonStyle.secondary,
            row=2
        )
        voice_pull_btn.callback = self.voice_pull_toggle_callback
        view.add_item(voice_pull_btn)

        # Кнопки управления
        save_btn = Button(label="💾 Сохранить", style=discord.ButtonStyle.success, row=3)
        save_btn.callback = self.save_callback
        view.add_item(save_btn)

        undo_btn = Button(label="↩️ Отменить изменения", style=discord.ButtonStyle.secondary, row=3)
        undo_btn.callback = self.undo_changes_callback
        view.add_item(undo_btn)

        factory_reset_btn = Button(label="🏭 Сброс к заводским", style=discord.ButtonStyle.secondary, row=4)
        factory_reset_btn.callback = self.create_navigation_callback('factory_reset_confirm')
        view.add_item(factory_reset_btn)

        close_btn = Button(label="❌ Закрыть", style=discord.ButtonStyle.danger, row=4)
        close_btn.callback = self.close_callback
        view.add_item(close_btn)

        return view

    def get_factory_reset_confirm_view(self) -> View:
        """Экран подтверждения сброса к заводским настройкам"""
        view = PanelView(self)

        confirm_btn = Button(label="Да, сбросить", style=discord.ButtonStyle.danger, row=0)
        confirm_btn.callback = self.factory_reset_confirm_callback
        view.add_item(confirm_btn)

        cancel_btn = Button(label="Отмена", style=discord.ButtonStyle.secondary, row=0)
        cancel_btn.callback = self.back_callback
        view.add_item(cancel_btn)

        return view

    def get_channels_view(self) -> View:
        """Получить меню настройки каналов"""
        view = PanelView(self)

        jail_channel_btn = Button(label="🔒 Канал тюрьмы", style=discord.ButtonStyle.primary, row=0)
        jail_channel_btn.callback = self.setup_jail_channel_callback
        view.add_item(jail_channel_btn)

        notif_channel_btn = Button(label="📢 Канал для подачи апелляций", style=discord.ButtonStyle.primary, row=0)
        notif_channel_btn.callback = self.setup_notif_channel_callback
        view.add_item(notif_channel_btn)

        appeal_channel_btn = Button(label="⚖️ Канал голосования", style=discord.ButtonStyle.primary, row=1)
        appeal_channel_btn.callback = self.setup_appeal_channel_callback
        view.add_item(appeal_channel_btn)

        back_btn = Button(label="◀️ Назад", style=discord.ButtonStyle.secondary, row=2)
        back_btn.callback = self.back_callback
        view.add_item(back_btn)

        return view

    def get_roles_view(self) -> View:
        """Получить меню настройки ролей"""
        view = PanelView(self)

        jail_role_btn = Button(label="👤 Роль заключенного", style=discord.ButtonStyle.primary, row=0)
        jail_role_btn.callback = self.setup_jail_role_callback
        view.add_item(jail_role_btn)

        admin_roles_btn = Button(label="👮 Доп. админские роли", style=discord.ButtonStyle.primary, row=0)
        admin_roles_btn.callback = self.admin_roles_callback
        view.add_item(admin_roles_btn)

        back_btn = Button(label="◀️ Назад", style=discord.ButtonStyle.secondary, row=1)
        back_btn.callback = self.back_callback
        view.add_item(back_btn)

        return view

    def get_arrest_durations_view(self) -> View:
        """Получить меню настройки сроков ареста"""
        view = PanelView(self)

        add_btn = Button(label="➕ Добавить пресет", style=discord.ButtonStyle.success, row=0)
        add_btn.callback = self.add_duration_callback
        view.add_item(add_btn)

        edit_btn = Button(label="✏️ Изменить пресет", style=discord.ButtonStyle.primary, row=0)
        edit_btn.callback = self.edit_duration_callback
        view.add_item(edit_btn)

        delete_btn = Button(label="🗑️ Удалить пресет", style=discord.ButtonStyle.danger, row=0)
        delete_btn.callback = self.delete_duration_callback
        view.add_item(delete_btn)

        back_btn = Button(label="◀️ Назад", style=discord.ButtonStyle.secondary, row=1)
        back_btn.callback = self.back_callback
        view.add_item(back_btn)

        return view

    def get_appeals_view(self) -> View:
        """Получить меню настройки апелляций"""
        view = PanelView(self)

        edit_btn = Button(label="✏️ Изменить время голосования", style=discord.ButtonStyle.primary, row=0)
        edit_btn.callback = self.edit_appeal_callback
        view.add_item(edit_btn)

        defaults_btn = Button(label="🔄 Установить значения по умолчанию", style=discord.ButtonStyle.secondary, row=0)
        defaults_btn.callback = self.set_appeal_defaults_callback
        view.add_item(defaults_btn)

        back_btn = Button(label="◀️ Назад", style=discord.ButtonStyle.secondary, row=1)
        back_btn.callback = self.back_callback
        view.add_item(back_btn)

        return view

    # Callbacks

    async def voice_pull_toggle_callback(self, interaction: discord.Interaction):
        """Переключить opt-in функцию голосового подключения."""
        if interaction.user.id != self.admin_id:
            await interaction.response.send_message(
                "❌ Только администратор может изменять настройки!",
                ephemeral=True
            )
            return

        current = self.draft.get_draft().get('voice_pull_enabled', False)
        self.draft.update('voice_pull_enabled', not current)
        await self.update_display(interaction)

    def create_navigation_callback(self, screen: str):
        """Создать callback для навигации"""
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.admin_id:
                await interaction.response.send_message(
                    "❌ Только администратор, открывший панель, может изменять настройки!",
                    ephemeral=True
                )
                return

            self.navigation.navigate_to(screen)
            await self.update_display(interaction)

        return callback

    async def back_callback(self, interaction: discord.Interaction):
        """Callback для кнопки Назад"""
        if interaction.user.id != self.admin_id:
            await interaction.response.send_message(
                "❌ Только администратор может изменять настройки!",
                ephemeral=True
            )
            return

        self.navigation.go_back()
        await self.update_display(interaction)

    async def save_callback(self, interaction: discord.Interaction):
        """Callback для сохранения настроек"""
        if interaction.user.id != self.admin_id:
            await interaction.response.send_message(
                "❌ Только администратор может сохранять настройки!",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        # Валидация
        validation_errors = self.validate_settings()
        if validation_errors:
            await interaction.followup.send(
                "❌ **Ошибки валидации:**\n" + "\n".join(f"• {err}" for err in validation_errors),
                ephemeral=True
            )
            return

        # Сохраняем в БД (async API)
        try:
            await self.bot.db.update_guild_settings(self.draft.guild_id, self.draft.get_draft())
            self.draft.original = copy.deepcopy(self.draft.draft)

            if hasattr(self.bot, 'invalidate_guild_cache'):
                self.bot.invalidate_guild_cache(self.draft.guild_id)

            # Настраиваем права доступа для роли заключённого в фоне
            asyncio.create_task(self.configure_jail_role_permissions(interaction.guild))

            await interaction.followup.send(
                "✅ **Настройки сохранены, права каналов настраиваются в фоне.**",
                ephemeral=True
            )

            await self.refresh_panel_message()
        except Exception as e:
            logger.error("Ошибка при сохранении настроек: %s", e)
            await interaction.followup.send(
                f"❌ **Ошибка при сохранении:** {str(e)}",
                ephemeral=True
            )

    async def undo_changes_callback(self, interaction: discord.Interaction):
        """Callback для отмены несохранённых изменений (без обращения к БД)"""
        if interaction.user.id != self.admin_id:
            await interaction.response.send_message(
                "❌ Только администратор может изменять настройки!",
                ephemeral=True
            )
            return

        self.draft.reset()
        await self.update_display(interaction)

    async def factory_reset_confirm_callback(self, interaction: discord.Interaction):
        """Callback подтверждения сброса к заводским настройкам"""
        if interaction.user.id != self.admin_id:
            await interaction.response.send_message(
                "❌ Только администратор может сбрасывать настройки!",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            # Удаляем все настройки сервера из БД и создаём по умолчанию
            await self.bot.db.delete_guild_settings(self.draft.guild_id)
            default_settings = await self.bot.db.create_default_guild_settings(self.draft.guild_id)

            if hasattr(self.bot, 'invalidate_guild_cache'):
                self.bot.invalidate_guild_cache(self.draft.guild_id)

            # Обновляем черновик с новыми настройками по умолчанию
            self.draft.draft = copy.deepcopy(default_settings)
            self.draft.original = copy.deepcopy(default_settings)

            # Возвращаемся на главный экран
            self.navigation.reset()

            await interaction.followup.send(
                "🏭 **Все настройки сервера удалены и сброшены к заводским значениям!**",
                ephemeral=True
            )

            await self.refresh_panel_message()
        except Exception as e:
            logger.error("Ошибка при сбросе настроек: %s", e)
            await interaction.followup.send(
                f"❌ **Ошибка при сбросе настроек:** {str(e)}",
                ephemeral=True
            )

    async def close_callback(self, interaction: discord.Interaction):
        """Callback для закрытия панели"""
        if interaction.user.id != self.admin_id:
            await interaction.response.send_message(
                "❌ Только администратор может закрыть панель!",
                ephemeral=True
            )
            return

        if self.draft.has_changes():
            await interaction.response.send_message(
                "⚠️ **У вас есть несохраненные изменения!**\nСохраните их перед закрытием.",
                ephemeral=True
            )
            return

        await interaction.response.send_message(
            "✅ Панель настроек закрыта.",
            ephemeral=True
        )

        if self.message:
            try:
                await self.message.delete()
            except discord.HTTPException as e:
                logger.warning("Не удалось удалить сообщение панели: %s", e)

    async def admin_roles_callback(self, interaction: discord.Interaction):
        """Callback для настройки админских ролей"""
        if interaction.user.id != self.admin_id:
            await interaction.response.send_message(
                "❌ Только администратор может изменять настройки!",
                ephemeral=True
            )
            return

        guild = self.bot.get_guild(self.draft.guild_id)
        if not guild:
            await interaction.response.send_message("❌ Ошибка: сервер не найден", ephemeral=True)
            return

        # Создаем view с role select
        view = PanelView(self, timeout=180)
        role_select = ui.RoleSelect(
            placeholder="Выберите админские роли...",
            min_values=1,
            max_values=10
        )

        async def role_select_callback(select_interaction: discord.Interaction):
            if select_interaction.user.id != self.admin_id:
                await select_interaction.response.send_message(
                    "❌ Только администратор может изменять настройки!",
                    ephemeral=True
                )
                return

            selected_role_ids = [role.id for role in role_select.values]
            self.draft.update('admin_role_ids', selected_role_ids)

            # Возвращаемся назад и обновляем панель
            self.navigation.go_back()
            embed, new_view = self.get_current_screen()

            await select_interaction.response.edit_message(
                content=None,
                embed=embed,
                view=new_view
            )

        role_select.callback = role_select_callback
        view.add_item(role_select)

        back_btn = Button(label="◀️ Назад", style=discord.ButtonStyle.secondary, row=1)
        back_btn.callback = self.back_callback
        view.add_item(back_btn)

        embed = discord.Embed(
            title="👮 Выбор дополнительных админских ролей",
            description=(
                "Выберите роли, которые смогут использовать команды бота (опционально).\n"
                "💡 По умолчанию команды доступны всем администраторам сервера."
            ),
            color=discord.Color.blue()
        )
        await interaction.response.edit_message(content=None, embed=embed, view=view)

    def create_channel_callback(self, setting_key: str, setting_name: str):
        """Создать callback для выбора канала"""
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.admin_id:
                await interaction.response.send_message(
                    "❌ Только администратор может изменять настройки!",
                    ephemeral=True
                )
                return

            select = interaction.data['values'][0]
            channel_id = int(select)

            self.draft.update(setting_key, channel_id)

            # Возвращаемся назад и обновляем панель
            self.navigation.go_back()
            embed, view = self.get_current_screen()

            await interaction.response.edit_message(content=None, embed=embed, view=view)

        return callback

    async def jail_role_callback(self, interaction: discord.Interaction):
        """Callback для выбора роли заключенного"""
        if interaction.user.id != self.admin_id:
            await interaction.response.send_message(
                "❌ Только администратор может изменять настройки!",
                ephemeral=True
            )
            return

        role = interaction.data['values'][0]
        role_id = int(role)

        self.draft.update('jail_role_id', role_id)

        # Возвращаемся назад и обновляем панель
        self.navigation.go_back()
        embed, view = self.get_current_screen()

        await interaction.response.edit_message(content=None, embed=embed, view=view)

    async def setup_jail_channel_callback(self, interaction: discord.Interaction):
        """Callback для настройки канала тюрьмы"""
        if interaction.user.id != self.admin_id:
            await interaction.response.send_message(
                "❌ Только администратор может изменять настройки!",
                ephemeral=True
            )
            return

        view = PanelView(self, timeout=180)

        jail_channel_select = ui.ChannelSelect(
            placeholder="🔒 Выберите существующий канал тюрьмы...",
            channel_types=[discord.ChannelType.voice],
            row=0
        )
        jail_channel_select.callback = self.create_channel_callback('jail_channel_id', 'Канал тюрьмы')
        view.add_item(jail_channel_select)

        create_btn = Button(label="➕ Создать новый канал", style=discord.ButtonStyle.success, row=1)
        create_btn.callback = self.create_jail_channel_callback
        view.add_item(create_btn)

        back_btn = Button(label="◀️ Назад", style=discord.ButtonStyle.secondary, row=2)
        back_btn.callback = self.back_callback
        view.add_item(back_btn)

        embed = discord.Embed(
            title="🔒 Настройка канала тюрьмы",
            description="Выберите существующий голосовой канал или создайте новый:",
            color=discord.Color.blue()
        )
        await interaction.response.edit_message(content=None, embed=embed, view=view)

    async def setup_notif_channel_callback(self, interaction: discord.Interaction):
        """Callback для настройки канала уведомлений"""
        if interaction.user.id != self.admin_id:
            await interaction.response.send_message(
                "❌ Только администратор может изменять настройки!",
                ephemeral=True
            )
            return

        view = PanelView(self, timeout=180)

        notif_channel_select = ui.ChannelSelect(
            placeholder="📢 Выберите существующий канал для подачи апелляций...",
            channel_types=[discord.ChannelType.text],
            row=0
        )
        notif_channel_select.callback = self.create_channel_callback('arrest_notification_channel_id', 'Канал для подачи апелляций')
        view.add_item(notif_channel_select)

        create_btn = Button(label="➕ Создать новый канал", style=discord.ButtonStyle.success, row=1)
        create_btn.callback = self.create_notification_channel_callback
        view.add_item(create_btn)

        back_btn = Button(label="◀️ Назад", style=discord.ButtonStyle.secondary, row=2)
        back_btn.callback = self.back_callback
        view.add_item(back_btn)

        embed = discord.Embed(
            title="📢 Настройка канала для подачи апелляций",
            description="Выберите существующий текстовый канал или создайте новый:",
            color=discord.Color.blue()
        )
        await interaction.response.edit_message(content=None, embed=embed, view=view)

    async def setup_appeal_channel_callback(self, interaction: discord.Interaction):
        """Callback для настройки канала голосования"""
        if interaction.user.id != self.admin_id:
            await interaction.response.send_message(
                "❌ Только администратор может изменять настройки!",
                ephemeral=True
            )
            return

        view = PanelView(self, timeout=180)

        appeal_channel_select = ui.ChannelSelect(
            placeholder="⚖️ Выберите существующий канал голосования...",
            channel_types=[discord.ChannelType.text],
            row=0
        )
        appeal_channel_select.callback = self.create_channel_callback('appeal_voting_channel_id', 'Канал голосования')
        view.add_item(appeal_channel_select)

        create_btn = Button(label="➕ Создать новый канал", style=discord.ButtonStyle.success, row=1)
        create_btn.callback = self.create_appeal_channel_callback
        view.add_item(create_btn)

        back_btn = Button(label="◀️ Назад", style=discord.ButtonStyle.secondary, row=2)
        back_btn.callback = self.back_callback
        view.add_item(back_btn)

        embed = discord.Embed(
            title="⚖️ Настройка канала голосования",
            description="Выберите существующий текстовый канал или создайте новый:",
            color=discord.Color.blue()
        )
        await interaction.response.edit_message(content=None, embed=embed, view=view)

    async def setup_jail_role_callback(self, interaction: discord.Interaction):
        """Callback для настройки роли заключенного"""
        if interaction.user.id != self.admin_id:
            await interaction.response.send_message(
                "❌ Только администратор может изменять настройки!",
                ephemeral=True
            )
            return

        view = PanelView(self, timeout=180)

        jail_role_select = ui.RoleSelect(
            placeholder="👤 Выберите существующую роль заключенного...",
            row=0
        )
        jail_role_select.callback = self.jail_role_callback
        view.add_item(jail_role_select)

        create_btn = Button(label="➕ Создать новую роль", style=discord.ButtonStyle.success, row=1)
        create_btn.callback = self.create_jail_role_callback
        view.add_item(create_btn)

        back_btn = Button(label="◀️ Назад", style=discord.ButtonStyle.secondary, row=2)
        back_btn.callback = self.back_callback
        view.add_item(back_btn)

        embed = discord.Embed(
            title="👤 Настройка роли заключенного",
            description="Выберите существующую роль или создайте новую:",
            color=discord.Color.blue()
        )
        await interaction.response.edit_message(content=None, embed=embed, view=view)

    async def create_jail_channel_callback(self, interaction: discord.Interaction):
        """Callback для создания канала тюрьмы"""
        if interaction.user.id != self.admin_id:
            await interaction.response.send_message(
                "❌ Только администратор может изменять настройки!",
                ephemeral=True
            )
            return

        modal = CreateJailChannelModal(self)
        await interaction.response.send_modal(modal)

    async def create_notification_channel_callback(self, interaction: discord.Interaction):
        """Callback для создания канала уведомлений"""
        if interaction.user.id != self.admin_id:
            await interaction.response.send_message(
                "❌ Только администратор может изменять настройки!",
                ephemeral=True
            )
            return

        modal = CreateNotificationChannelModal(self)
        await interaction.response.send_modal(modal)

    async def create_appeal_channel_callback(self, interaction: discord.Interaction):
        """Callback для создания канала голосования"""
        if interaction.user.id != self.admin_id:
            await interaction.response.send_message(
                "❌ Только администратор может изменять настройки!",
                ephemeral=True
            )
            return

        modal = CreateAppealChannelModal(self)
        await interaction.response.send_modal(modal)

    async def create_jail_role_callback(self, interaction: discord.Interaction):
        """Callback для создания роли заключенного"""
        if interaction.user.id != self.admin_id:
            await interaction.response.send_message(
                "❌ Только администратор может изменять настройки!",
                ephemeral=True
            )
            return

        modal = CreateJailRoleModal(self)
        await interaction.response.send_modal(modal)

    async def add_duration_callback(self, interaction: discord.Interaction):
        """Callback для добавления пресета"""
        if interaction.user.id != self.admin_id:
            await interaction.response.send_message(
                "❌ Только администратор может изменять настройки!",
                ephemeral=True
            )
            return

        modal = AddDurationModal(self)
        await interaction.response.send_modal(modal)

    async def edit_duration_callback(self, interaction: discord.Interaction):
        """Callback для редактирования пресета"""
        if interaction.user.id != self.admin_id:
            await interaction.response.send_message(
                "❌ Только администратор может изменять настройки!",
                ephemeral=True
            )
            return

        durations = self.draft.get_draft().get('arrest_durations', [])
        if not durations:
            await interaction.response.send_message(
                "❌ Нет пресетов для редактирования!",
                ephemeral=True
            )
            return

        view = PanelView(self, timeout=180)
        options = [
            discord.SelectOption(
                label=f"{d['label']} ({d['seconds']} сек)",
                value=str(i)
            )
            for i, d in enumerate(durations)
        ]

        select = Select(placeholder="Выберите пресет...", options=options)

        async def select_callback(select_interaction: discord.Interaction):
            if select_interaction.user.id != self.admin_id:
                await select_interaction.response.send_message(
                    "❌ Только администратор может изменять настройки!",
                    ephemeral=True
                )
                return

            index = int(select_interaction.data['values'][0])
            duration = durations[index]

            modal = EditDurationModal(self, index, duration)
            await select_interaction.response.send_modal(modal)

        select.callback = select_callback
        view.add_item(select)

        back_btn = Button(label="◀️ Назад", style=discord.ButtonStyle.secondary, row=1)
        back_btn.callback = self.back_callback
        view.add_item(back_btn)

        embed = discord.Embed(
            title="✏️ Выберите пресет для редактирования",
            color=discord.Color.blue()
        )
        await interaction.response.edit_message(content=None, embed=embed, view=view)

    async def delete_duration_callback(self, interaction: discord.Interaction):
        """Callback для удаления пресета"""
        if interaction.user.id != self.admin_id:
            await interaction.response.send_message(
                "❌ Только администратор может изменять настройки!",
                ephemeral=True
            )
            return

        durations = self.draft.get_draft().get('arrest_durations', [])
        if not durations:
            await interaction.response.send_message(
                "❌ Нет пресетов для удаления!",
                ephemeral=True
            )
            return

        view = PanelView(self, timeout=180)
        options = [
            discord.SelectOption(
                label=f"{d['label']} ({d['seconds']} сек)",
                value=str(i)
            )
            for i, d in enumerate(durations)
        ]

        select = Select(placeholder="Выберите пресет для удаления...", options=options)

        async def select_callback(select_interaction: discord.Interaction):
            if select_interaction.user.id != self.admin_id:
                await select_interaction.response.send_message(
                    "❌ Только администратор может изменять настройки!",
                    ephemeral=True
                )
                return

            index = int(select_interaction.data['values'][0])
            duration = durations[index]

            # Удаляем пресет
            durations.pop(index)
            self.draft.update('arrest_durations', durations)

            # Удаляем соответствующую настройку апелляции
            appeals = self.draft.get_draft().get('appeal_voting_durations', {})
            if str(duration['seconds']) in appeals:
                appeals.pop(str(duration['seconds']))
                self.draft.update('appeal_voting_durations', appeals)

            await select_interaction.response.send_message(
                f"✅ **Пресет удален:** {duration['label']} ({duration['seconds']} сек)",
                ephemeral=True
            )

            await self.refresh_panel_message()

        select.callback = select_callback
        view.add_item(select)

        back_btn = Button(label="◀️ Назад", style=discord.ButtonStyle.secondary, row=1)
        back_btn.callback = self.back_callback
        view.add_item(back_btn)

        embed = discord.Embed(
            title="🗑️ Выберите пресет для удаления",
            color=discord.Color.blue()
        )
        await interaction.response.edit_message(content=None, embed=embed, view=view)

    async def edit_appeal_callback(self, interaction: discord.Interaction):
        """Callback для редактирования времени голосования"""
        if interaction.user.id != self.admin_id:
            await interaction.response.send_message(
                "❌ Только администратор может изменять настройки!",
                ephemeral=True
            )
            return

        durations = self.draft.get_draft().get('arrest_durations', [])
        if not durations:
            await interaction.response.send_message(
                "❌ Сначала настройте пресеты времени ареста!",
                ephemeral=True
            )
            return

        view = PanelView(self, timeout=180)
        options = [
            discord.SelectOption(
                label=f"{d['label']} ({d['seconds']} сек)",
                value=str(d['seconds'])
            )
            for d in durations
        ]

        select = Select(placeholder="Выберите пресет...", options=options)

        async def select_callback(select_interaction: discord.Interaction):
            if select_interaction.user.id != self.admin_id:
                await select_interaction.response.send_message(
                    "❌ Только администратор может изменять настройки!",
                    ephemeral=True
                )
                return

            arrest_seconds = int(select_interaction.data['values'][0])
            appeals = self.draft.get_draft().get('appeal_voting_durations', {})
            current_voting = appeals.get(str(arrest_seconds), 0)

            modal = EditAppealVotingModal(self, arrest_seconds, current_voting)
            await select_interaction.response.send_modal(modal)

        select.callback = select_callback
        view.add_item(select)

        back_btn = Button(label="◀️ Назад", style=discord.ButtonStyle.secondary, row=1)
        back_btn.callback = self.back_callback
        view.add_item(back_btn)

        embed = discord.Embed(
            title="⚖️ Выберите пресет для настройки времени голосования",
            color=discord.Color.blue()
        )
        await interaction.response.edit_message(content=None, embed=embed, view=view)

    async def set_appeal_defaults_callback(self, interaction: discord.Interaction):
        """Callback для установки значений по умолчанию"""
        if interaction.user.id != self.admin_id:
            await interaction.response.send_message(
                "❌ Только администратор может изменять настройки!",
                ephemeral=True
            )
            return

        durations = self.draft.get_draft().get('arrest_durations', [])
        if not durations:
            await interaction.response.send_message(
                "❌ Сначала настройте пресеты времени ареста!",
                ephemeral=True
            )
            return

        # Устанавливаем значения по умолчанию
        appeals = {}
        for duration in durations:
            seconds = duration['seconds']
            # Для коротких сроков - 0, для длинных - пропорционально
            if seconds <= 30:
                default_voting = 0
            else:
                default_voting = max(15, min(120, seconds // 10))
            appeals[str(seconds)] = default_voting

        self.draft.update('appeal_voting_durations', appeals)

        await interaction.response.send_message(
            "✅ **Установлены значения по умолчанию для всех пресетов!**",
            ephemeral=True
        )

        await self.refresh_panel_message()

    def validate_settings(self) -> List[str]:
        """Валидация настроек перед сохранением (минимальная проверка)"""
        errors = []
        settings = self.draft.get_draft()
        guild = self.bot.get_guild(self.draft.guild_id)

        if not guild:
            errors.append("Сервер не найден")
            return errors

        # Проверка существования каналов (если указаны)
        if settings.get('jail_channel_id', 0) != 0:
            if not guild.get_channel(settings['jail_channel_id']):
                errors.append("Канал тюрьмы не существует на сервере")

        if settings.get('arrest_notification_channel_id', 0) != 0:
            if not guild.get_channel(settings['arrest_notification_channel_id']):
                errors.append("Канал для подачи апелляций не существует на сервере")

        if settings.get('appeal_voting_channel_id', 0) != 0:
            if not guild.get_channel(settings['appeal_voting_channel_id']):
                errors.append("Канал голосования не существует на сервере")

        # Проверка существования роли заключенного (если указана)
        if settings.get('jail_role_id', 0) != 0:
            if not guild.get_role(settings['jail_role_id']):
                errors.append("Роль заключенного не существует на сервере")

        # Проверка админских ролей (опционально, только если указаны)
        if settings.get('admin_role_ids'):
            for role_id in settings['admin_role_ids']:
                if not guild.get_role(role_id):
                    errors.append(f"Админская роль с ID {role_id} не существует на сервере")

        # Проверка пресетов (если указаны)
        if settings.get('arrest_durations'):
            seen_seconds = set()
            for duration in settings['arrest_durations']:
                if not duration.get('label'):
                    errors.append("У одного из пресетов отсутствует название")
                if duration.get('seconds', 0) <= 0:
                    errors.append(f"Пресет '{duration.get('label', 'без названия')}' имеет некорректное время")
                if duration.get('seconds') in seen_seconds:
                    errors.append(f"Дублирующееся время ареста: {duration.get('seconds')} секунд")
                seen_seconds.add(duration.get('seconds'))

        # Проверка апелляций (если указаны)
        if settings.get('arrest_durations') and settings.get('appeal_voting_durations'):
            arrest_seconds = {d['seconds'] for d in settings['arrest_durations']}
            appeal_seconds = {int(k) for k in settings['appeal_voting_durations'].keys()}

            if arrest_seconds != appeal_seconds:
                errors.append("Настройки апелляций не соответствуют пресетам времени ареста")

            for seconds_str, voting_time in settings['appeal_voting_durations'].items():
                if voting_time < 0:
                    errors.append(f"Отрицательное время голосования для пресета {seconds_str} секунд")

        return errors

    async def configure_jail_role_permissions(self, guild: discord.Guild):
        """Настройка прав доступа роли заключённого к голосовым каналам (в фоне).

        Каналы с уже корректными overwrite пропускаются.
        """
        settings = self.draft.get_draft()

        jail_role_id = settings.get('jail_role_id', 0)
        if jail_role_id == 0:
            return  # Роль не указана, ничего не делаем

        jail_role = guild.get_role(jail_role_id)
        if not jail_role:
            return  # Роль не найдена

        jail_channel_id = settings.get('jail_channel_id', 0)

        configured = 0
        skipped = 0
        for channel in guild.voice_channels:
            is_jail = channel.id == jail_channel_id
            desired = {
                'connect': True if is_jail else False,
                'speak': True if is_jail else False,
                'view_channel': True,
            }

            # Проверяем текущий overwrite — пропускаем каналы, где всё уже корректно
            current = channel.overwrites_for(jail_role)
            if (current.connect == desired['connect']
                    and current.speak == desired['speak']
                    and current.view_channel == desired['view_channel']):
                skipped += 1
                continue

            try:
                await channel.set_permissions(
                    jail_role,
                    connect=desired['connect'],
                    speak=desired['speak'],
                    view_channel=desired['view_channel'],
                    reason="Настройка прав доступа для роли заключённого"
                )
                configured += 1
            except discord.Forbidden:
                logger.warning("Нет прав для настройки канала %s", channel.name)
            except Exception as e:
                logger.error("Ошибка при настройке прав для канала %s: %s", channel.name, e)

        logger.info(
            "Права роли заключённого настроены: %d каналов обновлено, %d пропущено (уже корректны)",
            configured, skipped
        )


# Модальные окна

class AddDurationModal(Modal, title="Добавить пресет времени ареста"):
    """Модальное окно для добавления пресета"""

    label_input = TextInput(
        label="Название пресета",
        placeholder="Например: 5 минут",
        required=True,
        max_length=50
    )

    seconds_input = TextInput(
        label="Длительность в секундах",
        placeholder="Например: 300",
        required=True,
        max_length=10
    )

    def __init__(self, panel: MainConfigPanel):
        super().__init__()
        self.panel = panel

    async def on_submit(self, interaction: discord.Interaction):
        try:
            label = self.label_input.value.strip()
            seconds = int(self.seconds_input.value.strip())

            if seconds <= 0:
                await interaction.response.send_message(
                    "❌ Длительность должна быть положительным числом!",
                    ephemeral=True
                )
                return

            durations = self.panel.draft.get_draft().get('arrest_durations', [])

            # Проверяем лимит (максимум 25 пресетов)
            if len(durations) >= 25:
                await interaction.response.send_message(
                    "❌ Достигнут максимальный лимит пресетов (25)!",
                    ephemeral=True
                )
                return

            if any(d['seconds'] == seconds for d in durations):
                await interaction.response.send_message(
                    f"❌ Пресет с длительностью {seconds} секунд уже существует!",
                    ephemeral=True
                )
                return

            # Добавляем новый пресет
            durations.append({'label': label, 'seconds': seconds})
            self.panel.draft.update('arrest_durations', durations)

            # Добавляем настройку апелляции по умолчанию
            appeals = self.panel.draft.get_draft().get('appeal_voting_durations', {})
            if str(seconds) not in appeals:
                default_voting = max(0, min(120, seconds // 10))
                appeals[str(seconds)] = default_voting
                self.panel.draft.update('appeal_voting_durations', appeals)

            await interaction.response.send_message(
                f"✅ **Пресет добавлен:** {label} ({seconds} сек)",
                ephemeral=True
            )

            await self.panel.refresh_panel_message()

        except ValueError:
            await interaction.response.send_message(
                "❌ Длительность должна быть числом!",
                ephemeral=True
            )


class EditDurationModal(Modal, title="Изменить пресет"):
    """Модальное окно для редактирования пресета"""

    label_input = TextInput(
        label="Название пресета",
        required=True,
        max_length=50
    )

    seconds_input = TextInput(
        label="Длительность в секундах",
        required=True,
        max_length=10
    )

    def __init__(self, panel: MainConfigPanel, index: int, duration: Dict):
        super().__init__()
        self.panel = panel
        self.index = index
        self.old_seconds = duration['seconds']

        # Заполняем текущие значения
        self.label_input.default = duration['label']
        self.seconds_input.default = str(duration['seconds'])

    async def on_submit(self, interaction: discord.Interaction):
        try:
            label = self.label_input.value.strip()
            seconds = int(self.seconds_input.value.strip())

            if seconds <= 0:
                await interaction.response.send_message(
                    "❌ Длительность должна быть положительным числом!",
                    ephemeral=True
                )
                return

            durations = self.panel.draft.get_draft().get('arrest_durations', [])

            # Проверяем на дубликаты (кроме текущего)
            if any(i != self.index and d['seconds'] == seconds for i, d in enumerate(durations)):
                await interaction.response.send_message(
                    f"❌ Пресет с длительностью {seconds} секунд уже существует!",
                    ephemeral=True
                )
                return

            # Обновляем пресет
            durations[self.index] = {'label': label, 'seconds': seconds}
            self.panel.draft.update('arrest_durations', durations)

            # Обновляем настройки апелляций, если изменилось время
            if seconds != self.old_seconds:
                appeals = self.panel.draft.get_draft().get('appeal_voting_durations', {})
                if str(self.old_seconds) in appeals:
                    old_value = appeals.pop(str(self.old_seconds))
                    appeals[str(seconds)] = old_value
                    self.panel.draft.update('appeal_voting_durations', appeals)

            await interaction.response.send_message(
                f"✅ **Пресет обновлен:** {label} ({seconds} сек)",
                ephemeral=True
            )

            await self.panel.refresh_panel_message()

        except ValueError:
            await interaction.response.send_message(
                "❌ Длительность должна быть числом!",
                ephemeral=True
            )


class EditAppealVotingModal(Modal, title="Настройка времени голосования"):
    """Модальное окно для настройки времени голосования по апелляции"""

    voting_seconds_input = TextInput(
        label="Время голосования (секунды)",
        placeholder="0 = апелляция недоступна",
        required=True,
        max_length=10
    )

    def __init__(self, panel: MainConfigPanel, arrest_seconds: int, current_voting: int):
        super().__init__()
        self.panel = panel
        self.arrest_seconds = arrest_seconds

        # Заполняем текущее значение
        self.voting_seconds_input.default = str(current_voting)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            voting_seconds = int(self.voting_seconds_input.value.strip())

            if voting_seconds < 0:
                await interaction.response.send_message(
                    "❌ Время голосования не может быть отрицательным!",
                    ephemeral=True
                )
                return

            # Обновляем настройку
            appeals = self.panel.draft.get_draft().get('appeal_voting_durations', {})
            appeals[str(self.arrest_seconds)] = voting_seconds
            self.panel.draft.update('appeal_voting_durations', appeals)

            if voting_seconds == 0:
                status = "недоступна"
            else:
                status = f"доступна, голосование {voting_seconds} сек"

            await interaction.response.send_message(
                f"✅ **Апелляция для пресета {self.arrest_seconds} сек:** {status}",
                ephemeral=True
            )

            await self.panel.refresh_panel_message()

        except ValueError:
            await interaction.response.send_message(
                "❌ Время голосования должно быть числом!",
                ephemeral=True
            )


class CreateJailChannelModal(Modal, title="Создать канал тюрьмы"):
    """Модальное окно для создания голосового канала тюрьмы"""

    channel_name = TextInput(
        label="Название канала",
        placeholder="Например: 🔒-тюрьма",
        required=True,
        max_length=100,
        default="🔒-тюрьма"
    )

    def __init__(self, panel: MainConfigPanel):
        super().__init__()
        self.panel = panel

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            guild = interaction.guild
            if not guild:
                await interaction.followup.send(
                    "❌ Ошибка: сервер не найден",
                    ephemeral=True
                )
                return

            channel_name = self.channel_name.value.strip()

            # Создаем голосовой канал
            channel = await guild.create_voice_channel(
                name=channel_name,
                reason="Создан через панель настроек бота"
            )

            # Обновляем настройки
            self.panel.draft.update('jail_channel_id', channel.id)

            # Возвращаемся назад и обновляем панель
            self.panel.navigation.go_back()

            await interaction.followup.send(
                f"✅ **Канал тюрьмы создан:** {channel.mention}\n"
                f"💡 Не забудьте настроить права доступа к каналу!",
                ephemeral=True
            )
            await self.panel.refresh_panel_message()

        except discord.Forbidden:
            await interaction.followup.send(
                "❌ У бота нет прав для создания каналов!",
                ephemeral=True
            )
        except Exception as e:
            logger.error("Ошибка при создании канала тюрьмы: %s", e)
            await interaction.followup.send(
                f"❌ Ошибка при создании канала: {str(e)}",
                ephemeral=True
            )


class CreateNotificationChannelModal(Modal, title="Создать канал для подачи апелляций"):
    """Модальное окно для создания текстового канала для подачи апелляций"""

    channel_name = TextInput(
        label="Название канала",
        placeholder="Например: 📢-апелляции",
        required=True,
        max_length=100,
        default="📢-апелляции"
    )

    def __init__(self, panel: MainConfigPanel):
        super().__init__()
        self.panel = panel

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            guild = interaction.guild
            if not guild:
                await interaction.followup.send(
                    "❌ Ошибка: сервер не найден",
                    ephemeral=True
                )
                return

            channel_name = self.channel_name.value.strip()

            # Создаем текстовый канал
            channel = await guild.create_text_channel(
                name=channel_name,
                reason="Создан через панель настроек бота"
            )

            # Обновляем настройки
            self.panel.draft.update('arrest_notification_channel_id', channel.id)

            # Возвращаемся назад и обновляем панель
            self.panel.navigation.go_back()

            await interaction.followup.send(
                f"✅ **Канал для подачи апелляций создан:** {channel.mention}\n"
                f"💡 В этот канал будут отправляться уведомления об арестах и кнопки апелляций.",
                ephemeral=True
            )
            await self.panel.refresh_panel_message()

        except discord.Forbidden:
            await interaction.followup.send(
                "❌ У бота нет прав для создания каналов!",
                ephemeral=True
            )
        except Exception as e:
            logger.error("Ошибка при создании канала уведомлений: %s", e)
            await interaction.followup.send(
                f"❌ Ошибка при создании канала: {str(e)}",
                ephemeral=True
            )


class CreateAppealChannelModal(Modal, title="Создать канал голосования"):
    """Модальное окно для создания канала голосования по апелляциям"""

    channel_name = TextInput(
        label="Название канала",
        placeholder="Например: ⚖️-голосование-по-апелляциям",
        required=True,
        max_length=100,
        default="⚖️-голосование-по-апелляциям"
    )

    def __init__(self, panel: MainConfigPanel):
        super().__init__()
        self.panel = panel

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            guild = interaction.guild
            if not guild:
                await interaction.followup.send(
                    "❌ Ошибка: сервер не найден",
                    ephemeral=True
                )
                return

            channel_name = self.channel_name.value.strip()

            # Создаем текстовый канал
            channel = await guild.create_text_channel(
                name=channel_name,
                reason="Создан через панель настроек бота"
            )

            # Обновляем настройки
            self.panel.draft.update('appeal_voting_channel_id', channel.id)

            # Возвращаемся назад и обновляем панель
            self.panel.navigation.go_back()

            await interaction.followup.send(
                f"✅ **Канал голосования создан:** {channel.mention}\n"
                f"💡 В этот канал будут отправляться апелляции для голосования.",
                ephemeral=True
            )
            await self.panel.refresh_panel_message()

        except discord.Forbidden:
            await interaction.followup.send(
                "❌ У бота нет прав для создания каналов!",
                ephemeral=True
            )
        except Exception as e:
            logger.error("Ошибка при создании канала голосования: %s", e)
            await interaction.followup.send(
                f"❌ Ошибка при создании канала: {str(e)}",
                ephemeral=True
            )


class CreateJailRoleModal(Modal, title="Создать роль заключенного"):
    """Модальное окно для создания роли заключенного"""

    role_name = TextInput(
        label="Название роли",
        placeholder="Например: 🔒 Заключенный",
        required=True,
        max_length=100,
        default="🔒 Заключенный"
    )

    def __init__(self, panel: MainConfigPanel):
        super().__init__()
        self.panel = panel

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            guild = interaction.guild
            if not guild:
                await interaction.followup.send(
                    "❌ Ошибка: сервер не найден",
                    ephemeral=True
                )
                return

            role_name = self.role_name.value.strip()

            # Создаем роль с серым цветом и без прав
            role = await guild.create_role(
                name=role_name,
                color=discord.Color.dark_gray(),
                permissions=discord.Permissions.none(),
                reason="Создана через панель настроек бота"
            )

            # Обновляем настройки
            self.panel.draft.update('jail_role_id', role.id)

            # Возвращаемся назад и обновляем панель
            self.panel.navigation.go_back()

            await interaction.followup.send(
                f"✅ **Роль заключенного создана:** {role.mention}\n"
                f"💡 Рекомендации:\n"
                f"• Настройте права доступа к каналам для этой роли\n"
                f"• Убедитесь, что роль бота выше этой роли в иерархии",
                ephemeral=True
            )
            await self.panel.refresh_panel_message()

        except discord.Forbidden:
            await interaction.followup.send(
                "❌ У бота нет прав для создания ролей!",
                ephemeral=True
            )
        except Exception as e:
            logger.error("Ошибка при создании роли заключенного: %s", e)
            await interaction.followup.send(
                f"❌ Ошибка при создании роли: {str(e)}",
                ephemeral=True
            )
