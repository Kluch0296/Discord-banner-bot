import discord
from discord.ext import commands
from discord.ui import Button, View
import json
import asyncio
from typing import Dict, List, Optional

# Загрузка конфигурации
with open('config.json', 'r', encoding='utf-8') as f:
    config = json.load(f)

# Настройка intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True
intents.guilds = True

# Создание бота
bot = commands.Bot(command_prefix=config['command_prefix'], intents=intents)

# Словарь для хранения информации об арестованных пользователях
arrested_users: Dict[int, Dict] = {}

# Словарь для хранения активных апелляций
active_appeals: Dict[int, Dict] = {}


class MemberSelectView(View):
    """View для выбора участника для ареста"""
    
    def __init__(self, members: List[discord.Member], admin: discord.Member):
        super().__init__(timeout=60)
        self.selected_member: Optional[discord.Member] = None
        self.admin = admin
        
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
            time_view = TimeSelectView(member, self.admin)
            await interaction.response.edit_message(
                content=f"На какой срок арестовать {member.display_name}?",
                view=time_view
            )
            self.stop()
        
        return callback


class TimeSelectView(View):
    """View для выбора времени ареста"""
    
    def __init__(self, target_member: discord.Member, admin: discord.Member):
        super().__init__(timeout=60)
        self.target_member = target_member
        self.admin = admin
        
        # Получаем варианты времени из конфига
        arrest_durations = config.get('arrest_durations', [])
        
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
    
    def __init__(self, arrested_member: discord.Member, arrest_duration: int):
        super().__init__(timeout=None)
        self.arrested_member = arrested_member
        self.arrest_duration = arrest_duration
        
        # Проверяем, доступна ли апелляция для данного срока
        voting_durations = config.get('appeal_voting_durations', {})
        voting_time = voting_durations.get(str(arrest_duration), 0)
        if voting_time == 0:
            # Апелляция недоступна для 30 секунд
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
            'duration': self.arrest_duration
        }


class AppealVotingView(View):
    """View с кнопками голосования за/против освобождения"""
    
    def __init__(self, arrested_member: discord.Member, voting_duration: int):
        super().__init__(timeout=voting_duration)
        self.arrested_member = arrested_member
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
        jail_role_id = config.get('jail_role_id')
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
        jail_role_id = config.get('jail_role_id')
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
            except:
                pass
        
        # Если решено освободить - освобождаем
        if should_release and self.arrested_member.id in arrested_users:
            user_data = arrested_users[self.arrested_member.id]
            guild = user_data['guild']
            member = guild.get_member(self.arrested_member.id)
            
            if member:
                try:
                    # Убираем роль заключенного
                    await member.remove_roles(user_data['jail_role'], reason="Апелляция одобрена")
                    
                    # Возвращаем оригинальные роли
                    await member.add_roles(*user_data['original_roles'], reason="Апелляция одобрена")
                    
                    # Перемещаем обратно в оригинальный канал
                    if member.voice and user_data['original_channel']:
                        try:
                            await member.move_to(user_data['original_channel'], reason="Апелляция одобрена")
                        except:
                            pass
                    
                    # Удаляем из словаря арестованных
                    del arrested_users[self.arrested_member.id]
                except:
                    pass
        
        # Удаляем из активных апелляций
        if self.arrested_member.id in active_appeals:
            del active_appeals[self.arrested_member.id]


async def arrest_member(
    member: discord.Member,
    duration: int,
    guild: discord.Guild,
    admin: discord.Member
) -> bool:
    """Арестовывает участника на указанное время"""
    
    try:
        # Получаем канал тюрьмы и роль заключенного
        jail_channel = guild.get_channel(config['jail_channel_id'])
        jail_role = guild.get_role(config['jail_role_id'])
        
        if not jail_channel or not jail_role:
            print("Ошибка: канал тюрьмы или роль заключенного не найдены в конфиге")
            return False
        
        # Сохраняем текущий голосовой канал
        original_channel = member.voice.channel if member.voice else None
        
        # Сохраняем текущие роли (кроме @everyone)
        original_roles = [role for role in member.roles if role.name != "@everyone"]
        
        # Сохраняем информацию об арестованном
        arrested_users[member.id] = {
            'original_channel': original_channel,
            'original_roles': original_roles,
            'jail_role': jail_role,
            'guild': guild
        }
        
        # Убираем все роли
        await member.remove_roles(*original_roles, reason=f"Арестован администратором {admin.display_name}")
        
        # Добавляем роль заключенного
        await member.add_roles(jail_role, reason=f"Арестован администратором {admin.display_name}")
        
        # Перемещаем в канал тюрьмы
        if member.voice:
            await member.move_to(jail_channel, reason=f"Арестован администратором {admin.display_name}")
        
        # Отправляем уведомление об аресте в текстовый канал
        notification_channel_id = config.get('arrest_notification_channel_id')
        if notification_channel_id:
            notification_channel = guild.get_channel(notification_channel_id)
            if notification_channel:
                # Создаем View с кнопкой апелляции
                appeal_view = AppealButtonView(member, duration)
                
                # Формируем сообщение
                voting_durations = config.get('appeal_voting_durations', {})
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
                    print(f"Ошибка при отправке уведомления об аресте: {e}")
        
        # Запускаем таймер освобождения
        asyncio.create_task(release_member_after_timeout(member.id, duration))
        
        return True
        
    except Exception as e:
        print(f"Ошибка при аресте участника: {e}")
        return False


