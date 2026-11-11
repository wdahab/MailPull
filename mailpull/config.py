"""Configuration: credentials, settings, and named pull definitions.

Everything lives in one TOML file (default: ``mailpull.toml`` at the repo
root, override with ``MAILPULL_CONFIG``). See ``mailpull.example.toml``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tomllib
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.environ.get("MAILPULL_CONFIG", ROOT / "mailpull.toml"))

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class ConfigError(Exception):
    pass


# ============================================================================
# Pull definition
# ============================================================================

# TOML key -> Pull attribute. Keys not listed here are rejected (catches typos).
PULL_KEYS = {
    "mailbox": "mailbox",
    "from": "senders",
    "to": "recipients",
    "subject": "subject",
    "exclude_subject": "exclude_subject",
    "body": "body",
    "search": "search",
    "folders": "folders",
    "since": "since",
    "until": "until",
    "has_attachments": "has_attachments",
    "attachments": "save_attachments",
    "save_eml": "save_eml",
    "max_attachment_mb": "max_attachment_mb",
    "limit": "limit",
    "description": "description",
}

LIST_FIELDS = {"senders", "recipients", "subject", "exclude_subject", "body", "folders"}


@dataclass
class Pull:
    """A named set of criteria selecting emails from one mailbox.

    Matching rules: every criterion that is set must match (AND). Within a
    list criterion, any value may match (OR). Text matches are
    case-insensitive substrings.
    """

    name: str
    mailbox: str | None = None          # None = the signed-in user's own mailbox
    senders: list[str] = field(default_factory=list)      # "a@b.com" or "@b.com"
    recipients: list[str] = field(default_factory=list)   # matches To/Cc/Bcc address or name
    subject: list[str] = field(default_factory=list)
    exclude_subject: list[str] = field(default_factory=list)
    body: list[str] = field(default_factory=list)
    search: str | None = None           # Graph $search (KQL), server-side full text
    folders: list[str] = field(default_factory=list)      # empty = all folders
    since: str | None = None            # "2024-01-01", "30d", "6m", "1y"
    until: str | None = None
    has_attachments: bool | None = None
    save_attachments: bool = True
    save_eml: bool = True
    max_attachment_mb: float = 25.0
    limit: int | None = None            # max messages per run (newest first)
    description: str = ""

    def criteria_hash(self) -> str:
        """Hash of the criteria that decide *which* messages match.

        Used to detect edited pulls so the next run re-scans full history.
        """
        keys = ["mailbox", "senders", "recipients", "subject", "exclude_subject",
                "body", "search", "folders", "since", "until", "has_attachments"]
        d = {k: getattr(self, k) for k in keys}
        return hashlib.sha1(json.dumps(d, sort_keys=True, default=str).encode()).hexdigest()[:12]

    def to_toml(self) -> str:
        """Render as a [pulls.<name>] TOML block (for --save)."""
        inv = {v: k for k, v in PULL_KEYS.items()}
        defaults = Pull(name=self.name)
        lines = [f"[pulls.{self.name}]"]
        for attr, value in asdict(self).items():
            if attr == "name" or value == getattr(defaults, attr):
                continue
            lines.append(f"{inv[attr]} = {_toml_value(value)}")
        return "\n".join(lines) + "\n"


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    return json.dumps(str(v))  # JSON string escaping is valid TOML basic-string escaping


def make_pull(name: str, data: dict) -> Pull:
    """Build a Pull from a TOML table, validating keys and types."""
    if not NAME_RE.match(name):
        raise ConfigError(f"Pull name {name!r} must be lowercase letters, digits, '-' or '_'")
    unknown = set(data) - set(PULL_KEYS)
    if unknown:
        raise ConfigError(
            f"[pulls.{name}] unknown key(s): {', '.join(sorted(unknown))}. "
            f"Valid keys: {', '.join(PULL_KEYS)}"
        )
    kwargs = {}
    for key, value in data.items():
        attr = PULL_KEYS[key]
        if attr in LIST_FIELDS:
            value = [value] if isinstance(value, str) else list(value)
            value = [str(x).strip() for x in value if str(x).strip()]
        elif attr in ("since", "until") and isinstance(value, (date, datetime)):
            value = value.isoformat()[:10]
        kwargs[attr] = value
    pull = Pull(name=name, **kwargs)
    # Validate dates early so typos fail at load time, not mid-run.
    parse_when(pull.since)
    parse_when(pull.until)
    return pull


# ============================================================================
# Dates
# ============================================================================

_REL_RE = re.compile(r"^(\d+)\s*([dwmy])$", re.I)


def parse_when(value: str | None) -> datetime | None:
    """Parse "YYYY-MM-DD" or a relative age like "30d", "8w", "6m", "1y".

    Returns a timezone-aware local datetime at midnight, or None.
    """
    if value in (None, ""):
        return None
    s = str(value).strip()
    m = _REL_RE.match(s)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        days = {"d": 1, "w": 7, "m": 30, "y": 365}[unit] * n
        d = date.today() - timedelta(days=days)
    else:
        try:
            d = date.fromisoformat(s[:10])
        except ValueError:
            raise ConfigError(
                f"Bad date {value!r}: use YYYY-MM-DD or a relative age like 30d, 8w, 6m, 1y"
            ) from None
    return datetime(d.year, d.month, d.day).astimezone()


# ============================================================================
# Config file
# ============================================================================

@dataclass
class Config:
    client_id: str | None
    client_secret: str | None
    data_dir: Path
    default_mailbox: str | None
    pulls: dict[str, Pull]
    path: Path


def load_config(path: Path = CONFIG_PATH) -> Config:
    raw = {}
    if path.exists():
        try:
            raw = tomllib.loads(path.read_text())
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"{path}: {e}") from None

    azure = raw.get("azure", {})
    settings = raw.get("settings", {})

    data_dir = Path(os.environ.get("MAILPULL_DATA") or settings.get("data_dir") or "data")
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir

    pulls = {name: make_pull(name, tbl) for name, tbl in raw.get("pulls", {}).items()}
    default_mailbox = settings.get("default_mailbox") or None
    for p in pulls.values():
        if p.mailbox is None:
            p.mailbox = default_mailbox

    client_id = os.environ.get("MAILPULL_CLIENT_ID") or azure.get("client_id")
    client_secret = os.environ.get("MAILPULL_CLIENT_SECRET") or azure.get("client_secret")
    if not (client_id and client_secret):
        client_id, client_secret = _legacy_credentials(client_id, client_secret)

    return Config(client_id, client_secret, data_dir, default_mailbox, pulls, path)


def _legacy_credentials(cid, secret):
    """Fall back to ../rfpdownloader/config.py (the app findaily used)."""
    legacy = ROOT.parent / "rfpdownloader" / "config.py"
    if not legacy.exists():
        return cid, secret
    ns: dict = {}
    try:
        exec(compile(legacy.read_text(), str(legacy), "exec"), ns)
    except Exception as e:  # noqa: BLE001
        print(f"warning: could not read {legacy}: {e}", file=sys.stderr)
        return cid, secret
    return cid or ns.get("CLIENT_ID"), secret or ns.get("CLIENT_SECRET")


def append_pull(pull: Pull, path: Path = CONFIG_PATH) -> None:
    """Append a pull definition to the config file (creating it if needed)."""
    existing = path.read_text() if path.exists() else ""
    if re.search(rf"^\[pulls\.{re.escape(pull.name)}\]", existing, re.M):
        raise ConfigError(f"A pull named {pull.name!r} already exists in {path}")
    sep = "" if not existing or existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
    path.write_text(existing + sep + pull.to_toml())
