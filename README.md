# Discord 勤怠管理 Bot

Discordのボタンから出勤・退勤を記録し、本人が勤怠を修正できるBotです。
修正した記録には「修正済み」と表示され、変更前後・理由・操作者・修正時刻がSQLiteと監査チャンネルに残ります。

## 機能

- `出勤` ボタン: 出勤中でなければ現在時刻を記録
- `退勤` ボタン: 出勤中の記録に現在時刻を記録
- `修正` ボタン: 自分の直近の記録をフォームで修正
- `勤怠一覧` ボタン: 一覧専用パネルから自分の指定月の勤怠記録を表示。月を省略すると当月を表示
- `/勤怠一覧`: 自分の指定月の勤怠記録を表示。月を省略すると当月を表示
- `/修正履歴`: 自分の直近10件の修正履歴を表示
- `/勤怠パネル`: 管理権限を持つ人が操作パネルを設置
- `/勤怠一覧パネル`: 管理権限を持つ人が一覧専用パネルを手動設置

記録はBotを再起動しても `attendance.db` に残ります。

## Raspberry Piへセットアップ

1. Discord Developer PortalでBotを作成します。
2. OAuth2 URL Generatorで `bot` と `applications.commands` を選び、Botをサーバーへ招待します。
3. Bot Permissionsには最低限 `View Channels`、`Send Messages`、`Embed Links` を付与します。
4. Raspberry Pi OS上でこのリポジトリを取得します。

```bash
sudo apt update
sudo apt install -y git python3 python3-venv
git clone https://github.com/nwnwnw1/discord-attendance-bot.git
cd discord-attendance-bot
cp .env.example .env
nano .env
```

5. `.env` の `DISCORD_TOKEN`、`AUDIT_CHANNEL_ID`、`ATTENDANCE_LIST_CHANNEL_ID` を設定します。
6. セットアップスクリプトを実行します。

```bash
chmod +x scripts/install-raspberry-pi.sh
sudo ./scripts/install-raspberry-pi.sh
```

このスクリプトは `.venv` の作成、依存関係のインストール、systemdサービスの登録、有効化を行います。

7. Discordで `/勤怠パネル` を一度実行します。一覧用チャンネルにはBot起動時に自動で一覧専用パネルが表示されます。

手動で起動する場合は次のコマンドを使います。

```bash
.venv/bin/python bot.py
```

## Raspberry Piで自動起動

`scripts/install-raspberry-pi.sh` は `/etc/systemd/system/discord-attendance.service` を自動作成します。

手動で作成する場合は、`discord-attendance.service.example` を参考に `/etc/systemd/system/discord-attendance.service` を作成します。
`YOUR_USER` と `/home/YOUR_USER/discord-attendance-bot` は実際のユーザー名と配置先に置き換えてください。

```ini
[Unit]
Description=Discord Attendance Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/home/YOUR_USER/discord-attendance-bot
Environment=PYTHONUNBUFFERED=1
ExecStart=/home/YOUR_USER/discord-attendance-bot/.venv/bin/python bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

サービスを有効化します。

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now discord-attendance
sudo systemctl status discord-attendance
```

ログを確認します。

```bash
journalctl -u discord-attendance -f
```

Botを更新する場合は、リポジトリをpullして依存関係を更新してからサービスを再起動します。

```bash
cd /home/YOUR_USER/discord-attendance-bot
git pull
source .venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart discord-attendance
```

勤怠データは `attendance.db` に保存されます。バックアップする場合はBotを止めてからコピーしてください。

```bash
sudo systemctl stop discord-attendance
cp attendance.db attendance.db.backup
sudo systemctl start discord-attendance
```

## 修正の扱い

`修正` ボタンを押すと直近の記録が初期値として表示されます。
過去の記録を直す場合は `/勤怠一覧` で確認した記録IDへ書き換えてください。

時刻は `YYYY-MM-DD HH:MM` 形式で入力します。未退勤へ戻す場合、退勤時刻には `-` を入力します。

## 月別一覧

一覧専用パネルの `勤怠一覧` ボタン、または `/勤怠一覧` で自分の指定月の勤怠を表示できます。
月は `YYYY-MM` 形式で指定します。月を省略した場合は当月の一覧を表示します。

一覧専用パネルを自動表示するには、`.env` の `ATTENDANCE_LIST_CHANNEL_ID` に一覧用チャンネルIDを設定します。
Botは起動時にそのチャンネルへ一覧専用パネルを作成します。既に作成済みの場合は重複作成しません。
