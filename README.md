# Shir · שיר

**Live Hebrew lyrics for Spotify on your Mac, with English translation and transliteration, synced line by line.**

Play a Hebrew song in the Spotify desktop app. Shir finds the lyrics, translates them, and highlights each line as it's sung, with the Hebrew, how to pronounce it, and what it means. Click any line to jump there.

<img src="screenshot.png" width="420" alt="Shir showing Shlomo Artzi's מלך העולם with transliteration and English">

## How it works

1. **Spotify desktop app** → what's playing, and where you are in the song (via AppleScript; no Spotify login needed)
2. **[LRCLIB](https://lrclib.net)** → time-synced lyrics (free). Spotify often writes Hebrew songs in English letters ("Mami Ze Nigmar"), so Shir first guesses the Hebrew spelling ("מאמי זה נגמר") to search with.
3. **[Jev](https://typesafe.ai)** (TypeSafe's decision model) → for each search result, answers "same song, by the same artist?", so you get no lyrics rather than the wrong song's lyrics
4. **DeepSeek** → English translation + transliteration, translated in parallel chunks with the whole song as context (about 3 seconds)
5. **Jev** again → double-checks every translated line; shaky ones get a small gold dot •
6. Everything is **cached** in `~/.shir/cache`, so replays are instant and free

If LRCLIB doesn't have a song, Shir tries [lyrics.ovh](https://lyrics.ovh). If that fails too, it offers a **Search Shironet** button and a **Paste lyrics** box, and translates whatever you paste.

## Setup

You need macOS, the Spotify desktop app, and Python 3.9+ (already on most Macs). Shir uses only the Python standard library, so there's nothing to install.

**1. Get the code**
```bash
git clone https://github.com/admiller007/shir.git ~/shir
```

**2. Get an [OpenRouter](https://openrouter.ai/keys) API key** and add a few dollars of credit. One key covers both DeepSeek and Jev.

**3. Save your settings**
```bash
mkdir -p ~/.shir && cat > ~/.shir/config.json <<'JSON'
{
  "llm_api_key": "sk-or-v1-YOUR-KEY",
  "llm_base_url": "https://openrouter.ai/api/v1",
  "llm_model": "deepseek/deepseek-v4-pro"
}
JSON
```

**4. Run it** (with a song playing in Spotify)
```bash
cd ~/shir && python3 shir.py
```
A browser tab opens. The first time, macOS asks whether Terminal can control Spotify. Click **OK**.

To get a floating window that stays on top, run `pip3 install pywebview` and then `python3 shir.py --float`. You can also double-click `Shir.command`.

**Update** to the latest version:
```bash
cd ~/shir && git pull
```

## Cost

| | per new song |
|---|---|
| Jev (matching + translation check) | ~$0.0002 |
| DeepSeek v4-pro translation | ~$0.005 |
| DeepSeek v4-flash translation (cheaper, rougher transliteration) | ~$0.001 |

Replays cost nothing because results are cached. In testing, v4-pro was noticeably better than v4-flash at transliteration, Hebrew gender forms, and slang.

## How well it works

In a test on 24 Hebrew songs written the way Spotify shows them (21 in LRCLIB, 3 not):

| | right lyrics | wrong song's lyrics |
|---|---|---|
| without Jev | 22 / 24 | 2 |
| with Jev | **24 / 24** | **0** |

In a separate test, 12 of 76 translated lines were deliberately corrupted. Jev's check flagged all 12, and it falsely flagged 1 of the 64 correct lines.

## Keyboard

`space` play/pause · `←` `→` previous/next · `h` `t` `e` show/hide Hebrew, transliteration, English · **A− / A+** text size

## Options

`~/.shir/config.json` keys:

| key | default | |
|---|---|---|
| `llm_api_key` | | OpenRouter (or DeepSeek) API key |
| `llm_base_url` | `https://api.deepseek.com` | any OpenAI-compatible API |
| `llm_model` | `deepseek-chat` | e.g. `deepseek/deepseek-v4-pro` |
| `jev_openrouter_model` | `typesafe/jev-1.14` | Jev runs automatically with an OpenRouter key |
| `typesafe_api_key` | | optional: call Jev directly through TypeSafe instead |

Other commands:
- `python3 shir.py --demo "מלך העולם" "שלמה ארצי"` previews the app without Spotify, using a fake player.
- `rm ~/.shir/cache/*.json` clears saved songs.

## Privacy

Shir runs locally on your Mac. The only things that leave it are lyrics searches (to LRCLIB and lyrics.ovh) and the song's title, artist, and lyrics (to OpenRouter, for translation and Jev). Your API key stays in `~/.shir/config.json`.

Lyrics are copyrighted by their authors. Shir is meant for personal listening.

## License

MIT
