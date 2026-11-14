import asyncio
from copy import deepcopy
from collections import Counter
import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

import discord
from discord import app_commands
from dotenv import load_dotenv

BRAND = "💀 ПГО КАРАТЕЛИ"
COLOR = 0x871C30
PROTECTED_RANK = "Заместитель основателя"
HIRE_DEPARTMENT = "Отдел логистики и снабжения"
ACTIONS = {
    "hire": ("🤝", "Принять"),
    "promote": ("📈", "Повышение"),
    "demote": ("📉", "Понижение"),
    "transfer": ("🔄", "Перевод"),
    "dismiss": ("🚪", "Уволить"),
    "blacklist": ("⛔", "Занести в чёрный список"),
    "unblacklist": ("✅", "Вынести из чёрного списка"),
    "grant": ("🎖️", "Назначить на должность"),
}


STATUS_ACTIONS = {"dismiss": "Уволен", "blacklist": "Чёрный список", "unblacklist": "Чёрный список"}
BLACKLIST_TITLES = {
    "blacklist": "⛔ Занесён в чёрный список",
    "unblacklist": "✅ Вынесен из чёрного списка",
}


def blacklist_log_channel_id(config):
    channel_id = config.blacklist_channel
    if not channel_id or channel_id in (config.journal, config.panel):
        raise ValueError("Укажите отдельный канал для ЧС в blacklist_channel_id, не канал аудита или панели.")
    return channel_id


def action_log_channel_id(config, action):
    return blacklist_log_channel_id(config) if action in BLACKLIST_TITLES else config.journal


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
    roles = tuple(snowflake(role) for role in values)
    if len(roles) != len(set(roles)):
        raise ValueError("Не повторяйте один ID внутри списка одного отдела.")
    return roles


def shared_department_roles(departments):
    counts = Counter(role for bundle in departments.values() for role in bundle)
    return {role for role, count in counts.items() if count > 1}


def member_department(config, current):
    shared = shared_department_roles(config.departments)
    markers = current - shared
    units = [unit for unit, bundle in config.departments.items() if markers.intersection(bundle)]
    if len(units) != 1:
        raise ValueError("Для перевода у участника должна быть уникальная роль ровно одного отдела.")
    return units[0]


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
    blacklist_channel: int | None = None

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
        if not set(STATUS_ACTIONS.values()) <= groups[2].keys():
            raise ValueError("В dismissal_statuses нужны роли «Уволен» и «Чёрный список».")
        guild, panel, journal, senior = (snowflake(raw[k]) for k in (
            "guild_id", "panel_channel_id", "audit_channel_id", "senior_role_id"))
        chief = snowflake(raw["family_chief_role_id"])
        founder = snowflake(raw["founder_role_id"])
        if founder in (guild, senior):
            raise ValueError("Укажите отдельный ID роли Основателя.")
        if list(groups[0])[-1] != PROTECTED_RANK:
            raise ValueError(f"Последним в ranks укажите «{PROTECTED_RANK}».")
        ids = [role for index, group in enumerate(groups) if index != 1 for role in group.values()]
        department_roles = {role for bundle in groups[1].values() for role in bundle}
        shared = shared_department_roles(groups[1])
        for name, bundle in groups[1].items():
            if not set(bundle) - shared:
                raise ValueError(f"У отдела «{name}» должна быть хотя бы одна уникальная роль.")
        ids.extend(department_roles)
        if len(ids) != len(set(ids)) or guild in ids or {senior, chief, founder} & set(ids) or len({guild, senior, chief}) != 3:
            raise ValueError("ID ролей званий, отделов, статусов, Начальника семьи и старшего состава должны различаться.")
        if panel == journal:
            raise ValueError("Панель и кадровый журнал должны находиться в разных каналах.")
        tags = raw.get("department_tags", {})
        if not isinstance(tags, dict) or any(not isinstance(tag, str) or not 1 <= len(tag.strip()) <= 10
                                             or not tag.replace(" ", "").isalnum() for tag in tags.values()):
            raise ValueError("В department_tags укажите короткие обозначения отделов без знаков.")
        return cls(guild, panel, journal, senior, chief, founder, *groups, tags,
                   os.getenv("KADRO_DB", "karateli_kadro.sqlite3"),
                   snowflake(raw["blacklist_channel_id"]) if raw.get("blacklist_channel_id") else None)


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
        or (action in ("promote", "demote", "dismiss", "blacklist") and protected in current)
    ):
        raise ValueError("Только Начальник семьи может выдать или снять роль «Заместитель основателя».")


