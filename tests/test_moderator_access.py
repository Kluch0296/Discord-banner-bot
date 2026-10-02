import copy
import importlib
import logging
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, mock_open, patch

import discord

from config_ui import (
    ConfigDraft, MainConfigPanel, AddDurationModal, EditDurationModal,
    EditAppealVotingModal, CreateJailChannelModal, CreateNotificationChannelModal,
    CreateAppealChannelModal, CreateJailRoleModal,
)

# Импорт без рабочего токена, чтения config.json и подключения к Discord.
with patch('builtins.open', mock_open(read_data='{"bot_token":"test","command_prefix":"!"}')), \
     patch('logging.FileHandler', return_value=logging.NullHandler()), patch('logging.basicConfig'):
    bot_module = importlib.import_module('bot')


SETTINGS = {
    'guild_id': 100, 'jail_channel_id': 101, 'jail_role_id': 102,
    'admin_role_ids': [103], 'moderator_role_ids': [104], 'voice_pull_enabled': True,
    'arrest_notification_channel_id': 105, 'appeal_voting_channel_id': 106,
    'arrest_durations': [{'label': '42 секунды', 'seconds': 42}],
    'appeal_voting_durations': {'42': 17},
}


def member(user_id=10, role_ids=(), administrator=False, moderate_members=False):
    return SimpleNamespace(
        id=user_id, display_name='Пользователь', roles=[SimpleNamespace(id=role_id) for role_id in role_ids],
        guild_permissions=SimpleNamespace(administrator=administrator, moderate_members=moderate_members),
        guild=SimpleNamespace(id=100, owner_id=99),
    )


def interaction(user):
    return SimpleNamespace(
        user=user, guild_id=100, guild=user.guild, data={'values': ['0']},
        response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock(),
                                 defer=AsyncMock(), send_modal=AsyncMock(), is_done=Mock(return_value=True)),
        followup=SimpleNamespace(send=AsyncMock()), edit_original_response=AsyncMock(),
    )


def fake_guild():
    guild = Mock()
    guild.get_role.side_effect = lambda role_id: SimpleNamespace(id=role_id, mention=f'<@&{role_id}>')
    guild.get_channel.side_effect = lambda channel_id: SimpleNamespace(id=channel_id, mention=f'<#{channel_id}>')
    guild.create_voice_channel = AsyncMock()
    guild.create_text_channel = AsyncMock()
    guild.create_role = AsyncMock()
    return guild


def panel_for(user, *, read_only, edit_allowed=False):
    guild = fake_guild()
    bot = SimpleNamespace(
        get_guild=lambda guild_id: guild,
        db=SimpleNamespace(update_guild_settings=AsyncMock(), delete_guild_settings=AsyncMock()),
    )
    return MainConfigPanel(
        bot, ConfigDraft(100, SETTINGS), user.id, read_only=read_only,
        access_check=AsyncMock(return_value=True), edit_check=AsyncMock(return_value=edit_allowed),
    )


class AccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_permission_matrix_and_existing_admin_behavior(self):
        cases = [
            ('ordinary', member(), False, False),
            ('moderator', member(role_ids=[104]), False, True),
            ('configured_admin', member(role_ids=[103]), True, True),
            ('both_roles', member(role_ids=[103, 104]), True, True),
            ('discord_admin', member(administrator=True), True, True),
            ('super_admin', member(user_id=next(iter(bot_module.SUPER_ADMIN_IDS))), True, True),
            ('discord_moderate_only', member(moderate_members=True), False, False),
        ]
        with patch.object(bot_module, 'get_guild_config', AsyncMock(return_value=SETTINGS)):
            for name, user, admin, commands in cases:
                with self.subTest(name=name):
                    self.assertEqual(await bot_module.has_admin_role(100, user), admin)
                    self.assertEqual(await bot_module.has_command_access(100, user), commands)
            self.assertTrue(await bot_module.has_voice_admin_access(member(role_ids=[104]), SETTINGS))
            self.assertTrue(await bot_module.has_voice_admin_access(member(moderate_members=True), SETTINGS))
        with patch.object(bot_module, 'get_guild_config', AsyncMock(return_value={'admin_role_ids': [103]})):
            self.assertFalse(await bot_module.has_command_access(100, member(role_ids=[104])))
            self.assertTrue(await bot_module.has_command_access(100, member(role_ids=[103])))

    async def test_jail_config_opens_read_only_for_moderator_and_editable_for_admin(self):
        for user, expected in [(member(role_ids=[104]), True), (member(role_ids=[103]), False)]:
            with self.subTest(read_only=expected):
                request = interaction(user)
                with patch.object(bot_module, 'get_guild_config', AsyncMock(return_value=SETTINGS)), \
                     patch.object(bot_module.db, 'get_or_create_guild_settings', AsyncMock(return_value=SETTINGS)), \
                     patch.object(bot_module.bot, 'get_guild', return_value=fake_guild()):
                    await bot_module.jail_config.callback(request)
                sent = request.followup.send.await_args.kwargs
                self.assertTrue(sent['ephemeral'])
                self.assertEqual(sent['view'].panel.read_only, expected)
                labels = [item.label for item in sent['view'].children]
                self.assertEqual('💾 Сохранить' in labels, not expected)

    async def test_moderator_can_run_all_six_moderation_commands(self):
        user = member(role_ids=[104])
        user.voice = SimpleNamespace(channel=SimpleNamespace(members=[]))
        target = member(user_id=20)
        target.bot = False
        target.voice = SimpleNamespace(channel=SimpleNamespace(id=900))
        target.display_name = 'Участник'
        target.move_to = AsyncMock()
        user.voice.channel.members = [user, target]
        ctx = SimpleNamespace(guild=SimpleNamespace(id=100), author=user, send=AsyncMock())
        arrest = AsyncMock(return_value=True)
        release = AsyncMock()
        with patch.object(bot_module, 'get_guild_config', AsyncMock(return_value=SETTINGS)), \
             patch.object(bot_module, 'validate_bot_configuration', AsyncMock(return_value=(True, ''))), \
             patch.object(bot_module, 'arrest_member', arrest), \
             patch.object(bot_module, 'release_arrested_member', release), \
             patch.object(bot_module.db, 'get_active_arrest', AsyncMock(return_value={'member_id': target.id})):
            await bot_module.arrest_slash.callback(interaction(user), target, '42')
            arrest.assert_awaited_once()
            await bot_module.release_slash.callback(interaction(user), target)
            await bot_module.sleep_slash.callback(interaction(user), target)
            await bot_module.arrest_command.callback(ctx)
            self.assertEqual(ctx.send.await_args.kwargs['view'].admin.id, user.id)
            await bot_module.release_command.callback(ctx, target)
            await bot_module.sleep_command.callback(ctx, target)
            self.assertEqual(release.await_count, 2)
            self.assertEqual(target.move_to.await_count, 2)

    async def test_ordinary_member_denied_all_command_entrypoints(self):
        user = member()
        target = member(user_id=20)
        with patch.object(bot_module, 'get_guild_config', AsyncMock(return_value=SETTINGS)):
            for command, args in [
                (bot_module.jail_config, ()), (bot_module.arrest_slash, (target, '42')),
                (bot_module.release_slash, (target,)), (bot_module.sleep_slash, (target,)),
            ]:
                request = interaction(user)
                await command.callback(request, *args)
                calls = request.response.send_message.await_args or request.followup.send.await_args
                self.assertIn('нет прав', calls.args[0])
            for command, args in [
                (bot_module.arrest_command, ()), (bot_module.release_command, (target,)),
                (bot_module.sleep_command, (target,)),
            ]:
                ctx = SimpleNamespace(author=user, guild=user.guild, send=AsyncMock())
                await command.callback(ctx, *args)
                self.assertIn('нет прав', ctx.send.await_args.args[0])

    async def test_moderator_can_view_every_section_and_close(self):
        user = member(role_ids=[104])
        panel = panel_for(user, read_only=True)
        embed, view = panel.get_current_screen()
        self.assertIn('<@&104>', embed.fields[1].value)
        self.assertEqual(len(view.children), 5)
        for screen, expected in [
            ('channels', '<#101>'), ('roles', '<@&104>'),
            ('arrest_durations', '42 секунды'), ('appeals', '17 сек'),
        ]:
            await panel.create_navigation_callback(screen)(interaction(user))
            embed, view = panel.get_current_screen()
            self.assertIn(expected, embed.description)
            self.assertEqual([item.label for item in view.children], ['◀️ Назад'])
            await panel.back_callback(interaction(user))
            self.assertEqual(panel.navigation.current_screen, 'main')
        request = interaction(user)
        await panel.close_callback(request)
        self.assertIn('закрыта', request.response.send_message.await_args.args[0])
        self.assertFalse(panel.draft.has_changes())

    async def test_every_mutating_callback_denied_for_read_only_and_revoked_admin(self):
        user = member(role_ids=[104])
        names = [
            'voice_pull_toggle_callback', 'save_callback', 'undo_changes_callback',
            'factory_reset_confirm_callback', 'admin_roles_callback', 'moderator_roles_callback',
            'jail_role_callback', 'setup_jail_channel_callback', 'setup_notif_channel_callback',
            'setup_appeal_channel_callback', 'setup_jail_role_callback', 'create_jail_channel_callback',
            'create_notification_channel_callback', 'create_appeal_channel_callback', 'create_jail_role_callback',
            'add_duration_callback', 'edit_duration_callback', 'delete_duration_callback',
            'edit_appeal_callback', 'set_appeal_defaults_callback',
        ]
        for read_only in (True, False):
            panel = panel_for(user, read_only=read_only)
            before = copy.deepcopy(panel.draft.get_draft())
            for name in names:
                with self.subTest(read_only=read_only, callback=name):
                    request = interaction(user)
                    await getattr(panel, name)(request)
                    request.response.send_message.assert_awaited_once()
                    self.assertIn('только администраторы', request.response.send_message.await_args.args[0])
                    request.response.edit_message.assert_not_awaited()
                    request.response.send_modal.assert_not_awaited()
                    self.assertEqual(panel.draft.get_draft(), before)
            await panel.create_navigation_callback('factory_reset_confirm')(interaction(user))
            self.assertEqual(panel.navigation.current_screen, 'main')
            await panel.create_channel_callback('jail_channel_id', 'Тюрьма')(interaction(user))
            panel.bot.db.update_guild_settings.assert_not_awaited()
            panel.bot.db.delete_guild_settings.assert_not_awaited()

    async def test_all_modal_submissions_recheck_access(self):
        user = member(role_ids=[104])
        for read_only in (True, False):
            panel = panel_for(user, read_only=read_only)
            modals = [AddDurationModal(panel), EditDurationModal(panel, 0, SETTINGS['arrest_durations'][0]),
                      EditAppealVotingModal(panel, 42, 17), CreateJailChannelModal(panel),
                      CreateNotificationChannelModal(panel), CreateAppealChannelModal(panel), CreateJailRoleModal(panel)]
            for modal in modals:
                with self.subTest(read_only=read_only, modal=type(modal).__name__):
                    request = interaction(user)
                    request.guild = panel.bot.get_guild(100)
                    await modal.on_submit(request)
                    request.response.send_message.assert_awaited_once()
                    request.response.defer.assert_not_awaited()
                    self.assertFalse(panel.draft.has_changes())
                    request.guild.create_voice_channel.assert_not_awaited()
                    request.guild.create_text_channel.assert_not_awaited()
                    request.guild.create_role.assert_not_awaited()

    async def test_nested_selects_recheck_access_after_opening(self):
        user = member(role_ids=[103])
        for name in ('admin_roles_callback', 'moderator_roles_callback', 'edit_duration_callback',
                     'delete_duration_callback', 'edit_appeal_callback'):
            panel = panel_for(user, read_only=False, edit_allowed=True)
            request = interaction(user)
            await getattr(panel, name)(request)
            view = request.response.edit_message.await_args.kwargs['view']
            panel.edit_check.return_value = False
            await view.children[0].callback(interaction(user))
            self.assertFalse(panel.draft.has_changes())

    async def test_admin_can_save_moderators_and_selector_can_clear_them(self):
        user = member(role_ids=[103])
        panel = panel_for(user, read_only=False, edit_allowed=True)
        request = interaction(user)
        await panel.moderator_roles_callback(request)
        selector = request.response.edit_message.await_args.kwargs['view'].children[0]
        self.assertEqual(selector.min_values, 0)
        # RoleSelect.values принимает роли из обработанного Discord interaction.
        with patch.object(discord.ui.RoleSelect, 'values', new_callable=unittest.mock.PropertyMock,
                          return_value=[SimpleNamespace(id=110)]):
            await selector.callback(interaction(user))
        self.assertEqual(panel.draft.get_draft()['moderator_role_ids'], [110])
        self.assertEqual(panel.draft.get_draft()['admin_role_ids'], [103])
        with patch.object(panel, 'configure_jail_role_permissions', AsyncMock()):
            await panel.save_callback(interaction(user))
        panel.bot.db.update_guild_settings.assert_awaited_once()
        self.assertEqual(panel.bot.db.update_guild_settings.await_args.args[1]['moderator_role_ids'], [110])
        self.assertFalse(panel.draft.has_changes())
        with patch.object(discord.ui.RoleSelect, 'values', new_callable=unittest.mock.PropertyMock, return_value=[]):
            await selector.callback(interaction(user))
        self.assertEqual(panel.draft.get_draft()['moderator_role_ids'], [])

    async def test_another_user_cannot_use_panel_and_revoked_viewer_denied(self):
        panel = panel_for(member(role_ids=[104]), read_only=True)
        self.assertFalse(await panel.check_access(interaction(member(user_id=11)), edit=False))
        panel.access_check.return_value = False
        self.assertFalse(await panel.check_access(interaction(member()), edit=False))
