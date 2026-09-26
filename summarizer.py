import anthropic

MODEL = "claude-haiku-4-5"

SYSTEM_PROMPT = """あなたはユーザーの日記を読み、期間の振り返りをまとめるアシスタントです。
日記に書かれていることだけを根拠にし、書かれていない出来事や感情を推測で補わないでください。
出力はDiscordにそのまま投稿されます。Discordのマークダウンで、全体を1500字以内にまとめてください。

出力形式:
**📌 ハイライト**
- 印象的な出来事を3〜5個

**😊 良かったこと**
- 1〜3個

**💭 気になったこと・課題**
- 0〜3個（無ければ省略）

**🔁 よく出てきたテーマ**
- 1〜3個

**💬 ひとこと**
前向きで押し付けがましくないコメントを1〜2文。"""


async def summarize(client: anthropic.AsyncAnthropic, entries: list[tuple[str, str]], label: str) -> str:
    diary_text = "\n\n".join(f"## {d}\n{c}" for d, c in entries)
    response = await client.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"以下は{label}の日記です。振り返りをまとめてください。\n\n{diary_text}"}],
    )
    if response.stop_reason == "refusal":
        return "（この期間の要約は生成できませんでした）"
    return "".join(b.text for b in response.content if b.type == "text").strip()