def blacklist_days(value):
    text = value.strip().lower()
    if text == "бессрочно":
        return None
    if text.isascii() and text.isdecimal() and len(text) <= 4 and 1 <= int(text) <= 3650:
        return int(text)
    raise ValueError("Укажите срок от 1 до 3650 дней или «бессрочно».")


def family_role_id(config, guild_roles):
    configured = config.extra_roles.get("Сотрудник семьи")
    if configured is not None:
        return configured
    ids = [role.id for role in guild_roles
           if role.name == "Сотрудник семьи" and not role.managed and not role.is_default()]
    reserved = {config.chief, config.founder, config.senior, *config.ranks.values(),
                *config.statuses.values(), *config.extra_roles.values()}
    reserved.update(role_id for bundle in config.departments.values() for role_id in bundle)
    return ids[0] if len(ids) == 1 and ids[0] not in reserved else None


def require_dismissible(config, current, base_role=None):
    if config.statuses["Чёрный список"] in current:
        raise ValueError("Участник в чёрном списке. Сначала вынесите его из списка.")
    base_role = config.extra_roles.get("Сотрудник семьи", base_role)
    if base_role is None:
        raise ValueError("Укажите ID базовой роли «Сотрудник семьи» в extra_roles в config.json.")
    if base_role not in current:
        raise ValueError("⛔ **Увольнение недоступно**\n"
                         "У участника нет роли **«Сотрудник семьи»**.\n"
                         "Он уже считается уволенным.")
    return base_role


def plan_roles(config, current, action, destination, department=None, *, managed_roles=(), base_role=None):
    if config.statuses["Чёрный список"] in current and action in ("hire", "promote", "demote", "transfer", "grant"):
        raise ValueError("Сначала вынесите участника из чёрного списка.")
    if action == "hire":
        base_role = config.extra_roles.get("Сотрудник семьи", base_role)
        if base_role is None:
            raise ValueError("Укажите ID базовой роли «Сотрудник семьи» в extra_roles в config.json.")
        if base_role in current and config.statuses["Уволен"] not in current:
            raise ValueError("Участник уже принят: у него есть роль **«Сотрудник семьи»**.")
        trainee = config.ranks.get("Стажёр") or config.ranks.get("Стажер")
        ols_roles = set(config.departments.get(HIRE_DEPARTMENT, ()))
        if trainee is None or len(ols_roles) < 3:
            raise ValueError("Для приёма настройте звание «Стажёр» и три роли ОЛС в config.json.")
        other_ranks = set(config.ranks.values()) - {trainee}
        other_departments = {role for bundle in config.departments.values() for role in bundle} - ols_roles
        if current & (other_ranks | other_departments):
            raise ValueError("У участника уже есть другое звание или отдел. Используйте кадровый перевод или повышение.")
        return (current - {config.statuses["Уволен"]}) | {base_role, trainee} | ols_roles
    ranks = list(config.ranks.values())
    departments = {role for bundle in config.departments.values() for role in bundle}
    shared = shared_department_roles(config.departments)
    unique_department_roles = departments - shared
    active_ranks = current & set(ranks)
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
        old_unit = member_department(config, current)
        if destination not in config.departments or destination == old_unit:
            raise ValueError("Выберите другой настроенный отдел.")
        return (current - unique_department_roles) | (set(config.departments[destination]) - shared)
    if action == "grant":
        if destination not in config.extra_roles.values():
            raise ValueError("Эта дополнительная роль не настроена.")
        if department not in config.departments:
            raise ValueError("Выберите настроенный отдел.")
        position_roles = {role for name, role in config.extra_roles.items() if name != "Сотрудник семьи"}
        old_positions = position_roles if destination in position_roles else set()
        return ((current - unique_department_roles - old_positions)
                | (set(config.departments[department]) - shared) | {destination})
    if action in STATUS_ACTIONS:
        status = config.statuses[STATUS_ACTIONS[action]]
        if action == "unblacklist":
            if status not in current:
                raise ValueError("Участник не в чёрном списке.")
            return (current - {status}) | {config.statuses["Уволен"]}
        remaining = (current & set(managed_roles)) | {status}
        if action == "dismiss":
            remaining.discard(require_dismissible(config, current, base_role))
        elif action == "blacklist":
            remaining.add(config.statuses["Уволен"])
        return remaining
    raise ValueError("Неизвестная операция.")


