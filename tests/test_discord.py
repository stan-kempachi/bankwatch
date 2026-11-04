import httpx

from bankwatch import discord as d


class Recorder:
    def __init__(self, responses=None):
        self.calls, self.responses = [], list(responses or [])

    def __call__(self, url, json=None, headers=None, timeout=None):
        self.calls.append((url, json, headers or {}))
        code, body = self.responses.pop(0) if self.responses else (200, {})
        return httpx.Response(code, json=body, request=httpx.Request("POST", url))


def test_bot_mode_posts_to_channel(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(d.httpx, "post", rec)
    n = d.DiscordNotifier(webhook_url="", channel_id="123", bot_token="tok")
    assert n.mode == "bot"
    n.send_text("hello")
    url, payload, headers = rec.calls[0]
    assert url.endswith("/channels/123/messages")
    assert headers["Authorization"] == "Bot tok"
    assert "username" not in payload


def test_webhook_mode(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(d.httpx, "post", rec)
    n = d.DiscordNotifier(webhook_url="https://discord.com/api/webhooks/1/x", channel_id="", bot_token="")
    assert n.mode == "webhook"
    n.send_text("hi")
    assert rec.calls[0][1]["username"] == "BankWatch"


def test_bot_preferred_over_webhook():
    n = d.DiscordNotifier(webhook_url="https://w", channel_id="1", bot_token="t")
    assert n.mode == "bot"


def test_token_read_from_shared_env(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text('FOO=1\nDISCORD_BOT_TOKEN="abc.def"\n')
    monkeypatch.setattr(d.settings, "discord_token_file", str(f))
    monkeypatch.setattr(d.settings, "discord_bot_token", "")
    monkeypatch.setattr(d.settings, "discord_channel_id", "42")
    assert d.DiscordNotifier().bot_token == "abc.def"


def test_retry_on_429(monkeypatch):
    rec = Recorder([(429, {"retry_after": 0.01}), (200, {})])
    monkeypatch.setattr(d.httpx, "post", rec)
    d.DiscordNotifier(webhook_url="", channel_id="1", bot_token="t").send_text("x")
    assert len(rec.calls) == 2


def test_disabled_does_not_post(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(d.httpx, "post", rec)
    d.DiscordNotifier(webhook_url="", channel_id="", bot_token="").send_text("x")
    assert rec.calls == []
