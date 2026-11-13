import asyncio
from copy import deepcopy
import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import discord
from discord import app_commands
from dotenv import load_dotenv

BRAND = "💀 ПГО КАРАТЕЛИ"
COLOR = 0x871C30
PROTECTED_RANK = "Заместитель основателя"
ACTIONS = {
    "promote": ("📈", "Повышение"),
    "demote": ("📉", "Понижение"),
    "transfer": ("🔄", "Перевод"),
    "dismiss": ("🚪", "Увольнение"),
    "grant": ("🎖️", "Назначить на должность"),
}


def snowflake(value):
    if isinstance(value, bool) or not str(value).isascii() or not str(value).isdecimal():
        raise ValueError("Укажите числовые ID Discord без пробелов.")
    number = int(value)
    if not 0 < number < 2**63:
        raise ValueError("Некорректный ID Discord.")
    return number


def department_role_ids(value):
    values = value if isinstance(value, list) else [value]
    if not values:
        raise ValueError("У каждого отдела должна быть хотя бы одна роль Discord.")
    return tuple(snowflake(role) for role in values)


@dataclass(frozen=True)
class Config:
    guild: int
    panel: int
    journal: int
    senior: int
    chief: int
    founder: int
    ranks: dict
    departments: dict
    statuses: dict
    extra_roles: dict
    department_tags: dict
    database: str

    @classmethod
    def load(cls, path):
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        groups = []
        for key in ("ranks", "departments", "dismissal_statuses", "extra_roles"):
            source = raw[key]
            if not isinstance(source, dict) or not 1 <= len(source) <= 25:
                raise ValueError(f"{key} должен содержать от 1 до 25 ролей.")
            if any(not 1 <= len(name.strip()) <= 100 for name in source):
                raise ValueError(f"Проверьте названия ролей в {key}.")
            if key == "departments":
                groups.append({name: department_role_ids(value) for name, value in source.items()})
            else:
                groups.append({name: snowflake(role) for name, role in source.items()})
        guild, panel, journal, senior = (snowflake(raw[k]) for k in (
            "guild_id", "panel_channel_id", "audit_channel_id", "senior_role_id"))
        chief = snowflake(raw["family_chief_role_id"])
        founder = snowflake(raw["founder_role_id"])
        if founder in (guild, senior):
            raise ValueError("Укажите отдельный ID роли Основателя.")
        if list(groups[0])[-1] != PROTECTED_RANK:
            raise ValueError(f"Последним в ranks укажите «{PROTECTED_RANK}».")
        ids = [role for index, group in enumerate(groups) if index != 1 for role in group.values()]
        ids.extend(role for bundle in groups[1].values() for role in bundle)
        if len(ids) != len(set(ids)) or guild in ids or {senior, chief, founder} & set(ids) or len({guild, senior, chief}) != 3:
            raise ValueError("ID ролей званий, отделов, статусов, Начальника семьи и старшего состава должны различаться.")
        if panel == journal:
            raise ValueError("Панель и кадровый журнал должны находиться в разных каналах.")
        tags = raw.get("department_tags", {})
        if not isinstance(tags, dict) or any(not isinstance(tag, str) or not 1 <= len(tag.strip()) <= 10
                                             or not tag.replace(" ", "").isalnum() for tag in tags.values()):
            raise ValueError("В department_tags укажите короткие обозначения отделов без знаков.")
        return cls(guild, panel, journal, senior, chief, founder, *groups, tags,
                   os.getenv("KADRO_DB", "karateli_kadro.sqlite3"))


def can_grant_extra(config, actor_roles):
    return bool({config.founder, config.ranks[PROTECTED_RANK]} & actor_roles)


