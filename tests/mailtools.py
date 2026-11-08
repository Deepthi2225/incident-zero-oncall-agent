"""Reading the emails that tests capture."""


def plain_text(msg):
    """The plain-text body of a captured email, single-part or multipart."""
    part = next(p for p in msg.walk() if p.get_content_type() == "text/plain" and not p.get_filename())
    return part.get_payload(decode=True).decode()
