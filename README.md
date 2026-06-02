# Discord 勤怠管理 Bot

Discordのボタンから出勤・退勤を記録し、本人が勤怠を修正できるBotです。
修正した記録には「修正済み」と表示され、変更前後・理由・操作者・修正時刻がSQLiteと監査チャンネルに残ります。

## 機能

- `出勤` ボタン: 出勤中でなければ現在時刻を記録
- `退勤` ボタン: 出勤中の記録に現在時刻を記録
- `修正` ボタン: 自分の直近の記録をフォームで修正
- `/勤怠一覧`: 自分の直近10件を表示。修正済みの記録も判別可能
- `/修正履歴`: 自分の直近10件の修正履歴を表示
- `/勤怠パネル`: 管理権限を持つ人が操作パネルを設置

記録はBotを再起動しても `attendance.db` に残ります。

## Raspberry Piへセットアップ

1. Discord Developer PortalでBotを作成します。
2. OAuth2 URL Generatorで `bot` と `applications.commands` を選び、Botをサーバーへ招待します。
3. Bot Permissionsには最低限 `View Channels`、`Send Messages`、`Embed Links` を付与します。
4. Raspberry Pi OS上でこのリポジトリを取得し、依存関係をインストールします。

```bash
sudo apt update
sudo apt install -y git python3 python3-venv
git clone git@github.com:nwnwnw1/discord-attendance-bot.git
cd discord-attendance-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env
```

5. `.env` の `DISCORD_TOKEN` と `AUDIT_CHANNEL_ID` を設定します。
6. Botを起動します。

```bash
python3 bot.py
```

7. Discordで `/勤怠パネル` を一度実行します。

## Raspberry Piで自動起動

Privateリポジトリを取得するため、事前にRaspberry PiのSSH公開鍵をGitHubへ登録してください。

`discord-attendance.service.example` を参考に `/etc/systemd/system/discord-attendance.service` を作成します。
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

## 修正の扱い

`修正` ボタンを押すと直近の記録が初期値として表示されます。
過去の記録を直す場合は `/勤怠一覧` で確認した記録IDへ書き換えてください。

時刻は `YYYY-MM-DD HH:MM` 形式で入力します。未退勤へ戻す場合、退勤時刻には `-` を入力します。