def require_authorization(config, actor_roles, current, action, destination):
    if action == "grant":
        if not can_grant_extra(config, actor_roles):
            raise ValueError("Дополнительные роли выдают только Основатель и Заместитель основателя.")
        return
    if not {config.senior, config.chief} & actor_roles:
        raise ValueError("У вас нет роли старшего состава или Начальника семьи.")
    protected = config.ranks[PROTECTED_RANK]
    if config.chief not in actor_roles and (
        (action in ("promote", "demote") and destination == protected)
        or (action in ("promote", "demote", "dismiss") and protected in current)
    ):
        raise ValueError("Только Начальник семьи может выдать или снять роль «Заместитель основателя».")


def plan_roles(config, current, action, destination, department=None):
    ranks = list(config.ranks.values())
    departments = {role for bundle in config.departments.values() for role in bundle}
    statuses = set(config.statuses.values())
    active_ranks = current & set(ranks)
    active_departments = current & departments
    if action in ("promote", "demote"):
        if len(active_ranks) != 1:
            raise ValueError("У участника должно быть ровно одно настроенное звание.")
        old = ranks.index(next(iter(active_ranks)))
        if destination not in ranks:
            raise ValueError("Это звание не настроено.")
        new = ranks.index(destination)
        if (action == "promote" and new <= old) or (action == "demote" and new >= old):
            raise ValueError("Новое звание должно соответствовать направлению операции.")
        return (current - active_ranks) | {destination}
    if action == "transfer":
        old_units = [unit for unit, bundle in config.departments.items() if active_departments.intersection(bundle)]
        if len(old_units) != 1:
            raise ValueError("Для перевода у участника должен быть ровно один настроенный отдел.")
        if destination not in config.departments or destination == old_units[0]:
            raise ValueError("Выберите другой настроенный отдел.")
        return (current - departments) | set(config.departments[destination])
    if action == "grant":
        if destination not in config.extra_roles.values():
            raise ValueError("Эта дополнительная роль не настроена.")
        if department not in config.departments:
            raise ValueError("Выберите настроенный отдел.")
        position_roles = {role for name, role in config.extra_roles.items() if name != "Сотрудник семьи"}
        old_positions = position_roles if destination in position_roles else set()
        return (current - departments - old_positions) | set(config.departments[department]) | {destination}
    if action == "dismiss":
        if destination not in statuses:
            raise ValueError("Этот статус увольнения не настроен.")
        if not active_ranks and not active_departments:
            raise ValueError("У участника нет настроенных кадровых ролей.")
        return (current - set(ranks) - departments - statuses) | {destination}
    raise ValueError("Неизвестная операция.")


def department_tag(config, department_name):
    return config.department_tags.get(department_name) or "".join(
        word[0].upper() for word in department_name.split() if len(word) > 1)[:10] or "ОТДЕЛ"


def appointment_nick(config, destination, department, name):
    position = next((name for name, rid in config.extra_roles.items() if rid == destination), None)
    unit = department if department in config.departments else None
    if not position or not unit:
        raise ValueError("Должность или отдел не настроены.")
    prefixes = {"Начальник отдела": "Начальник", "Зам. начальника отдела": "Зам. нач.",
                "Сотрудник семьи": "Сотрудник"}
    prefix = prefixes.get(position, position)
    nick = f"{prefix} {department_tag(config, unit)} | {' '.join(name.split())}"
    if not name.strip() or len(nick) > 32:
        raise ValueError("Укажите имя короче: весь новый никнейм должен занимать не более 32 символов.")
    return nick


def suggested_name(config, label):
    prefix, separator, name = label.partition(" | ")
    managed = {"Начальник", "Зам. начальника", "Зам. нач.", "Сотрудник"} | set(config.extra_roles)
    tags = {department_tag(config, unit) for unit in config.departments}
    return name if separator and prefix in {f"{p} {t}" for p in managed for t in tags} else label


def embed(title, description=None):
    card = discord.Embed(title=title, description=description, color=COLOR,
                         timestamp=discord.utils.utcnow())
    card.set_footer(text=f"{BRAND} • Кадровый аудит")
    return card


