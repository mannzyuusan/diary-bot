# 日記リマインドBot

Discordの日記チャンネルに投稿するだけで日記として記録し、書き忘れをリマインド、定期的にClaudeが振り返りをまとめるBotです。

## 機能

| 機能 | 内容 |
|---|---|
| 質問形式で記入 | `/diary` またはリマインドの「✏️日記を書く」ボタンでフォームが開き、「今日あったこと」「うれしかったこと・悲しかったこと」「明日やること」に答えるだけで記録（1問目以外は空欄OK） |
| 自由記入 | 日記チャンネルへの自分の投稿を保存（✅リアクション、その日最初の投稿で連続日数を返信）。編集・削除も反映 |
| リマインド | 23:00 / 翌1:00 に今日の日記が未記入なら通知。GitHub Actions で動くのでPCの電源が切れていても届く |
| 週まとめ | 毎週月曜 7:00 に先週分をAIが要約して投稿 |
| 月まとめ | 毎月1日 7:00 に先月分をAIが要約して投稿 |
| `/streak` | 連続日数・今月の記入日数 |
| `/summary days:N` | 直近N日分をその場で要約 |

- 朝4時より前の投稿は前日の日記扱い（`DAY_START_HOUR` で変更可）
- Bot停止中の投稿は、次回起動時に自動で取り込み
- 要約は `summaries/` にMarkdownでも保存

## セットアップ

### 1. Discord Botを作る
1. https://discord.com/developers/applications → New Application
2. **Bot** タブ → Reset Token でトークンを取得
3. 同じタブの **Privileged Gateway Intents** で **MESSAGE CONTENT INTENT** をON
4. **OAuth2 > URL Generator** で scope に `bot` と `applications.commands`、権限に `View Channels` / `Send Messages` / `Read Message History` / `Add Reactions` を選び、生成URLから自分のサーバーに招待

### 2. IDを調べる
Discordの 設定 > 詳細設定 > **開発者モード** をON → サーバー・日記チャンネル・自分のアイコンを右クリック →「IDをコピー」

### 3. 設定ファイル
`.env.example` を `.env` にコピーして値を埋める。Claude APIキーは https://platform.claude.com/ で発行。

### 4. 起動
`run.bat` をダブルクリック（初回のみ下記でライブラリをインストール）

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

PCログイン時に自動起動したい場合は、`Win+R` →`shell:startup` を開き、`run.bat` のショートカットを置く。

## 費用の目安
要約は Claude Haiku 4.5 を使用。週・月の自動まとめ合わせて月20円程度（Claude Proプランとは別に、APIクレジットのチャージが必要）。

## リマインド（GitHub Actions）

`.github/workflows/remind.yml` が毎日 23:00 / 翌1:00 (JST) に `remind_action.py` を実行する。
Discordのチャンネル履歴から記入状況を判定するので、PCのBotが止まっていても動く（数分〜十数分遅れることがある）。

リポジトリの Settings → Secrets and variables → Actions に以下を登録する：
`DISCORD_TOKEN` / `DIARY_CHANNEL_ID` / `OWNER_ID`

動作テストは Actions タブ → 日記リマインド → Run workflow（`force` をONにすると記入済みでも送信）。