def department_tag(config, department_name):
    return config.department_tags.get(department_name) or "".join(
        word[0].upper() for word in department_name.split() if len(word) > 1)[:10] or "ОТДЕЛ"


LEADERSHIP_PREFIXES = {
    "Начальник отдела": "Нач",
    "Зам. начальника отдела": "Зам.Нач",
    "Заместитель начальника отдела": "Зам.Нач",
}
POSITION_TAGS = {"Начальник", "Зам", "Зам. начальника", "Зам. нач.", "Сотрудник",
                 "Стажёр", "Стажер", *LEADERSHIP_PREFIXES.values()}


def appointment_nick(config, destination, department, name):
    position = next((name for name, rid in config.extra_roles.items() if rid == destination), None)
    unit = department if department in config.departments else None
    if not position or not unit:
        raise ValueError("Должность или отдел не настроены.")
    prefixes = {**LEADERSHIP_PREFIXES, "Сотрудник семьи": "Сотрудник"}
    prefix = prefixes.get(position, position)
    base = clean_member_name(config, name)
    nick = f"{prefix} {department_tag(config, unit)} | {' '.join(base.split())}"
    if not base or len(nick) > 32:
        raise ValueError("Укажите имя короче: весь новый никнейм должен занимать не более 32 символов.")
    return nick


def suggested_name(config, label):
    prefix, separator, name = label.partition(" | ")
    managed = POSITION_TAGS | set(config.extra_roles)
    tags = {department_tag(config, unit) for unit in config.departments}
    return name if separator and prefix.casefold() in {f"{p} {t}".casefold() for p in managed for t in tags} else label


STATUS_NICK_PREFIX = re.compile(r"^(?:\[(?:ЧС\s*\+\s*УВОЛЕН|ЧС|УВОЛЕН)\]\s*|"
                                r"(?:ЧС\s*\+\s*УВОЛЕН|ЧС|УВОЛЕН)\s*\|\s*)",
                                re.IGNORECASE)


def clean_member_name(config, label):
    base = label.strip()
    tags = {department_tag(config, unit).casefold() for unit in config.departments}
    bracket_positions = {position.casefold() for position in POSITION_TAGS}
    while base:
        cleaned = suggested_name(config, STATUS_NICK_PREFIX.sub("", base, count=1)).strip()
        tag, separator, rest = cleaned.partition("]")
        if separator and tag.startswith("["):
            position, _, unit = tag[1:].casefold().strip().rpartition(" ")
            if unit in tags and position in bracket_positions:
                cleaned = rest.strip()
        if cleaned == base:
            break
        base = cleaned
    return base


def status_nick(config, action, label, fallback):
    base = clean_member_name(config, label) or fallback.strip()
    prefix = {
        "blacklist": "[ЧС + УВОЛЕН] ",
        "hire": f"[Стажёр {department_tag(config, HIRE_DEPARTMENT)}] ",
    }.get(action, "[УВОЛЕН] ")
    return (prefix + base)[:32].rstrip()


def transfer_nick(config, old_unit, new_unit, label, fallback, roles):
    old_tag = re.escape(department_tag(config, old_unit))
    new_tag = department_tag(config, new_unit)
    positions = POSITION_TAGS | set(config.extra_roles) | set(config.ranks)
    prefixes = "|".join(re.escape(p) for p in sorted(positions, key=len, reverse=True))
    for pattern, template in (
        (rf"^\[({prefixes})\s+{old_tag}\]\s*(.*)$", "[{position} {tag}] "),
        (rf"^({prefixes})\s+{old_tag}\s*\|\s*(.*)$", "{position} {tag} | "),
    ):
        match = re.match(pattern, label.strip(), re.IGNORECASE)
        if match:
            prefix = template.format(position=match[1], tag=new_tag)
            return (prefix + (match[2].strip() or fallback))[:32].rstrip()
    position = next((short for key, short in LEADERSHIP_PREFIXES.items()
                     if config.extra_roles.get(key) in roles), None)
    if position is None:
        trainee = config.ranks.get("Стажёр") or config.ranks.get("Стажер")
        position = "Стажёр" if trainee in roles else "Сотрудник"
    return (f"[{position} {new_tag}] " + (label.strip() or fallback))[:32].rstrip()


