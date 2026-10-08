from app.services.mailer import invitation_mail


def test_invitation_mail_names_the_drive_and_never_holds_a_password():
    subject, body = invitation_mail("Alex", "Photos", "reader", "https://app.example.com", has_account=True)
    assert "Photos" in subject and "Alex" in subject
    assert "https://app.example.com" in body and "reader" in body
    assert "Ask the person who shared it for its password" in body


def test_a_line_break_in_a_name_does_not_reach_the_header():
    subject, _ = invitation_mail("Al\r\nBcc: x@example.com", "My\ndrive", "writer", "https://app.example.com", has_account=False)
    assert "\n" not in subject and "\r" not in subject
