"""Prompts live in prompts/ under git. A run stores the version name and the sha256."""

import hashlib

from config import PROMPTS

PRODUCT = {"excel": "Microsoft Excel", "sheets": "Google Sheets"}


def load_template(version: str) -> tuple[str, str]:
    """Return (template text, sha256). Line endings and the trailing newline are
    normalized first, so the hash does not depend on how git checked the file out."""
    path = PROMPTS / f"{version}.txt"
    if not path.exists():
        raise SystemExit(f"prompt not found: {path}")
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n").rstrip("\n")
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()


def system_prompt(template: str, dialect: str) -> str:
    return template.replace("<<PRODUCT>>", PRODUCT[dialect])


def user_message(formula: str, context: str) -> str:
    # same two lines as the Node version's userMessage()
    return f"User formula: {formula}\nUser context: {context or 'none'}"