def embed(title, description=None):
    card = discord.Embed(title=title, description=description, color=COLOR,
                         timestamp=discord.utils.utcnow())
    card.set_footer(text=f"{BRAND} • Кадровый аудит")
    return card


async def send_personal_notice(target, actor, card, action, record):
    """Notify only the affected member, using text rather than server mentions."""
    notice = discord.Embed.from_dict(deepcopy(card.to_dict()))
    def role_name(match):
        role = target.guild.get_role(int(match[1]))
        return f"**{discord.utils.escape_markdown(role.name)}**" if role else "Роль недоступна"

    for index, field in enumerate(notice.fields):
        value = re.sub(r"<@&(\d+)>", role_name, field.value)
        if value != field.value:
            notice.set_field_at(index, name=field.name, value=value[:1024], inline=field.inline)
    notice.title = {
        "hire": "🤝 Тебя приняли на службу",
        "promote": "📈 Тебя повысили",
        "demote": "📉 Твоё звание изменено",
        "transfer": "🔄 Тебя перевели в другой отдел",
        "dismiss": "🚪 Ты уволен из состава",
        "blacklist": "⛔ Тебя внесли в чёрный список",
        "unblacklist": "✅ Тебя вынесли из чёрного списка",
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
        self.expiry_task = None
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
            db.execute("""CREATE TABLE IF NOT EXISTS blacklist_expiry (
                member INTEGER PRIMARY KEY, expires INTEGER NOT NULL
            )""")
        self.add_view(MainPanel(self))
        await self.tree.sync(guild=discord.Object(self.config.guild))

    async def on_ready(self):
        logging.info("ПГО КАРАТЕЛИ подключён. Сервер %s", self.config.guild)
        if self.expiry_task is None or self.expiry_task.done():
            self.expiry_task = asyncio.create_task(self.expire_blacklist())

    async def expire_blacklist(self):
        conf = self.config
        while not self.is_closed():
            try:
                guild = self.get_guild(conf.guild)
                if guild is not None:
                    async with self.lock:
                        with sqlite3.connect(conf.database) as db:
                            due = db.execute("SELECT member FROM blacklist_expiry WHERE expires <= ?",
                                             (int(time.time()),)).fetchall()
                        for (member_id,) in due:
                            await self.expire_entry(guild, member_id)
            except Exception:
                logging.exception("Ошибка проверки сроков ЧС")
            await asyncio.sleep(60)

    async def expire_entry(self, guild, member_id):
        conf = self.config
        role = guild.get_role(conf.statuses["Чёрный список"])
        if role is None:
            return
        try:
            member = await guild.fetch_member(member_id)
            had_blacklist = role in member.roles
            if had_blacklist:
                dismissed = guild.get_role(conf.statuses["Уволен"])
                if dismissed is None:
                    logging.warning("Не найдена роль «Уволен» при снятии ЧС у %s", member_id)
                    return
                if dismissed not in member.roles:
                    await member.add_roles(dismissed, reason="Истёк срок чёрного списка")
                await member.remove_roles(role, reason="Истёк срок чёрного списка")
        except discord.NotFound:
            pass
        except discord.HTTPException:
            logging.warning("Не удалось снять истёкший ЧС у %s, повторим позже", member_id)
            return
        else:
            if had_blacklist:
                new_nick = status_nick(conf, "unblacklist", member.display_name, member.name)
                try:
                    if new_nick != member.nick:
                        await member.edit(nick=new_nick, reason="Истёк срок чёрного списка")
                except discord.HTTPException:
                    logging.warning("ЧС снят, но не удалось изменить никнейм участника %s", member_id)
        with sqlite3.connect(conf.database) as db:
            db.execute("DELETE FROM blacklist_expiry WHERE member=?", (member_id,))
        if conf.blacklist_channel:
            try:
                card = embed(BLACKLIST_TITLES["unblacklist"])
                card.add_field(name="Участник", value=f"<@{member_id}> · `{member_id}`")
                card.add_field(name="Причина", value="Срок чёрного списка истёк.", inline=False)
                await self.channel(guild, blacklist_log_channel_id(conf)).send(embed=card)
            except (ValueError, discord.HTTPException):
                logging.warning("Не удалось опубликовать окончание ЧС у %s", member_id)

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
                "Нажмите **«Подать аудит»**, чтобы открыть личное меню кадровых действий.\n\n"
                "🤝 **Принять** оформляет стажёра в ОЛС и снимает «Уволен».\n"
                "📈 **Повышение** и 📉 **Понижение** меняют звание, сохраняя отдел.\n"
                "🔄 **Перевод** меняет отдел, сохраняя звание.\n"
                "🚪 **Уволить** снимает обычные роли и выдаёт «Уволен».\n"
                "⛔ **Занести в чёрный список** выдаёт «Уволен» и «Чёрный список».\n"
                "✅ **Вынести из чёрного списка** снимает ЧС и оставляет «Уволен».\n\n"
                "🎖️ **Назначить на должность** — только Основатель и Заместитель основателя.\n\n"
                "🔒 Остальные действия доступны старшему составу и Начальнику семьи. "
                "Роль «Заместитель основателя» может выдать или снять только Начальник семьи."),
                view=MainPanel(self))
            await reply(interaction, f"✅ Панель опубликована. [Открыть]({message.jump_url})")
        except (ValueError, discord.HTTPException) as exc:
            logging.warning("Не удалось опубликовать панель: %s", exc)
            await reply(interaction, str(exc) if isinstance(exc, ValueError)
                        else "Не удалось опубликовать панель. Проверьте права бота и логи.")

    async def execute(self, interaction, action, member_id, destination, reason, department=None, name=None, days=None):
        if not self.can_review(interaction, action):
            return await reply(interaction, "У вас нет доступа к этому действию.")
        if member_id == interaction.user.id:
            return await reply(interaction, "Нельзя проводить кадровое действие над собой.")
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        conf = self.config
        is_blacklist_action = action in BLACKLIST_TITLES
        async with self.lock:
            try:
                channel = self.channel(guild, action_log_channel_id(conf, action))
                expires = int(time.time()) + days * 86400 if action == "blacklist" and days is not None else None
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
                new = plan_roles(conf, current, action, destination, department,
                                 managed_roles={r.id for r in target.roles if r.managed},
                                 base_role=family_role_id(conf, guild.roles) if action in ("hire", "dismiss") else None)
                new_nick = None
                old_display_name = target.display_name
                if action == "grant":
                    if not guild.me.guild_permissions.manage_nicknames:
                        raise ValueError("Боту не хватает права «Управлять никнеймами».")
                    new_nick = appointment_nick(conf, destination, department, name or "")
                    if new == current and new_nick == target.nick:
                        raise ValueError("Участник уже на этой должности, никнейм совпадает.")
                elif action == "hire":
                    candidate = status_nick(conf, action, old_display_name, target.name)
                    if candidate != target.nick:
                        if not guild.me.guild_permissions.manage_nicknames:
                            raise ValueError("Боту не хватает права «Управлять никнеймами», чтобы поставить приставку стажёра.")
                        new_nick = candidate
                elif action == "transfer":
                    old_unit = member_department(conf, current)
                    candidate = transfer_nick(conf, old_unit, destination, old_display_name, target.name, current)
                    if candidate != target.nick:
                        if not guild.me.guild_permissions.manage_nicknames:
                            raise ValueError("Боту не хватает права «Управлять никнеймами» для перевода.")
                        new_nick = candidate
                elif action in STATUS_ACTIONS:
                    candidate = status_nick(conf, action, old_display_name, target.name)
                    if candidate != target.nick:
                        if not guild.me.guild_permissions.manage_nicknames:
                            raise ValueError("Боту не хватает права «Управлять никнеймами» для изменения статуса.")
                        new_nick = candidate
                    elif new == current:
                        raise ValueError("Этот статус уже установлен, никнейм совпадает.")
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
                lines = []
                size = 0
                for role in roles:
                    line = f"• {role.mention}"
                    if size + len(line) + 1 > 1022:
                        lines.append("…")
                        break
                    lines.append(line)
                    size += len(line) + 1
                return "\n".join(lines) or "—"

            mark, label = ACTIONS[action]
            title = {"hire": "🤝 Принят на службу", "transfer": "🔄 Переведён в другой отдел"}
            card = embed(title.get(action, BLACKLIST_TITLES.get(action, f"{mark} {label.upper()}")))
            card.add_field(name="Участник", value=f"{target.mention}\nID: `{target.id}`")
            card.add_field(name="Кадровик", value=f"{actor.mention}\n{discord.utils.escape_markdown(actor.display_name)}")
            fields = {
                "hire": ("Снятые роли", "Итоговый статус"),
                "promote": ("Было звание", "Стало звание"),
                "demote": ("Было звание", "Стало звание"),
                "transfer": ("Снятые роли", "Итоговый статус"),
                "dismiss": ("Снятые роли", "Итоговый статус"),
                "blacklist": ("Снятые роли", "Итоговый статус"),
                "unblacklist": ("Снятая роль", "Результат"),
                "grant": ("Снятые роли", "Должность"),
            }
            previous, updated = fields[action]
            card.add_field(name=previous, value=names(before), inline=False)
            outcome = ("Принят на службу · Стажёр ОЛС" if action == "hire" else
                       f"Переведён · {department_tag(conf, destination)}" if action == "transfer" else
                       f"Исключён из чёрного списка. Роль <@&{conf.statuses['Уволен']}> остаётся." if action == "unblacklist" else
                       f"<@&{conf.statuses['Уволен']}>, <@&{conf.statuses['Чёрный список']}>" if action == "blacklist" else
                       f"<@&{conf.statuses[STATUS_ACTIONS[action]]}>" if action in STATUS_ACTIONS else
                       guild.get_role(destination).mention if action == "grant" else names(after))
            card.add_field(name=updated, value=outcome, inline=False)
            if action == "transfer":
                card.add_field(name="Отдел", value=f"{old_unit} → {destination}", inline=False)
                card.add_field(name="Выданные роли", value=names(after), inline=False)
            if action in ("grant", "hire"):
                card.add_field(name="Отдел", value=HIRE_DEPARTMENT if action == "hire" else department, inline=False)
                card.add_field(name="Выданные роли", value=names(after), inline=False)
            if new_nick is not None:
                card.add_field(name="Никнейм", value=f"{discord.utils.escape_markdown(old_display_name)} → "
                               f"{discord.utils.escape_markdown(new_nick)}", inline=False)
            card.add_field(name="Причина", value=reason, inline=False)
            if action == "blacklist":
                term = f"{days} дн. · до <t:{expires}:f>" if expires else "Бессрочно"
                card.add_field(name="Срок", value=term, inline=False)
            record = None
            expiry_saved = True
            if action in ("blacklist", "unblacklist"):
                try:
                    with sqlite3.connect(conf.database) as db:
                        if expires is None:
                            db.execute("DELETE FROM blacklist_expiry WHERE member=?", (member_id,))
                        else:
                            db.execute("INSERT OR REPLACE INTO blacklist_expiry VALUES (?, ?)", (member_id, expires))
                except sqlite3.Error:
                    logging.exception("Не удалось сохранить срок ЧС")
                    expiry_saved = False
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
                log = await channel.send(embed=card, allowed_mentions=discord.AllowedMentions.none())
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
                label = "Запись в канале ЧС" if is_blacklist_action else "Запись в журнале"
                result = f"✅ Готово. [{label}]({log.jump_url})"
            else:
                backup = f" Запись №{record} сохранена в базе." if record else " Сохранить запись в базе также не удалось."
                label = "канал ЧС" if is_blacklist_action else "канал аудита"
                result = f"⚠️ Действие выполнено, но запись в {label} не отправилась." + backup
            result += "\n📩 Личное сообщение отправлено." if dm_sent else "\n⚠️ Не удалось отправить ЛС участнику."
            if not expiry_saved:
                result += "\n⚠️ Срок ЧС не сохранён. Проверьте базу и снимите ЧС вручную при необходимости."
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

    @discord.ui.button(label="Подать аудит", emoji="📋", style=discord.ButtonStyle.danger,
                       custom_id="pgo:kadro:submit_audit")
    async def open_audit(self, interaction, button):
        await interaction.response.send_message(
            "**📋 Подача кадрового аудита**\nВыберите действие из списка.",
            view=ActionMenu(self.bot, interaction), ephemeral=True)


