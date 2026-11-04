import re, unicodedata


def slugify(text, max_len=50):
    """Turn text into a URL-friendly slug (see README for the rules)."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text)
    return text[:max_len]
