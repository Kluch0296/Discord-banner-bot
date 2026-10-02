import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
from discord.app_commands.namespace import ResolveKey

from config_ui import (
    AccessRolesModal, ResourceSettingModal, VoiceSettingsModal,
    DeleteDurationsModal, AppealDefaultsModal,
)
from test_moderator_access import SETTINGS, interaction, member, panel_for


def submit_values(modal, request, fields):
    """Пропускаем вложенные Label/Select через настоящий парсер discord.py."""
    components = []
    resolved = {}
    for item, value in fields:
        data = {'type': item.type.value, 'custom_id': item.custom_id}
        if isinstance(item, discord.ui.TextInput):
            data['value'] = value
        else:
            data['values'] = [str(v.id) if hasattr(v, 'id') else str(v) for v in value]
            for obj in value:
                if hasattr(obj, 'id'):
                    resolved[ResolveKey(id=str(obj.id), type=8 if isinstance(item, discord.ui.RoleSelect) else 7)] = obj
        components.append({'type': 18, 'component': data})
    modal._refresh(request, components, resolved)


class SettingsModalTests(unittest.IsolatedAsyncioTestCase):
    def make_panel(self):
        user = member(role_ids=[103])
        panel = panel_for(user, read_only=False, edit_allowed=True)
        panel.message = SimpleNamespace(edit=AsyncMock())
        guild = panel.bot.get_guild(100)
        guild.get_channel.side_effect = lambda ident: SimpleNamespace(
            id=ident, mention=f'<#{ident}>',
            type=discord.ChannelType.voice if ident == 101 else discord.ChannelType.text,
        )
        return user, panel, guild

    async def test_leaf_callbacks_open_forms_without_replacing_navigation_or_mutating_draft(self):
        cases = [
            ('roles', 'admin_roles_callback', AccessRolesModal),
            ('roles', 'moderator_roles_callback', AccessRolesModal),
            ('roles', 'setup_jail_role_callback', ResourceSettingModal),
            ('channels', 'setup_jail_channel_callback', ResourceSettingModal),
            ('channels', 'setup_notif_channel_callback', ResourceSettingModal),
            ('channels', 'setup_appeal_channel_callback', ResourceSettingModal),
            ('main', 'voice_pull_toggle_callback', VoiceSettingsModal),
            ('arrest_durations', 'delete_duration_callback', DeleteDurationsModal),
            ('appeals', 'set_appeal_defaults_callback', AppealDefaultsModal),
        ]
        for screen, callback, expected in cases:
            with self.subTest(callback=callback):
                user, panel, _ = self.make_panel()
                if screen != 'main':
                    panel.navigation.navigate_to(screen)
                before = copy.deepcopy(panel.draft.get_draft())
                history = list(panel.navigation.history)
                request = interaction(user)
                await getattr(panel, callback)(request)
                request.response.edit_message.assert_not_awaited()
                self.assertIsInstance(request.response.send_modal.await_args.args[0], expected)
                self.assertEqual(panel.draft.get_draft(), before)
                self.assertEqual(panel.navigation.history, history)
                panel.bot.db.update_guild_settings.assert_not_awaited()

    async def test_new_form_payloads_match_discord_limits_and_preselect_current_values(self):
        user, panel, _ = self.make_panel()
        admin = AccessRolesModal(panel, 'admin_role_ids')
        mod = AccessRolesModal(panel, 'moderator_role_ids')
        resources = [
            ResourceSettingModal(panel, 'jail_channel_id', 'Канал тюрьмы', 'voice'),
            ResourceSettingModal(panel, 'arrest_notification_channel_id', 'Апелляции', 'text'),
            ResourceSettingModal(panel, 'jail_role_id', 'Роль заключенного', 'role'),
        ]
        forms = [admin, mod, *resources, VoiceSettingsModal(panel), DeleteDurationsModal(panel), AppealDefaultsModal(panel)]
        for form in forms:
            payload = form.to_dict()
            self.assertLessEqual(len(payload['title']), 45)
            self.assertLessEqual(len(payload['components']), 5)
            for component in payload['components']:
                self.assertIn(component['type'], (18, 10))
                if component['type'] != 18:
                    continue
                self.assertLessEqual(len(component['label']), 45)
                self.assertLessEqual(len(component.get('description', '')), 100)
                field = component['component']
                if field['type'] in (3, 6, 8):
                    self.assertLessEqual(field['max_values'], 25)
                    if field['min_values'] == 0:
                        self.assertFalse(field['required'])
        self.assertEqual([v.id for v in admin.roles.default_values], [103])
        self.assertEqual([v.id for v in mod.roles.default_values], [104])
        self.assertEqual([f.selected.default_values[0].id for f in resources], [101, 105, 102])

    async def test_both_role_lists_can_replace_and_clear_without_touching_other_settings(self):
        for key in ('admin_role_ids', 'moderator_role_ids'):
            with self.subTest(key=key):
                user, panel, _ = self.make_panel()
                panel.navigation.navigate_to('roles')
                before = copy.deepcopy(panel.draft.get_draft())
                form = AccessRolesModal(panel, key)
                request = interaction(user)
                submit_values(form, request, [(form.roles, [SimpleNamespace(id=110)])])
                await form.on_submit(request)
                expected = copy.deepcopy(before)
                expected[key] = [110]
                self.assertEqual(panel.draft.get_draft(), expected)
                self.assertEqual(panel.navigation.current_screen, 'roles')
                panel.message.edit.assert_awaited_once()
                clear = AccessRolesModal(panel, key)
                request = interaction(user)
                submit_values(clear, request, [(clear.roles, [])])
                await clear.on_submit(request)
                self.assertEqual(panel.draft.get_draft()[key], [])
                panel.bot.db.update_guild_settings.assert_not_awaited()

    async def test_existing_channels_and_jail_role_change_only_on_submit(self):
        for key, kind, value in [
            ('jail_channel_id', 'voice', 101),
            ('arrest_notification_channel_id', 'text', 205),
            ('appeal_voting_channel_id', 'text', 206),
            ('jail_role_id', 'role', 202),
        ]:
            with self.subTest(key=key):
                user, panel, guild = self.make_panel()
                panel.navigation.navigate_to('roles' if kind == 'role' else 'channels')
                history = list(panel.navigation.history)
                form = ResourceSettingModal(panel, key, 'Настройка', kind)
                request = interaction(user)
                submit_values(form, request, [(form.selected, [SimpleNamespace(id=value)]), (form.new_name, '')])
                await form.on_submit(request)
                self.assertEqual(panel.draft.get_draft()[key], value)
                self.assertEqual(panel.navigation.history, history)
                guild.create_voice_channel.assert_not_awaited()
                guild.create_text_channel.assert_not_awaited()
                guild.create_role.assert_not_awaited()

    async def test_new_resource_creation_takes_precedence_and_keeps_current_section(self):
        for key, kind, create in [
            ('jail_channel_id', 'voice', 'create_voice_channel'),
            ('arrest_notification_channel_id', 'text', 'create_text_channel'),
            ('jail_role_id', 'role', 'create_role'),
        ]:
            with self.subTest(key=key):
                user, panel, guild = self.make_panel()
                panel.navigation.navigate_to('roles' if kind == 'role' else 'channels')
                history = list(panel.navigation.history)
                getattr(guild, create).return_value = SimpleNamespace(id=777)
                form = ResourceSettingModal(panel, key, 'Настройка', kind)
                request = interaction(user)
                submit_values(form, request, [(form.selected, [SimpleNamespace(id=panel.draft.get_draft()[key])]),
                                               (form.new_name, ' Новый объект ')])
                await form.on_submit(request)
                self.assertEqual(getattr(guild, create).await_args.kwargs['name'], 'Новый объект')
                self.assertEqual(panel.draft.get_draft()[key], 777)
                self.assertEqual(panel.navigation.history, history)
                panel.bot.db.update_guild_settings.assert_not_awaited()
                if kind == 'role':
                    self.assertEqual(guild.create_role.await_args.kwargs['permissions'].value, 0)

    async def test_optional_channel_can_clear_but_required_resources_cannot(self):
        for key, kind, allowed in [
            ('arrest_notification_channel_id', 'text', True),
            ('appeal_voting_channel_id', 'text', True),
            ('jail_channel_id', 'voice', False),
            ('jail_role_id', 'role', False),
        ]:
            with self.subTest(key=key):
                user, panel, _ = self.make_panel()
                form = ResourceSettingModal(panel, key, 'Настройка', kind)
                before = copy.deepcopy(panel.draft.get_draft())
                request = interaction(user)
                submit_values(form, request, [(form.selected, []), (form.new_name, '')])
                await form.on_submit(request)
                if allowed:
                    self.assertEqual(panel.draft.get_draft()[key], 0)
                else:
                    self.assertEqual(panel.draft.get_draft(), before)
                    self.assertIn('Выберите', request.response.send_message.await_args.args[0])

    async def test_wrong_channel_type_is_rejected_without_changing_draft(self):
        user, panel, _ = self.make_panel()
        form = ResourceSettingModal(panel, 'jail_channel_id', 'Тюрьма', 'voice')
        request = interaction(user)
        submit_values(form, request, [(form.selected, [SimpleNamespace(id=105)]), (form.new_name, '')])
        await form.on_submit(request)
        self.assertFalse(panel.draft.has_changes())
        self.assertIn('недоступен', request.response.send_message.await_args.args[0])

    async def test_forms_recheck_revoked_access_and_author_on_submission(self):
        factories = [
            lambda p: AccessRolesModal(p, 'admin_role_ids'),
            lambda p: AccessRolesModal(p, 'moderator_role_ids'),
            lambda p: ResourceSettingModal(p, 'jail_channel_id', 'Тюрьма', 'voice'),
            lambda p: ResourceSettingModal(p, 'jail_role_id', 'Роль', 'role'),
            VoiceSettingsModal, DeleteDurationsModal, AppealDefaultsModal,
        ]
        for factory in factories:
            for another_user in (False, True):
                with self.subTest(factory=factory, another_user=another_user):
                    user, panel, guild = self.make_panel()
                    form = factory(panel)
                    before = copy.deepcopy(panel.draft.get_draft())
                    if not another_user:
                        panel.edit_check.return_value = False
                    request = interaction(member(user_id=11) if another_user else user)
                    await form.on_submit(request)
                    request.response.send_message.assert_awaited_once()
                    self.assertEqual(panel.draft.get_draft(), before)
                    panel.bot.db.update_guild_settings.assert_not_awaited()
                    guild.create_voice_channel.assert_not_awaited()
                    guild.create_text_channel.assert_not_awaited()
                    guild.create_role.assert_not_awaited()

    async def test_create_permission_error_preserves_draft_and_menu(self):
        user, panel, guild = self.make_panel()
        response = SimpleNamespace(status=403, reason='Forbidden')
        guild.create_role.side_effect = discord.Forbidden(response, 'Missing Permissions')
        form = ResourceSettingModal(panel, 'jail_role_id', 'Роль', 'role')
        request = interaction(user)
        submit_values(form, request, [(form.selected, []), (form.new_name, 'Новая роль')])
        await form.on_submit(request)
        self.assertFalse(panel.draft.has_changes())
        self.assertIn('нет прав', request.followup.send.await_args.args[0])

    async def test_delete_presets_removes_only_their_appeals_and_detects_stale_forms(self):
        user, panel, _ = self.make_panel()
        panel.draft.update('arrest_durations', [*SETTINGS['arrest_durations'], {'label': 'Минута', 'seconds': 60}])
        panel.draft.update('appeal_voting_durations', {'42': 17, '60': 20})
        panel.navigation.navigate_to('arrest_durations')
        stale = DeleteDurationsModal(panel)
        form = DeleteDurationsModal(panel)
        request = interaction(user)
        submit_values(form, request, [(form.presets, ['42'])])
        await form.on_submit(request)
        self.assertEqual(panel.draft.get_draft()['arrest_durations'], [{'label': 'Минута', 'seconds': 60}])
        self.assertEqual(panel.draft.get_draft()['appeal_voting_durations'], {'60': 20})
        self.assertEqual(panel.navigation.current_screen, 'arrest_durations')
        before = copy.deepcopy(panel.draft.get_draft())
        request = interaction(user)
        submit_values(stale, request, [(stale.presets, ['42'])])
        await stale.on_submit(request)
        self.assertEqual(panel.draft.get_draft(), before)
        self.assertIn('изменились', request.response.send_message.await_args.args[0])

    async def test_voice_setting_and_default_appeals_apply_only_when_submitted(self):
        user, panel, _ = self.make_panel()
        voice = VoiceSettingsModal(panel)
        self.assertTrue(panel.draft.get_draft()['voice_pull_enabled'])
        request = interaction(user)
        submit_values(voice, request, [(voice.enabled, ['off'])])
        await voice.on_submit(request)
        self.assertFalse(panel.draft.get_draft()['voice_pull_enabled'])
        panel.draft.update('arrest_durations', [
            {'label': str(seconds), 'seconds': seconds} for seconds in (15, 30, 60, 300, 3000)
        ])
        defaults = AppealDefaultsModal(panel)
        request = interaction(user)
        submit_values(defaults, request, [(defaults.confirm, ['reset'])])
        await defaults.on_submit(request)
        self.assertEqual(panel.draft.get_draft()['appeal_voting_durations'],
                         {'15': 0, '30': 0, '60': 15, '300': 30, '3000': 120})
        panel.bot.db.update_guild_settings.assert_not_awaited()