class ActionMenu(StaffView):
    def __init__(self, bot, interaction):
        super().__init__(bot)
        menu = discord.ui.Select(
            placeholder="Выберите кадровое действие",
            options=[discord.SelectOption(label=label, value=action, emoji=emoji)
                     for action, (emoji, label) in ACTIONS.items()
                     if bot.can_review(interaction, action)])

        async def callback(interaction):
            action = menu.values[0]
            if not bot.can_review(interaction, action):
                return await reply(interaction, "У вас нет доступа к этому действию.")
            await interaction.response.edit_message(
                content=f"**{ACTIONS[action][1]}** · выберите участника.",
                view=MemberStep(bot, action))
        menu.callback = callback
        self.add_item(menu)


class MemberStep(StaffView):
    def __init__(self, bot, action):
        super().__init__(bot)
        self.action = action

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Выберите участника")
    async def member(self, interaction, menu):
        member = menu.values[0]
        if self.action == "dismiss":
            if not isinstance(member, discord.Member) or interaction.guild is None:
                return await reply(interaction, "Выберите участника этого сервера.")
            config = self.bot.config
            try:
                require_dismissible(config, {role.id for role in member.roles},
                                    family_role_id(config, interaction.guild.roles))
            except ValueError as exc:
                return await reply(interaction, str(exc))
        if self.action == "hire":
            if not isinstance(member, discord.Member) or interaction.guild is None or member.bot:
                return await reply(interaction, "Выберите участника этого сервера, а не бота.")
            config = self.bot.config
            try:
                plan_roles(config, {role.id for role in member.roles}, "hire", None,
                           base_role=family_role_id(config, interaction.guild.roles))
            except ValueError as exc:
                return await reply(interaction, str(exc))
        if self.action == "blacklist":
            return await interaction.response.send_modal(BlacklistForm(self.bot, member.id))
        if self.action in STATUS_ACTIONS or self.action == "hire":
            return await interaction.response.send_modal(
                ReasonForm(self.bot, self.action, member.id, None))
        await interaction.response.edit_message(
            content=f"Участник {member.mention}. " + (
                "Выберите должность." if self.action == "grant" else "Выберите итоговую роль."),
            view=RoleStep(self.bot, self.action, member.id, {r.id for r in interaction.user.roles},
                          clean_member_name(self.bot.config, member.display_name) or member.name))


class RoleStep(StaffView):
    def __init__(self, bot, action, member_id, actor_roles, name=""):
        super().__init__(bot)
        self.action = action
        conf = bot.config
        roles = {"transfer": conf.departments,
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


class BlacklistForm(discord.ui.Modal, title="Занесение в чёрный список"):
    duration = discord.ui.TextInput(label="Срок в днях или «бессрочно»", max_length=12)
    reason = discord.ui.TextInput(label="Причина", max_length=700, style=discord.TextStyle.paragraph)

    def __init__(self, bot, member_id):
        super().__init__(timeout=300)
        self.bot, self.member_id = bot, member_id

    async def on_submit(self, interaction):
        try:
            days = blacklist_days(self.duration.value)
            reason = self.reason.value.strip()
            if not reason:
                raise ValueError("Укажите причину.")
        except ValueError as exc:
            return await reply(interaction, str(exc))
        await self.bot.execute(interaction, "blacklist", self.member_id, None, reason, days=days)


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
        cfg = Config.load(os.getenv("KADRO_CONFIG") or Path(__file__).resolve().with_name("config.json"))
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise SystemExit(f"Заполните config.json по образцу. {error}") from error
    AuditClient(cfg).run(token)
