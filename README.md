# AI-Phone

Talk to ChatGPT or Claude through an old analogue telephone. Lift the handset and the AI answers; interrupt it mid-sentence like a real conversation. AI-Phone can also ring *you*: to read out a reminder, deliver a message from your home automation, or start a conversation on a schedule.

Everything is managed from a web interface: API keys, AI profiles and prompts, Asterisk and ATA settings, outbound calls and schedules. No configuration file needs to be edited by hand.

```
 analogue phone ──(2 wires)── Grandstream HT801/HT802 ──(SIP/RTP)── Asterisk ──(AudioSocket)── AI-Phone ──► OpenAI Realtime
  (PTT T65, GPO 746, …)          (ATA: line power,                       │                       │       └─► Deepgram → Claude → TTS
                                  ringing, pulse dialing)               AMI ◄── outbound calls ──┘
                                                                                                web interface :8080
```

## Features

- **Full-duplex conversations with barge-in.** Start talking while the AI speaks and it stops within one 20 ms audio frame; the model is told exactly how much you heard.
- **Two AI back-ends**
  - **OpenAI Realtime** (`gpt-realtime`, `gpt-realtime-mini`): speech-to-speech, the lowest latency (about 0.6 to 1.1 s).
  - **Claude**: Deepgram streaming speech-to-text → Claude (streamed, spoken sentence by sentence) → text-to-speech from Deepgram Aura, ElevenLabs or OpenAI.
  - **Echo test**: plays your voice back, to test the phone line without any API key.
