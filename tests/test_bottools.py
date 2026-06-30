from obby_jukebox import bottools
from obby_jukebox.bottools import BotTools, catalog


class _FakeIrc:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, str]]] = []

    def tagmsg(self, target: str, tags: dict[str, str]) -> None:
        self.sent.append((target, tags))


def test_catalog_covers_every_command():
    cat = catalog(".")
    commands = cat["commands"]
    assert isinstance(commands, list)
    names = {c["name"] for c in commands if isinstance(c, dict)}
    assert {"play", "yt", "seek", "show", "moviesearch"} <= names
    assert cat["prefix"] == "."


def test_query_replies_to_asker_with_encoded_catalog():
    irc = _FakeIrc()
    bot = BotTools(irc, "$tv", ".", lambda *_: None)
    assert bot.handle_tagmsg("alice", "jukebox", {"+draft/bot-cmds-query": None})
    target, tags = irc.sent[-1]
    assert target == "alice"
    decoded = bottools._decode(tags["+draft/bot-cmds"])
    assert isinstance(decoded, dict)
    assert any(c["name"] == "play" for c in decoded["commands"])


def test_invocation_routes_through_the_command_pipeline():
    calls: list[tuple[str, str, str, str | None, str | None]] = []

    def invoke(
        sender: str, target: str, text: str, msgid: str | None, account: str | None
    ) -> None:
        calls.append((sender, target, text, msgid, account))

    bot = BotTools(_FakeIrc(), "$tv", ".", invoke)
    payload = bottools._encode({"name": "play", "options": {"text": "http://x/v"}})
    bot.handle_tagmsg(
        "alice",
        "jukebox",
        {"+draft/bot-cmd": payload, "msgid": "m1", "account": "alice"},
    )
    assert calls == [("alice", "$tv", ".play http://x/v", "m1", "alice")]


def test_unrelated_tagmsg_is_not_consumed():
    bot = BotTools(_FakeIrc(), "$tv", ".", lambda *_: None)
    assert not bot.handle_tagmsg("alice", "jukebox", {"+draft/react": "👍"})
