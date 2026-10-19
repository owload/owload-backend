import smtplib

from app.core.config import Settings
from app.services import mailer
from app.services.mailer import invitation_mail


def test_invitation_mail_names_the_drive_and_never_holds_a_password():
    subject, body, html_body = invitation_mail("Alex", "Photos", "reader", "https://app.example.com", has_account=True)
    assert "Photos" in subject and "Alex" in subject
    assert "https://app.example.com" in body and "reader" in body
    assert "Ask the person who shared it for its password" in body
    assert "Photos" in html_body and "https://app.example.com" in html_body
    assert "Ask the person who shared it for its password" in html_body


def test_a_line_break_in_a_name_does_not_reach_the_header():
    subject, _, _ = invitation_mail("Al\r\nBcc: x@example.com", "My\ndrive", "writer", "https://app.example.com", has_account=False)
    assert "\n" not in subject and "\r" not in subject


def test_what_a_person_typed_is_escaped_in_the_html():
    _, body, html_body = invitation_mail('<img src=x onerror=alert(1)>', 'Fam"ily <b>drive</b>', "reader", 'https://app.example.com/?a="x"&b=1', has_account=True)
    assert "<img src=x" not in html_body and "<b>drive</b>" not in html_body
    assert "&lt;img src=x onerror=alert(1)&gt;" in html_body
    assert 'href="https://app.example.com/?a=&quot;x&quot;&amp;b=1"' in html_body
    assert "<img src=x onerror=alert(1)>" in body  # the plain text is plain text: nothing there is interpreted


def test_the_mail_says_to_whom_it_was_sent_and_escapes_the_address():
    _, _, html_body = invitation_mail("Alex", "Photos", "reader", "https://app.example.com", has_account=True, recipient='a"b<c>@example.com')
    assert "This message was sent to" in html_body
    assert 'a"b<c>@example.com' not in html_body and "a&quot;b&lt;c&gt;@example.com" in html_body
    _, _, without = invitation_mail("Alex", "Photos", "reader", "https://app.example.com", has_account=True)
    assert "This message was sent to" not in without


def test_the_html_keeps_to_the_left_and_does_not_centre():
    _, _, html_body = invitation_mail("Alex", "Photos", "reader", "https://app.example.com", has_account=True)
    assert 'align="center"' not in html_body and "margin: 0 auto" not in html_body and "margin:auto" not in html_body


def test_a_person_without_an_account_is_asked_to_sign_up():
    _, body, html_body = invitation_mail("Alex", "Photos", "reader", "https://app.example.com", has_account=False)
    assert "Sign up or sign in" in body and "Sign up or sign in" in html_body


def test_the_mail_is_text_and_html_with_the_logo_inside_it(monkeypatch):
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self, context=None):
            pass

        def login(self, user, password):
            pass

        def send_message(self, message):
            sent.append(message)

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    settings = Settings(_env_file=None, smtp_host="smtp.example.com", smtp_from="Owload <noreply@example.com>", smtp_user="u", smtp_password="p")
    subject, body, html_body = invitation_mail("Alex", "Photos", "reader", "https://app.example.com", has_account=True)
    mailer._send(settings, "person@example.com", subject, body, html_body)

    (message,) = sent
    assert message.get_content_type() == "multipart/alternative"
    assert message.get_body(preferencelist=("plain",)).get_content().strip().startswith("Alex gave you access")
    html_part = message.get_body(preferencelist=("html",))
    assert "cid:owload-logo" in html_part.get_content()
    inline = [part for part in message.walk() if part.get_content_type() == "image/png"]
    assert len(inline) == 1 and inline[0]["Content-ID"] == "<owload-logo>"
    assert inline[0].get_content()[:8] == b"\x89PNG\r\n\x1a\n"
    assert message.get_content_type() == "multipart/alternative" and [p.get_content_type() for p in message.iter_parts()] == ["text/plain", "multipart/related"]


def test_a_mail_without_html_stays_plain(monkeypatch):
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self, context=None):
            pass

        def send_message(self, message):
            sent.append(message)

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    settings = Settings(_env_file=None, smtp_host="smtp.example.com", smtp_from="noreply@example.com")
    mailer._send(settings, "person@example.com", "Hello", "Just text")
    assert sent[0].get_content_type() == "text/plain"