async def send_personal_notice(target, actor, card, action, record):
    """Notify only the affected member, using text rather than server mentions."""
    notice = discord.Embed.from_dict(deepcopy(card.to_dict()))
    notice.title = {
        "promote": "📈 Тебя повысили",
        "demote": "📉 Твоё звание изменено",
        "transfer": "🔄 Тебя перевели в другой отдел",
        "dismiss": "🚪 Ты уволен из состава",
        "grant": "🎖️ Тебя назначили на должность",
    }[action]
    notice.remove_field(0)  # The recipient already knows who they are.
    notice.set_field_at(0, name="Решение принял",
                        value=discord.utils.escape_markdown(actor.display_name), inline=False)
    notice.set_footer(text=BRAND + (f" • №{record}" if record else ""))
    try:
        await target.send(embed=notice, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        logging.info("Не удалось отправить ЛС участнику %s по записи %s", target.id, record)
        return False
    return True


async def reply(interaction, content):
    if interaction.response.is_done():
        await interaction.followup.send(content, ephemeral=True)
    else:
        await interaction.response.send_message(content, ephemeral=True)


class AuditClient(discord.Client):
    def __init__(self, config):
        super().__init__(intents=discord.Intents.default(),
                         allowed_mentions=discord.AllowedMentions.none())
        self.config = config
        self.tree = app_commands.CommandTree(self)
        self.lock = asyncio.Lock()
        @app_commands.default_permissions(manage_guild=True)
        async def publish_command(interaction: discord.Interaction):
            await self.publish(interaction)

        self.tree.add_command(app_commands.Command(
            name="setup_kadro", description="Опубликовать панель кадрового аудита",
            callback=publish_command),
            guild=discord.Object(config.guild))

    async def setup_hook(self):
        with sqlite3.connect(self.config.database) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS journal (
                id INTEGER PRIMARY KEY, created TEXT NOT NULL, actor INTEGER NOT NULL,
                member INTEGER NOT NULL, action TEXT NOT NULL, reason TEXT NOT NULL,
                old_roles TEXT NOT NULL, new_roles TEXT NOT NULL, discord_message INTEGER
            )""")
        self.add_view(MainPanel(self))
        await self.tree.sync(guild=discord.Object(self.config.guild))

    async def on_ready(self):
        logging.info("ПГО КАРАТЕЛИ подключён. Сервер %s", self.config.guild)

    def can_review(self, interaction, action=None):
        if interaction.guild_id != self.config.guild or not isinstance(interaction.user, discord.Member):
            return False
        actor_roles = {r.id for r in interaction.user.roles}
        standard = bool({self.config.senior, self.config.chief} & actor_roles)
        extra = can_grant_extra(self.config, actor_roles)
        return extra if action == "grant" else (standard or extra if action is None else standard)

    def channel(self, guild, channel_id):
        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel) or guild.me is None:
            raise ValueError("Канал панели или журнала не найден на сервере.")
        perms = channel.permissions_for(guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            raise ValueError("Бот должен видеть канал, отправлять сообщения и вставлять ссылки.")
        return channel

    async def publish(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.manage_guild:
            return await reply(interaction, "Нужно право «Управление сервером».")
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            guild = interaction.guild
            panel = self.channel(guild, self.config.panel)
            self.channel(guild, self.config.journal)
            if any(not guild.get_role(rid) for rid in (self.config.senior, self.config.chief, self.config.founder)):
                raise ValueError("Роль старшего состава, Начальника семьи или Основателя не найдена.")
            if not guild.me.guild_permissions.manage_roles:
                raise ValueError("Боту нужно право «Управлять ролями».")
            for group in (self.config.ranks, self.config.departments, self.config.statuses, self.config.extra_roles):
                for name, value in group.items():
                    for role_id in value if isinstance(value, tuple) else (value,):
                        role = guild.get_role(role_id)
                        if not role or not role.is_assignable():
                            raise ValueError(f"Роль «{name}» (ID {role_id}) не найдена или недоступна боту.")
            message = await panel.send(embed=embed(
                f"{BRAND} | КАДРОВЫЙ АУДИТ",
                "Выберите кадровое действие в выпадающем списке.\n\n"
                "📈 **Повышение** и 📉 **Понижение** меняют звание, сохраняя отдел.\n"
                "🔄 **Перевод** меняет отдел, сохраняя звание.\n"
                "🚪 **Увольнение** снимает кадровые роли и выдаёт выбранный статус.\n\n"
                "🎖️ **Назначить на должность** — только Основатель и Заместитель основателя.\n\n"
                "🔒 Остальные действия доступны старшему составу и Начальнику семьи. "
                "Роль «Заместитель основателя» может выдать или снять только Начальник семьи."),
                view=MainPanel(self))
            await reply(interaction, f"✅ Панель опубликована. [Открыть]({message.jump_url})")
        except (ValueError, discord.HTTPException) as exc:
            logging.warning("Не удалось опубликовать панель: %s", exc)
            await reply(interaction, str(exc) if isinstance(exc, ValueError)
                        else "Не удалось опубликовать панель. Проверьте права бота и логи.")

    async def execute(self, interaction, action, member_id, destination, reason, department=None, name=None):
        if not self.can_review(interaction, action):
            return await reply(interaction, "У вас нет доступа к этому действию.")
        if member_id == interaction.user.id:
            return await reply(interaction, "Нельзя проводить кадровое действие над собой.")
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        conf = self.config
        async with self.lock:
            try:
                channel = self.channel(guild, conf.journal)
                target = await guild.fetch_member(member_id)
                actor = await guild.fetch_member(interaction.user.id)
                if target.bot or target.id == guild.owner_id or target.top_role >= guild.me.top_role:
                    raise ValueError("Бот не может менять роли этого участника из-за иерархии Discord.")
                if actor.id != guild.owner_id and target.top_role >= actor.top_role:
                    raise ValueError("Нельзя менять роли участника с равной или более высокой ролью.")
                if not guild.me.guild_permissions.manage_roles:
                    raise ValueError("Боту не хватает права «Управлять ролями».")
                current = {r.id for r in target.roles if not r.is_default()}
                require_authorization(conf, {r.id for r in actor.roles}, current, action, destination)
                new = plan_roles(conf, current, action, destination, department)
                new_nick = None
                if action == "grant":
                    if not guild.me.guild_permissions.manage_nicknames:
                        raise ValueError("Боту не хватает права «Управлять никнеймами».")
                    new_nick = appointment_nick(conf, destination, department, name or "")
                    if new == current and new_nick == target.nick:
                        raise ValueError("Участник уже на этой должности, никнейм совпадает.")
                changed = [guild.get_role(rid) for rid in current ^ new]
                if any(r is None or not r.is_assignable()
                       or (actor.id != guild.owner_id and r >= actor.top_role) for r in changed):
                    raise ValueError("Бот или кадровик не может управлять одной из этих ролей.")
                before = [r for r in target.roles if r.id in current - new]
                after = [r for r in guild.roles if r.id in new - current]
                audit_reason = f"ПГО КАРАТЕЛИ: {ACTIONS[action][1]} / {actor.id}"
                changes = {"roles": [r for r in guild.roles if r.id in new]} if new != current else {}
                if new_nick is not None:
                    changes["nick"] = new_nick
                await target.edit(**changes, reason=audit_reason)
            except (ValueError, discord.HTTPException) as exc:
                logging.warning("Кадровое действие не подтверждено: %s", exc)
                return await reply(interaction, str(exc) if isinstance(exc, ValueError)
                                   else "Discord не подтвердил изменение. Проверьте роли участника и права бота.")

            def names(roles):
                return ", ".join(r.name for r in roles)[:1024] or "—"

            mark, label = ACTIONS[action]
            card = embed(f"{mark} {label.upper()}")
            card.add_field(name="Участник", value=f"{target.mention}\nID: `{target.id}`")
            card.add_field(name="Кадровик", value=f"{actor.mention}\n{discord.utils.escape_markdown(actor.display_name)}")
            fields = {
                "promote": ("Было звание", "Стало звание"),
                "demote": ("Было звание", "Стало звание"),
                "transfer": ("Прежний отдел", "Новый отдел"),
                "dismiss": ("Снятые кадровые роли", "Итоговый статус"),
                "grant": ("Снятые роли", "Должность"),
            }
            previous, updated = fields[action]
            card.add_field(name=previous, value=names(before), inline=False)
            card.add_field(name=updated, value=guild.get_role(destination).name
                           if action in ("grant", "dismiss") else names(after), inline=False)
            if action == "transfer":
                old_unit = next(unit for unit, bundle in conf.departments.items() if current.intersection(bundle))
                card.add_field(name="Перевод", value=f"{old_unit} → {destination}", inline=False)
            if action == "grant":
                card.add_field(name="Отдел", value=department, inline=False)
                card.add_field(name="Выданные роли", value=names(after), inline=False)
                card.add_field(name="Никнейм", value=f"{target.display_name} → {new_nick}", inline=False)
            card.add_field(name="Причина", value=reason, inline=False)
            record = None
            try:
                with sqlite3.connect(conf.database) as db:
                    record = db.execute(
                        """INSERT INTO journal(created, actor, member, action, reason, old_roles, new_roles)
                           VALUES(?, ?, ?, ?, ?, ?, ?)""",
                        (discord.utils.utcnow().isoformat(), actor.id, target.id, action,
                         reason, json.dumps(sorted(current)), json.dumps(sorted(new)))).lastrowid
                card.set_footer(text=f"{BRAND} • Кадровое действие №{record}")
            except sqlite3.Error:
                logging.exception("Не удалось сохранить кадровую запись")
            log = None
            try:
                log = await channel.send(embed=card)
            except discord.HTTPException:
                logging.exception("Роли изменены, но журнал Discord недоступен")
            if record and log:
                try:
                    with sqlite3.connect(conf.database) as db:
                        db.execute("UPDATE journal SET discord_message=? WHERE id=?", (log.id, record))
                except sqlite3.Error:
                    logging.exception("Не сохранён ID сообщения аудита")
            dm_sent = await send_personal_notice(target, actor, card, action, record)
            if log:
                result = f"✅ Готово. [Запись в журнале]({log.jump_url})"
            else:
                backup = f" Запись №{record} сохранена в базе." if record else " Сохранить запись в базе также не удалось."
                result = "⚠️ Действие выполнено, но запись в канал аудита не отправилась." + backup
            result += "\n📩 Личное сообщение отправлено." if dm_sent else "\n⚠️ Не удалось отправить ЛС участнику."
            await reply(interaction, result)


class StaffView(discord.ui.View):
    def __init__(self, bot, *, timeout=300):
        super().__init__(timeout=timeout)
        self.bot = bot

    async def interaction_check(self, interaction):
        if self.bot.can_review(interaction, getattr(self, "action", None)):
            return True
        await reply(interaction, "У вас нет доступа к этому действию.")
        return False


class MainPanel(StaffView):
    def __init__(self, bot):
        super().__init__(bot, timeout=None)
        menu = discord.ui.Select(
            custom_id="pgo:kadro:action_menu", placeholder="Выберите кадровое действие",
            options=[discord.SelectOption(label=label, value=action, emoji=emoji)
                     for action, (emoji, label) in ACTIONS.items()])
        async def callback(interaction):
            action = menu.values[0]
            if not bot.can_review(interaction, action):
                return await reply(interaction, "У вас нет доступа к этому действию.")
            await interaction.response.send_message(
                f"**{ACTIONS[action][1]}** · выберите участника.",
                view=MemberStep(bot, action), ephemeral=True)
        menu.callback = callback
        self.add_item(menu)


class MemberStep(StaffView):
    def __init__(self, bot, action):
        super().__init__(bot)
        self.action = action

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Выберите участника")
    async def member(self, interaction, menu):
        member = menu.values[0]
        await interaction.response.edit_message(
            content=f"Участник {member.mention}. " + (
                "Выберите должность." if self.action == "grant" else "Выберите итоговую роль."),
            view=RoleStep(self.bot, self.action, member.id, {r.id for r in interaction.user.roles},
                          suggested_name(self.bot.config, member.display_name)))


class RoleStep(StaffView):
    def __init__(self, bot, action, member_id, actor_roles, name=""):
        super().__init__(bot)
        self.action = action
        conf = bot.config
        roles = {"transfer": conf.departments, "dismiss": conf.statuses,
                 "grant": conf.extra_roles}.get(action, conf.ranks)
        menu = discord.ui.Select(placeholder="Выберите должность" if action == "grant" else
                                 "Выберите итоговую роль", options=[
            discord.SelectOption(label=name, value=name if action == "transfer" else str(role))
            for name, role in roles.items()
            if role != conf.ranks[PROTECTED_RANK] or conf.chief in actor_roles
        ])

        async def callback(interaction):
            destination = menu.values[0] if action == "transfer" else int(menu.values[0])
            if action == "grant":
                position = next(label for label, rid in conf.extra_roles.items() if rid == destination)
                return await interaction.response.edit_message(
                    content=f"Должность **{position}**. Выберите отдел.",
                    view=DepartmentStep(bot, member_id, destination, name))
            await interaction.response.send_modal(ReasonForm(bot, action, member_id, destination))
        menu.callback = callback
        self.add_item(menu)


class DepartmentStep(StaffView):
    def __init__(self, bot, member_id, destination, name):
        super().__init__(bot)
        self.action = "grant"
        menu = discord.ui.Select(placeholder="Выберите отдел", options=[
            discord.SelectOption(label=label, value=label)
            for label in bot.config.departments])
        async def callback(interaction):
            await interaction.response.send_modal(
                AppointmentForm(bot, member_id, destination, menu.values[0], name))
        menu.callback = callback
        self.add_item(menu)


class AppointmentForm(discord.ui.Modal, title="Назначение на должность"):
    name = discord.ui.TextInput(label="Имя и фамилия без приставки", max_length=32)
    reason = discord.ui.TextInput(label="Причина назначения", max_length=700,
                                 style=discord.TextStyle.paragraph)

    def __init__(self, bot, member_id, destination, department, name):
        super().__init__(timeout=300)
        self.bot, self.member_id = bot, member_id
        self.destination, self.department = destination, department
        self.name.default = name[:32]

    async def on_submit(self, interaction):
        reason = self.reason.value.strip()
        if not reason:
            return await reply(interaction, "Укажите причину назначения.")
        await self.bot.execute(interaction, "grant", self.member_id, self.destination, reason,
                               department=self.department, name=self.name.value)


class ReasonForm(discord.ui.Modal, title="Подтверждение кадрового действия"):
    reason = discord.ui.TextInput(label="Причина", max_length=700, required=True,
                                  style=discord.TextStyle.paragraph)

    def __init__(self, bot, action, member_id, destination):
        super().__init__(timeout=300)
        self.bot, self.action, self.member_id, self.destination = bot, action, member_id, destination

    async def on_submit(self, interaction):
        reason = self.reason.value.strip()
        if not reason:
            return await reply(interaction, "Укажите причину.")
        await self.bot.execute(interaction, self.action, self.member_id, self.destination, reason)

    async def on_error(self, interaction, error):
        logging.error("Кадровая форма: %s", error, exc_info=error)
        await reply(interaction, "Не удалось завершить обработку. Проверьте журнал и роли участника.")


if __name__ == "__main__":
    load_dotenv()
    token = (os.getenv("BOT_TOKEN") or os.getenv("DISCORD_TOKEN") or "").strip()
    if not token:
        raise SystemExit("Укажите BOT_TOKEN в настройках хостинга.")
    try:
        cfg = Config.load(os.getenv("KADRO_CONFIG", "config.json"))
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise SystemExit(f"Заполните config.json по образцу. {error}") from error
    AuditClient(cfg).run(token)