- **Profiles = phone numbers.** Each profile has its own number, model, voice, prompt and greeting. Lift the handset and wait for the default AI, or dial `1` for Claude, `2` for ChatGPT, and so on (works with a rotary dial).
- **The AI can hang up** after saying goodbye (an `end_call` tool).
- **Outbound calls**: ring the phone now from the web interface, on a schedule (once, daily, chosen weekdays), or via a webhook (Home Assistant, cron, doorbell…). Choose between reading out a message or starting a conversation, with retries when nobody answers.
- **Speed dial to real phones**: connect a SIP account (for example CheapConnect) and give mobile numbers a short number, so dialling `5` rings your mobile. The phone can only call these numbers, nothing else.
- **Self-update** from the Settings page: check GitHub for a new version and install it with one click, or automatically every night.
- **Call log with transcripts**, live while a call runs.
- **Cost protection**: maximum call length and hang-up after silence (a handset left off the hook won't run up a bill).

## What you need

| Part | Notes |
| --- | --- |
| An analogue telephone | Any normal a/b phone: a PTT T65 with rotary dial, a GPO 746 replica, … Not a digital system phone from an office PBX. |
| Grandstream HT801 or HT802 | The ATA powers the phone line, makes it ring, and understands pulse dialing. |
| A Linux machine or VM | Ubuntu Server 26.04 LTS, 1 CPU and 1 GB RAM are plenty. In a VM, use **bridged networking** and a **fixed IP address**. |
| API keys | OpenAI for ChatGPT. Anthropic + Deepgram for Claude (ElevenLabs optional). |

## Install

On a fresh Ubuntu Server 26.04 LTS:

```bash
sudo apt install -y git
git clone https://github.com/AnykeyNL/AI-Phone.git
cd AI-Phone
sudo ./install.sh --timezone Europe/Amsterdam
```

The installer:

- installs Asterisk from the Ubuntu repositories (or builds Asterisk 22 from source if the package is unavailable, or when you pass `--asterisk-from-source`),
- installs AI-Phone in `/opt/aiphone` with its own Python environment and a `aiphone` system user,
- saves your original Asterisk configuration to `/etc/asterisk.before-aiphone` and points Asterisk at configuration that AI-Phone generates,
- opens the firewall ports if `ufw` is active (8080/tcp web, 5060/udp SIP, 10000-10200/udp RTP),
- starts the `asterisk` and `aiphone` services.

Then open `http://<server-ip>:8080`:

1. Choose an admin password.
2. **Settings**: paste your API keys (each has a *Test* button).
3. **Phone & ATA**: copy the values into the HT801/HT802 web interface (the page lists every field). The port turns *connected* when the ATA has registered.
4. Dial `600` for the echo test, then hang up, lift the handset and wait: the AI answers.

Options: `--port 8080` (web port), `--timezone Europe/Amsterdam`, `--asterisk-from-source`.

**Upgrade**: use *Check for updates* on the Settings page, or `git pull && sudo ./install.sh`. Settings, profiles and the call log are kept.

**Uninstall**: `sudo ./uninstall.sh` (add `--purge` to delete settings and the call log).

**Forgot the password?** `sudo aiphone-reset-password`, then open the web interface to choose a new one.

## Grandstream HT801/HT802 settings

The *Phone & ATA* page shows the exact values for your installation. In short, on the **FXS Port** tab:

| Field | Value |
| --- | --- |
| Primary SIP Server | the server's IP address |
| SIP User ID / Authenticate ID | `line1` (port 1), `line2` (port 2) |
| Authenticate Password | shown on the *Phone & ATA* page |
| Offhook Auto-Dial | the default profile's number, e.g. `100` |
| Offhook Auto-Dial Delay | `3` seconds, so you can still dial another profile |
| Enable Pulse Dialing | Yes, for rotary phones |
| Enable Line Echo Canceller (LEC) | Yes |
| Impedance | European / CTR21 instead of 600 Ω (less echo) |
| Preferred Vocoder | PCMU, then PCMA |

## Speed dial through a SIP account

On the *Speed dial* page, enter your SIP account (for CheapConnect: server `sip.cheapconnect.net`, plus the SIP username and password from your CheapConnect account) and add speed-dial numbers, e.g. `5` → `0612345678`. Lift the handset, dial `5` within the auto-dial delay, and the call goes out through the SIP account.

For safety the phone can only call the numbers on this page: every other number is refused, and incoming calls from the SIP account are rejected. The dashboard shows whether the SIP account is registered. If calls connect but you hear nothing, fill in your public IP address under *Advanced*.

## Updating

**From the web interface:** on the *Settings* page, under *Updates*, save a GitHub token (the repository is private) and click *Save and check for updates*. When a new version is available, *Update now* downloads it and re-runs the installer; AI-Phone restarts within a minute or two and keeps all settings. Tick *Install updates automatically every night* to let it update itself (it waits until no call is active).

Create the token at GitHub → Settings → Developer settings → [Fine-grained tokens](https://github.com/settings/personal-access-tokens/new): give it access to only this repository with the permission *Contents: Read-only*.

**From the command line:** `git pull && sudo ./install.sh` in your clone.

## Outbound calls and webhooks

From the *Outbound calls* page you can ring the phone immediately or create schedules. Other systems can use the webhook (the token is shown on that page):

```bash
curl -X POST http://<server-ip>:8080/api/webhook/<token> \
  -H 'Content-Type: application/json' \
  -d '{"text": "The washing machine is done.", "mode": "message"}'

# or simply
curl "http://<server-ip>:8080/api/webhook/<token>?text=Dinner+is+ready"
```

| Field | Meaning |
| --- | --- |
| `text` | The message to read out, or what the conversation is about |
| `mode` | `message` (default) or `conversation` |
| `allow_reply` | `true` to keep talking after the message |
| `extension` | Which profile to use, by its number |
| `line` | `line1` or `line2` |
| `retries`, `retry_delay` | Try again when nobody answers |

`POST /api/call` accepts the same JSON with `Authorization: Bearer <token>`. Interactive API docs are at `/api/docs`.

Home Assistant example (`configuration.yaml`):

```yaml
rest_command:
  ring_phone:
    url: "http://192.168.1.20:8080/api/webhook/<token>"
    method: POST
    content_type: "application/json"
    payload: '{"text": "{{ message }}", "mode": "message", "allow_reply": true}'
```

## How it works

- The ATA registers with **Asterisk** (PJSIP). Every profile number in the dialplan asks AI-Phone for a call id (`CURL` to `127.0.0.1`) and then streams the call with **AudioSocket**: raw 16-bit, 8 kHz audio over a local TCP socket.
- **AI-Phone** (one Python process, `python -m aiphone`) runs the AudioSocket server, the AI providers, the scheduler and the web interface (FastAPI). Playback is paced in 20 ms frames, so there is never more than about 40 ms of AI audio in flight and barge-in is immediate.
- For **OpenAI Realtime**, audio is sent and received as G.711 µ-law (the phone's native format, no resampling). OpenAI's semantic voice activity detection decides when you're done talking; on interruption AI-Phone clears playback and sends `conversation.item.truncate`.
- For **Claude**, Deepgram's partial transcripts detect barge-in; Claude's streamed answer is cut into sentences and each is synthesised while the next is still being written.
- Outbound calls use the Asterisk Manager Interface (`Originate`), bound to `127.0.0.1` only.
- All settings live in SQLite (`/var/lib/aiphone/aiphone.db`). Asterisk's `pjsip.conf`, `extensions.conf` and `rtp.conf` are generated into `/etc/asterisk/aiphone/` and reloaded from the web interface.

### Files

| Path | What |
| --- | --- |
| `/opt/aiphone` | Application and Python environment |
| `/var/lib/aiphone/aiphone.db` | Settings, profiles, schedules, call log |
| `/var/lib/aiphone/bootstrap.json` | Web port and AMI credentials (written by the installer) |
| `/etc/asterisk/aiphone/` | Generated Asterisk configuration |
| `journalctl -u aiphone -u asterisk` | Logs (also on the *System log* page) |

## Tips for natural conversations

- Keep prompts short and phone-like ("answer in one to three sentences").
- If the AI interrupts itself because it hears its own echo, enable LEC in the ATA, lower the *Output volume* of the profile, raise the *Speech threshold* (OpenAI, server VAD) or *Words needed to interrupt* (Claude).
- A carbon microphone (old PTT phones) sounds muffled; an electret replacement capsule improves speech recognition a lot.
- `gpt-realtime-mini` and Claude Haiku are the cheapest and fastest options.

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest
AIPHONE_DATA_DIR=./data AIPHONE_ASTERISK_DIR=./data/asterisk python -m aiphone --port 8080
pytest
```

The tests run a fake Asterisk AudioSocket client against the real call manager, with fake OpenAI, Deepgram and Claude back-ends, and check barge-in behaviour.

## Security

The web interface is meant for your home network. It is protected by a password, but don't expose port 8080 to the internet without a VPN or a reverse proxy with HTTPS. The webhook token acts as a password: keep it private, and create a new one on the *Outbound calls* page if it leaks.

## License

MIT
