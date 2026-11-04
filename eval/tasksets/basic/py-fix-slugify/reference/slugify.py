import re, unicodedata


def slugify(text, max_len=50):
    """Turn text into a URL-friendly slug (see README for the rules)."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:max_len].rstrip("-")
