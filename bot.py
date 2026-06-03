import asyncio
import json
import logging
import os
import sqlite3
from calendar import monthrange
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv


load_dotenv()

JST = ZoneInfo("Asia/Tokyo")
DATABASE_PATH = os.getenv("DATABASE_PATH", "attendance.db")
AUDIT_CHANNEL_ID = int(os.getenv("AUDIT_CHANNEL_ID", "0"))
ATTENDANCE_LIST_CHANNEL_ID = int(os.getenv("ATTENDANCE_LIST_CHANNEL_ID", "0"))

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("attendance-bot")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def format_datetime(value: str | None) -> str:
    if not value:
        return "未打刻"
    return datetime.fromisoformat(value).astimezone(JST).strftime("%Y-%m-%d %H:%M")


def format_time(value: str | None) -> str:
    if not value:
        return "未打刻"
    return datetime.fromisoformat(value).astimezone(JST).strftime("%H:%M")


def format_clock_out_label(session: sqlite3.Row) -> str:
    if session["clock_out"]:
        return f"🔴退勤 `{format_time(session['clock_out'])}`"
    if session["missing_clock_out"]:
        return "⚠️退勤 `未打刻`"
    return "🔴退勤 `未打刻`"


def parse_jst_datetime(value: str, field_name: str) -> str | None:
    value = value.strip()
    if not value or value == "-":
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d %H:%M").replace(tzinfo=JST)
    except ValueError as exc:
        raise ValueError(f"{field_name}は `YYYY-MM-DD HH:MM` 形式で入力してください。") from exc
    return parsed.astimezone(timezone.utc).isoformat()


def parse_jst_month(value: str | None) -> datetime:
    if not value:
        return datetime.now(JST).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    try:
        return datetime.strptime(value.strip(), "%Y-%m").replace(tzinfo=JST)
    except ValueError as exc:
        raise ValueError("月は `YYYY-MM` 形式で入力してください。") from exc


def stale_clock_out_cutoff(clock_in: str) -> datetime:
    clock_in_jst = datetime.fromisoformat(clock_in).astimezone(JST)
    next_day = clock_in_jst.date() + timedelta(days=1)
    return datetime(next_day.year, next_day.month, next_day.day, 5, 0, tzinfo=JST)