async def release_member_after_timeout(member_id: int, duration: int):
    """Освобождает участника после истечения времени"""
    
    await asyncio.sleep(duration)
    
    if member_id not in arrested_users:
        return
    
    user_data = arrested_users[member_id]
    guild = user_data['guild']
    member = guild.get_member(member_id)
    
    if not member:
        # Пользователь покинул сервер, удаляем из словаря
        print(f"Пользователь {member_id} покинул сервер, удаляем из списка арестованных")
        del arrested_users[member_id]
        return
    
    try:
        # Убираем роль заключенного
        await member.remove_roles(user_data['jail_role'], reason="Срок ареста истек")
        
        # Возвращаем оригинальные роли
        await member.add_roles(*user_data['original_roles'], reason="Срок ареста истек")
        
        # Перемещаем обратно в оригинальный канал только если пользователь в голосовом канале
        if member.voice and user_data['original_channel']:
            try:
                await member.move_to(user_data['original_channel'], reason="Срок ареста истек")
                print(f"Участник {member.display_name} освобожден и перемещен в {user_data['original_channel'].name}")
            except Exception as move_error:
                print(f"Не удалось переместить {member.display_name}: {move_error}")
        else:
            print(f"Участник {member.display_name} освобожден (роли восстановлены), но не в голосовом канале")
        
        # Удаляем из словаря арестованных
        del arrested_users[member_id]
        
    except Exception as e:
        print(f"Ошибка при освобождении участника: {e}")
        if member_id in arrested_users:
            del arrested_users[member_id]


def has_admin_role(ctx: commands.Context) -> bool:
    """Проверяет, есть ли у пользователя одна из админских ролей"""
    if not config['admin_role_ids']:
        # Если список ролей пуст, проверяем права администратора
        return ctx.author.guild_permissions.administrator
    
    user_role_ids = [role.id for role in ctx.author.roles]
    return any(role_id in user_role_ids for role_id in config['admin_role_ids'])


@bot.event
async def on_ready():
    """Событие при запуске бота"""
    print(f'Бот {bot.user} успешно запущен!')
    print(f'ID бота: {bot.user.id}')
    print('Готов к работе!')


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
            except:
                pass
            
            # Обновляем статус в уведомлении
            try:
                await appeal_data['message'].edit(
                    content=f"{message.author.mention}, апелляция на рассмотрении...",
                    view=None
                )
            except:
                pass
            
            # Отправляем апелляцию в канал голосования
            appeal_channel_id = config.get('appeal_voting_channel_id')
            if appeal_channel_id:
                appeal_channel = message.guild.get_channel(appeal_channel_id)
                if appeal_channel:
                    # Определяем время голосования
                    voting_durations = config.get('appeal_voting_durations', {})
                    voting_duration = voting_durations.get(str(appeal_data['duration']), 30)
                    
                    # Создаем View с кнопками голосования
                    voting_view = AppealVotingView(message.author, voting_duration)
                    
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
                        print(f"Ошибка при отправке апелляции в канал голосования: {e}")
                        # Удаляем из активных апелляций при ошибке
                        del active_appeals[message.author.id]
            
            return
    
    # Обрабатываем команды
    await bot.process_commands(message)


@bot.command(name='арест')
async def arrest_command(ctx: commands.Context):
    """Команда для ареста участника голосового канала"""
    
    # Проверяем права доступа
    if not has_admin_role(ctx):
        await ctx.send("❌ У вас нет прав для использования этой команды!")
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
    view = MemberSelectView(members, ctx.author)
    await ctx.send("👮 Кого арестовать?", view=view)


@arrest_command.error
async def arrest_command_error(ctx: commands.Context, error):
    """Обработка ошибок команды арест"""
    await ctx.send(f"❌ Произошла ошибка: {str(error)}")
    print(f"Ошибка в команде арест: {error}")


@bot.command(name='освободить')
async def release_command(ctx: commands.Context, member: discord.Member):
    """Команда для досрочного освобождения участника"""
    
    # Проверяем права доступа
    if not has_admin_role(ctx):
        await ctx.send("❌ У вас нет прав для использования этой команды!")
        return
    
    if member.id not in arrested_users:
        await ctx.send(f"❌ {member.display_name} не находится под арестом!")
        return
    
    user_data = arrested_users[member.id]
    
    try:
        # Убираем роль заключенного
        await member.remove_roles(user_data['jail_role'], reason=f"Досрочно освобожден {ctx.author.display_name}")
        
        # Возвращаем оригинальные роли
        await member.add_roles(*user_data['original_roles'], reason=f"Досрочно освобожден {ctx.author.display_name}")
        
        # Перемещаем обратно в оригинальный канал только если пользователь в голосовом канале
        moved = False
        if member.voice and user_data['original_channel']:
            try:
                await member.move_to(user_data['original_channel'], reason=f"Досрочно освобожден {ctx.author.display_name}")
                moved = True
            except Exception as move_error:
                print(f"Не удалось переместить {member.display_name}: {move_error}")
        
        # Удаляем из словаря арестованных
        del arrested_users[member.id]
        
        if moved:
            await ctx.send(f"✅ {member.display_name} досрочно освобожден и перемещен обратно!")
        else:
            await ctx.send(f"✅ {member.display_name} досрочно освобожден (роли восстановлены)!")
        
    except Exception as e:
        await ctx.send(f"❌ Ошибка при освобождении: {str(e)}")
        if member.id in arrested_users:
            del arrested_users[member.id]


# Запуск бота
if __name__ == "__main__":
    bot.run(config['bot_token'])