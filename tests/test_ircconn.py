from irctokens import tokenise

from obby_jukebox.ircconn import IrcClient


class Probe(IrcClient):
    def __init__(self, **kw: object) -> None:
        super().__init__(
            host="h",
            port=1,
            tls=False,
            nick="bot",
            sasl_user="bot",
            sasl_pass="pw",
            caps=["message-tags"],
            **kw,  # type: ignore[arg-type]
        )
        self.sent: list[str] = []

    def send_raw(self, line: str) -> None:
        self.sent.append(line)

    def feed(self, raw: str) -> None:
        self._handle(tokenise(raw))


def test_cap_ls_requests_sasl_and_registration():
    c = Probe(register=True)
    c.feed("CAP * LS :message-tags sasl draft/account-registration")
    req = next(s for s in c.sent if s.startswith("CAP REQ"))
    assert "sasl" in req
    assert "draft/account-registration" in req


def test_cap_ls_with_values_requests_sasl_and_registration():
    c = Probe(register=True)
    c.feed(
        "CAP * LS :message-tags sasl=PLAIN,EXTERNAL "
        "draft/account-registration=before-connect,email-required"
    )
    req = next(s for s in c.sent if s.startswith("CAP REQ")).split()
    assert "sasl" in req
    assert "draft/account-registration" in req


def test_no_authenticate_when_server_has_no_sasl():
    c = Probe(register=True)
    c.feed("CAP * LS :message-tags")
    c.feed("CAP * ACK :message-tags")
    assert "AUTHENTICATE PLAIN" not in c.sent
    assert "CAP END" in c.sent


def test_no_registration_cap_when_disabled():
    c = Probe(register=False)
    c.feed("CAP * LS :sasl draft/account-registration")
    req = next(s for s in c.sent if s.startswith("CAP REQ"))
    assert "sasl" in req
    assert "draft/account-registration" not in req


def test_sasl_success_logs_in_and_ends_cap():
    c = Probe(register=True)
    c.feed("CAP * LS :sasl draft/account-registration")
    c.feed("CAP * ACK :sasl draft/account-registration")
    assert "AUTHENTICATE PLAIN" in c.sent
    c.feed("AUTHENTICATE +")
    c.feed(":srv 903 bot :SASL authentication successful")
    assert c.logged_in
    assert "CAP END" in c.sent
    assert not any(s.startswith("REGISTER") for s in c.sent)


def test_registers_when_sasl_fails_then_logs_in_on_success():
    c = Probe(register=True, register_email="bot@x.com")
    c.feed("CAP * LS :sasl draft/account-registration")
    c.feed("CAP * ACK :sasl draft/account-registration")
    c.feed("AUTHENTICATE +")
    c.feed(":srv 904 bot :SASL authentication failed")
    assert [s for s in c.sent if s.startswith("REGISTER")] == [
        "REGISTER bot bot@x.com pw"
    ]
    assert "CAP END" not in c.sent  # waits for the REGISTER reply first
    c.feed(":srv REGISTER SUCCESS bot :Account registered successfully.")
    assert c.logged_in
    assert "CAP END" in c.sent


def test_blank_register_email_sends_star():
    c = Probe(register=True)
    c.feed("CAP * LS :sasl draft/account-registration")
    c.feed("CAP * ACK :sasl draft/account-registration")
    c.feed("AUTHENTICATE +")
    c.feed(":srv 904 bot :nope")
    assert "REGISTER bot * pw" in c.sent


def test_verification_required_continues_unauthenticated():
    c = Probe(register=True, register_email="bot@x.com")
    c.feed("CAP * LS :sasl draft/account-registration")
    c.feed("CAP * ACK :sasl draft/account-registration")
    c.feed("AUTHENTICATE +")
    c.feed(":srv 904 bot :nope")
    c.feed(":srv REGISTER VERIFICATION_REQUIRED bot :check your email")
    assert not c.logged_in
    assert "CAP END" in c.sent


def test_register_failure_continues_unauthenticated():
    c = Probe(register=True, register_email="bot@x.com")
    c.feed("CAP * LS :sasl draft/account-registration")
    c.feed("CAP * ACK :sasl draft/account-registration")
    c.feed("AUTHENTICATE +")
    c.feed(":srv 904 bot :nope")
    c.feed(":srv FAIL REGISTER ACCOUNT_EXISTS bot :already taken")
    assert not c.logged_in
    assert "CAP END" in c.sent


def test_sasl_failure_without_registration_ends_cap():
    c = Probe(register=False)
    c.feed("CAP * LS :sasl")
    c.feed("CAP * ACK :sasl")
    c.feed("AUTHENTICATE +")
    c.feed(":srv 904 bot :nope")
    assert not any(s.startswith("REGISTER") for s in c.sent)
    assert "CAP END" in c.sent


def test_identify_fallback_when_not_logged_in():
    c = Probe(register=False)
    c.feed(":srv 001 bot :Welcome")
    assert "IDENTIFY bot pw" in c.sent


def test_no_identify_when_already_logged_in():
    c = Probe(register=True)
    c.logged_in = True
    c.feed(":srv 001 bot :Welcome")
    assert not any(s.startswith("IDENTIFY") for s in c.sent)


def test_rpl_loggedin_captures_account():
    c = Probe(register=True)
    c.feed(":srv 900 bot nick!u@h account-name :You are now logged in")
    assert c.account == "account-name"