class AttendanceDatabase:
    def __init__(self, path: str) -> None:
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.lock = asyncio.Lock()
        self._create_tables()

    def _create_tables(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS work_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                clock_in TEXT NOT NULL,
                clock_out TEXT,
                missing_clock_out INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS attendance_audits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                session_id INTEGER NOT NULL,
                editor_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                before_json TEXT,
                after_json TEXT,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_sessions_user
                ON work_sessions(guild_id, user_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_audits_session
                ON attendance_audits(guild_id, session_id, id DESC);
            """
        )
        columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(work_sessions)").fetchall()
        }
        if "missing_clock_out" not in columns:
            self.connection.execute(
                "ALTER TABLE work_sessions ADD COLUMN missing_clock_out INTEGER NOT NULL DEFAULT 0"
            )
        self.connection.commit()

    async def clock_in(self, guild_id: int, user_id: int) -> tuple[sqlite3.Row, list[sqlite3.Row]]:
        async with self.lock:
            stale_sessions = self.mark_stale_open_sessions(guild_id, user_id)
            open_session = self.connection.execute(
                """
                SELECT * FROM work_sessions
                WHERE guild_id = ? AND user_id = ? AND clock_out IS NULL AND missing_clock_out = 0
                ORDER BY id DESC LIMIT 1
                """,
                (guild_id, user_id),
            ).fetchone()
            if open_session:
                raise ValueError(f"すでに出勤済みです。記録ID: `{open_session['id']}`")

            now = utc_now()
            cursor = self.connection.execute(
                """
                INSERT INTO work_sessions(guild_id, user_id, clock_in, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (guild_id, user_id, now, now, now),
            )
            self.connection.commit()
            return self.get_session(cursor.lastrowid), stale_sessions

    async def clock_out(self, guild_id: int, user_id: int) -> sqlite3.Row:
        async with self.lock:
            self.mark_stale_open_sessions(guild_id, user_id)
            session = self.connection.execute(
                """
                SELECT * FROM work_sessions
                WHERE guild_id = ? AND user_id = ? AND clock_out IS NULL AND missing_clock_out = 0
                ORDER BY id DESC LIMIT 1
                """,
                (guild_id, user_id),
            ).fetchone()
            if not session:
                raise ValueError("出勤中の記録がありません。")

            now = utc_now()
            self.connection.execute(
                "UPDATE work_sessions SET clock_out = ?, updated_at = ? WHERE id = ?",
                (now, now, session["id"]),
            )
            self.connection.commit()
            return self.get_session(session["id"])

    def mark_stale_open_sessions(self, guild_id: int, user_id: int) -> list[sqlite3.Row]:
        now_jst = datetime.now(JST)
        open_sessions = self.connection.execute(
            """
            SELECT * FROM work_sessions
            WHERE guild_id = ? AND user_id = ? AND clock_out IS NULL AND missing_clock_out = 0
            ORDER BY id ASC
            """,
            (guild_id, user_id),
        ).fetchall()
        stale_sessions = [
            session for session in open_sessions if now_jst >= stale_clock_out_cutoff(session["clock_in"])
        ]
        if not stale_sessions:
            return []

        now = utc_now()
        self.connection.executemany(
            "UPDATE work_sessions SET missing_clock_out = 1, updated_at = ? WHERE id = ?",
            [(now, session["id"]) for session in stale_sessions],
        )
        self.connection.commit()
        return stale_sessions

    def get_session(self, session_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM work_sessions WHERE id = ?", (session_id,)
        ).fetchone()

    def latest_session(self, guild_id: int, user_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            """
            SELECT * FROM work_sessions
            WHERE guild_id = ? AND user_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (guild_id, user_id),
        ).fetchone()

    def list_sessions(self, guild_id: int, user_id: int, month_start: datetime) -> list[sqlite3.Row]:
        self.mark_stale_open_sessions(guild_id, user_id)
        if month_start.month == 12:
            next_month = month_start.replace(year=month_start.year + 1, month=1)
        else:
            next_month = month_start.replace(month=month_start.month + 1)
        start_utc = month_start.astimezone(timezone.utc).isoformat()
        end_utc = next_month.astimezone(timezone.utc).isoformat()
        return self.connection.execute(
            """
            SELECT s.*,
                   EXISTS(
                       SELECT 1 FROM attendance_audits a
                       WHERE a.session_id = s.id AND a.guild_id = s.guild_id
                   ) AS corrected
            FROM work_sessions s
            WHERE s.guild_id = ? AND s.user_id = ? AND s.clock_in >= ? AND s.clock_in < ?
            ORDER BY s.clock_in ASC, s.id ASC
            """,
            (guild_id, user_id, start_utc, end_utc),
        ).fetchall()

    def list_audits(self, guild_id: int, user_id: int, limit: int = 10) -> list[sqlite3.Row]:
        return self.connection.execute(
            """
            SELECT a.* FROM attendance_audits a
            INNER JOIN work_sessions s ON s.id = a.session_id
            WHERE a.guild_id = ? AND s.user_id = ?
            ORDER BY a.id DESC LIMIT ?
            """,
            (guild_id, user_id, limit),
        ).fetchall()

    def get_setting(self, key: str) -> str | None:
        row = self.connection.execute(
            "SELECT value FROM bot_settings WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        self.connection.execute(
            """
            INSERT INTO bot_settings(key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        self.connection.commit()

    async def correct_session(
        self,
        guild_id: int,
        owner_id: int,
        editor_id: int,
        session_id: int,
        clock_in: str,
        clock_out: str | None,
        reason: str,
    ) -> tuple[sqlite3.Row, sqlite3.Row]:
        async with self.lock:
            session = self.get_session(session_id)
            if not session or session["guild_id"] != guild_id or session["user_id"] != owner_id:
                raise ValueError("対象の勤怠記録が見つかりません。")
            if clock_out and datetime.fromisoformat(clock_out) < datetime.fromisoformat(clock_in):
                raise ValueError("退勤時刻は出勤時刻より後にしてください。")
            if not clock_out:
                other_open_session = self.connection.execute(
                    """
                    SELECT id FROM work_sessions
                    WHERE guild_id = ? AND user_id = ? AND clock_out IS NULL
                      AND missing_clock_out = 0 AND id != ?
                    LIMIT 1
                    """,
                    (guild_id, owner_id, session_id),
                ).fetchone()
                if other_open_session:
                    raise ValueError(
                        f"記録ID `{other_open_session['id']}` が出勤中のため、未退勤には変更できません。"
                    )

            before = dict(session)
            now = utc_now()
            self.connection.execute(
                """
                UPDATE work_sessions
                SET clock_in = ?, clock_out = ?, missing_clock_out = 0, updated_at = ?
                WHERE id = ?
                """,
                (clock_in, clock_out, now, session_id),
            )
            after = dict(self.get_session(session_id))
            cursor = self.connection.execute(
                """
                INSERT INTO attendance_audits(
                    guild_id, session_id, editor_id, action,
                    before_json, after_json, reason, created_at
                )
                VALUES (?, ?, ?, 'CORRECT', ?, ?, ?, ?)
                """,
                (
                    guild_id,
                    session_id,
                    editor_id,
                    json.dumps(before, ensure_ascii=False),
                    json.dumps(after, ensure_ascii=False),
                    reason,
                    now,
                ),
            )
            self.connection.commit()
            audit = self.connection.execute(
                "SELECT * FROM attendance_audits WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            return self.get_session(session_id), audit


db = AttendanceDatabase(DATABASE_PATH)


def session_text(session: sqlite3.Row) -> str:
    return (
        f"記録ID: `{session['id']}`\n"
        f"出勤: `{format_datetime(session['clock_in'])}`\n"
        f"退勤: `{format_datetime(session['clock_out'])}`"
    )


def session_status_embed(title: str, message: str, session: sqlite3.Row) -> discord.Embed:
    is_missing = bool(session["missing_clock_out"])
    is_working = session["clock_out"] is None and not is_missing
    status = "⚠️ 退勤未打刻" if is_missing else "🟢 出勤中" if is_working else "🔵 退勤済み"
    color = discord.Color.orange() if is_missing else discord.Color.green() if is_working else discord.Color.blue()
    embed = discord.Embed(title=title, description=message, color=color)
    embed.add_field(name="状態", value=status, inline=False)
    embed.add_field(name="記録ID", value=f"`#{session['id']}`")
    embed.add_field(name="出勤", value=f"🟢 `{format_datetime(session['clock_in'])}`")
    embed.add_field(name="退勤", value=format_clock_out_label(session))
    return embed


def attendance_embed(message: str | None = None) -> discord.Embed:
    description = (
        "🟢 出勤、🔴 退勤、📝 修正をボタンで記録できます。\n"
        "押した後は本人だけに現在の状態が表示されます。"
    )
    if message:
        description = f"{message}\n\n{description}"
    return discord.Embed(title="🕒 勤怠管理", description=description, color=discord.Color.blue())


def attendance_list_embed() -> discord.Embed:
    return discord.Embed(
        title="勤怠一覧",
        description="ボタンから自分の月別勤怠を確認できます。",
        color=discord.Color.green(),
    )


def chunk_lines(lines: list[str], limit: int = 1900) -> list[str]:
    chunks: list[str] = []
    current = ""
    for line in lines:
        next_value = f"{current}\n{line}" if current else line
        if len(next_value) > limit and current:
            chunks.append(current)
            current = line
        else:
            current = next_value
    if current:
        chunks.append(current)
    return chunks


def monthly_attendance_chunks(guild_id: int, user_id: int, month_start: datetime) -> list[str]:
    sessions = db.list_sessions(guild_id, user_id, month_start)
    label = month_start.strftime("%Y-%m")
    weekdays = ["月", "火", "水", "木", "金", "土", "日"]
    sessions_by_day: dict[int, list[sqlite3.Row]] = {}
    for session in sessions:
        day = datetime.fromisoformat(session["clock_in"]).astimezone(JST).day
        sessions_by_day.setdefault(day, []).append(session)

    lines = [f"**📅 {label} の勤怠一覧**"]
    _, last_day = monthrange(month_start.year, month_start.month)
    for day in range(1, last_day + 1):
        day_value = month_start.replace(day=day)
        weekday = weekdays[day_value.weekday()]
        day_label = f"{month_start.month:02}/{day:02}({weekday})"
        day_sessions = sessions_by_day.get(day, [])
        if not day_sessions:
            lines.append(f"`{day_label}` ⚪ 勤怠なし")
            continue

        for index, session in enumerate(day_sessions):
            corrected = " 📝修正済み" if session["corrected"] else ""
            prefix = f"`{day_label}`" if index == 0 else "`          `"
            lines.append(
                f"{prefix} 🟢出勤 `{format_time(session['clock_in'])}` / "
                f"{format_clock_out_label(session)} "
                f"`#{session['id']}`{corrected}"
            )
    return chunk_lines(lines)


async def send_audit_log(interaction: discord.Interaction, audit: sqlite3.Row) -> None:
    if not AUDIT_CHANNEL_ID:
        return
    channel = interaction.client.get_channel(AUDIT_CHANNEL_ID)
    if not isinstance(channel, discord.TextChannel):
        log.warning("AUDIT_CHANNEL_ID does not point to a cached text channel")
        return

    before = json.loads(audit["before_json"])
    after = json.loads(audit["after_json"])
    embed = discord.Embed(title="勤怠記録が修正されました", color=discord.Color.orange())
    embed.add_field(name="記録ID", value=str(audit["session_id"]))
    embed.add_field(name="修正者", value=f"<@{audit['editor_id']}>")
    embed.add_field(
        name="変更前",
        value=f"出勤: `{format_datetime(before['clock_in'])}`\n退勤: `{format_datetime(before['clock_out'])}`",
        inline=False,
    )
    embed.add_field(
        name="変更後",
        value=f"出勤: `{format_datetime(after['clock_in'])}`\n退勤: `{format_datetime(after['clock_out'])}`",
        inline=False,
    )
    embed.add_field(name="理由", value=audit["reason"], inline=False)
    try:
        await channel.send(embed=embed)
    except discord.HTTPException:
        log.exception("Failed to send the correction audit message")


async def send_monthly_attendance(
    interaction: discord.Interaction, guild_id: int, user_id: int, month_start: datetime
) -> None:
    chunks = monthly_attendance_chunks(guild_id, user_id, month_start)
    await interaction.response.send_message(
        chunks[0], view=AttendanceListView(), ephemeral=True
    )
    for chunk in chunks[1:]:
        await interaction.followup.send(chunk, ephemeral=True)


class CorrectionModal(discord.ui.Modal, title="勤怠記録の修正"):
    session_id = discord.ui.TextInput(label="記録ID", placeholder="例: 12", max_length=12)
    clock_in = discord.ui.TextInput(label="出勤時刻", placeholder="YYYY-MM-DD HH:MM")
    clock_out = discord.ui.TextInput(
        label="退勤時刻", placeholder="YYYY-MM-DD HH:MM または未打刻なら -", required=False
    )
    reason = discord.ui.TextInput(label="修正理由", style=discord.TextStyle.paragraph, max_length=300)

    def __init__(self, session: sqlite3.Row) -> None:
        super().__init__()
        self.session_id.default = str(session["id"])
        self.clock_in.default = format_datetime(session["clock_in"])
        self.clock_out.default = format_datetime(session["clock_out"]) if session["clock_out"] else "-"

    async def on_submit(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id
        try:
            session_id = int(self.session_id.value)
            clock_in = parse_jst_datetime(self.clock_in.value, "出勤時刻")
            clock_out = parse_jst_datetime(self.clock_out.value, "退勤時刻")
            if not clock_in:
                raise ValueError("出勤時刻は必須です。")
            if not self.reason.value.strip():
                raise ValueError("修正理由は必須です。")
            session, audit = await db.correct_session(
                interaction.guild_id,
                interaction.user.id,
                interaction.user.id,
                session_id,
                clock_in,
                clock_out,
                self.reason.value.strip(),
            )
            await send_audit_log(interaction, audit)
            await interaction.response.send_message(
                embed=session_status_embed(
                    "📝 勤怠を修正しました",
                    "修正履歴にも記録済みです。",
                    session,
                ),
                view=AttendanceView(),
                ephemeral=True,
            )
        except (ValueError, TypeError) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)


class AttendanceListModal(discord.ui.Modal, title="勤怠一覧"):
    month = discord.ui.TextInput(
        label="表示する月",
        placeholder="YYYY-MM。空欄なら当月",
        required=False,
        max_length=7,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id
        try:
            month_start = parse_jst_month(self.month.value)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await send_monthly_attendance(interaction, interaction.guild_id, interaction.user.id, month_start)


class AttendanceView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="🟢 出勤", style=discord.ButtonStyle.success, custom_id="attendance:clock_in")
    async def clock_in(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        assert interaction.guild_id
        try:
            session, stale_sessions = await db.clock_in(interaction.guild_id, interaction.user.id)
            message = "現在は出勤中です。退勤するときは `🔴 退勤` を押してください。"
            if stale_sessions:
                stale_ids = ", ".join(f"`#{stale['id']}`" for stale in stale_sessions)
                message = (
                    f"前回の退勤忘れを未打刻として扱いました: {stale_ids}\n"
                    "新しい出勤を記録しました。退勤するときは `🔴 退勤` を押してください。"
                )
            await interaction.response.send_message(
                embed=session_status_embed(
                    "🟢 出勤しました",
                    message,
                    session,
                ),
                view=AttendanceView(),
                ephemeral=True,
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)

    @discord.ui.button(label="🔴 退勤", style=discord.ButtonStyle.danger, custom_id="attendance:clock_out")
    async def clock_out(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        assert interaction.guild_id
        try:
            session = await db.clock_out(interaction.guild_id, interaction.user.id)
            await interaction.response.send_message(
                embed=session_status_embed(
                    "🔴 退勤しました",
                    "お疲れさまでした。退勤済みとして記録されています。",
                    session,
                ),
                view=AttendanceView(),
                ephemeral=True,
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)

    @discord.ui.button(label="📝 修正", style=discord.ButtonStyle.secondary, custom_id="attendance:correct")
    async def correct(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        assert interaction.guild_id
        session = db.latest_session(interaction.guild_id, interaction.user.id)
        if not session:
            await interaction.response.send_message("⚪ 修正できる勤怠記録がありません。", ephemeral=True)
            return
        await interaction.response.send_modal(CorrectionModal(session))


class AttendanceListView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="勤怠一覧", style=discord.ButtonStyle.primary, custom_id="attendance_list:open")
    async def list_attendance(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(AttendanceListModal())


class AttendanceBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.attendance_list_panel_ready = False

    async def setup_hook(self) -> None:
        self.add_view(AttendanceView())
        self.add_view(AttendanceListView())
        await self.tree.sync()

    async def on_ready(self) -> None:
        if self.attendance_list_panel_ready:
            return
        self.attendance_list_panel_ready = True
        await self.ensure_attendance_list_panel()

    async def ensure_attendance_list_panel(self) -> None:
        if not ATTENDANCE_LIST_CHANNEL_ID:
            return

        channel = self.get_channel(ATTENDANCE_LIST_CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.fetch_channel(ATTENDANCE_LIST_CHANNEL_ID)
            except discord.HTTPException:
                log.exception("Failed to fetch ATTENDANCE_LIST_CHANNEL_ID")
                return

        if not isinstance(channel, discord.TextChannel):
            log.warning("ATTENDANCE_LIST_CHANNEL_ID does not point to a text channel")
            return

        setting_key = f"attendance_list_panel_message_id:{ATTENDANCE_LIST_CHANNEL_ID}"
        saved_message_id = db.get_setting(setting_key)
        if saved_message_id:
            try:
                await channel.fetch_message(int(saved_message_id))
                return
            except (ValueError, discord.NotFound):
                pass
            except discord.HTTPException:
                log.exception("Failed to fetch the saved attendance list panel message")
                return

        try:
            message = await channel.send(embed=attendance_list_embed(), view=AttendanceListView())
        except discord.HTTPException:
            log.exception("Failed to send the attendance list panel")
            return
        db.set_setting(setting_key, str(message.id))
        log.info("Attendance list panel installed in channel %s", ATTENDANCE_LIST_CHANNEL_ID)


bot = AttendanceBot()


@bot.tree.command(name="勤怠パネル", description="出勤・退勤・修正ボタンを設置します")
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_guild=True)
async def attendance_panel(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(embed=attendance_embed(), view=AttendanceView())


@bot.tree.command(name="勤怠一覧パネル", description="勤怠一覧ボタンだけのパネルを設置します")
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_guild=True)
async def attendance_list_panel(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(embed=attendance_list_embed(), view=AttendanceListView())


@bot.tree.command(name="勤怠一覧", description="自分の指定月の勤怠記録を表示します")
@app_commands.guild_only()
@app_commands.describe(月="表示する月。例: 2026-06。未入力なら当月")
async def attendance_list(interaction: discord.Interaction, 月: str | None = None) -> None:
    assert interaction.guild_id
    try:
        month_start = parse_jst_month(月)
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return

    await send_monthly_attendance(interaction, interaction.guild_id, interaction.user.id, month_start)


@bot.tree.command(name="修正履歴", description="自分の直近の勤怠修正履歴を表示します")
@app_commands.guild_only()
async def correction_history(interaction: discord.Interaction) -> None:
    assert interaction.guild_id
    audits = db.list_audits(interaction.guild_id, interaction.user.id)
    if not audits:
        await interaction.response.send_message("修正履歴はありません。", ephemeral=True)
        return
    lines = [
        f"`記録#{audit['session_id']}` {format_datetime(audit['created_at'])} "
        f"理由: {audit['reason']}"
        for audit in audits
    ]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@attendance_panel.error
async def attendance_panel_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "勤怠パネルの設置にはサーバー管理権限が必要です。", ephemeral=True
        )
        return
    raise error


@attendance_list_panel.error
async def attendance_list_panel_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "勤怠一覧パネルの設置にはサーバー管理権限が必要です。", ephemeral=True
        )
        return
    raise error


token = os.getenv("DISCORD_TOKEN")
if not token:
    raise RuntimeError("DISCORD_TOKEN is not configured")

bot.run(token)
