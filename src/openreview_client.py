"""
Optional OpenReview API v2/v1 Client & Credential Manager (``src/openreview_client.py``).

Key Design Principles:
- **Strictly Optional**: Never contains built-in/default credentials. Works only when the user
  explicitly logs in via ``ra-cli login-openreview`` (or sets ``OPENREVIEW_TOKEN`` in their own ``.env``).
- **Owner-Only Local Storage**: Stores the authenticated JWT bearer token (and optional refresh
  credentials) in ``~/.research-agent/openreview_auth.json`` with ``0o600`` permissions.
- **API v2 + v1 Compatibility**: Unwraps both OpenReview API v2 (``{"value": ...}``) and API v1
  note content schemas seamlessly, supporting paper search, forum metadata, peer reviews, and PDF downloads.
"""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

import requests

from config import settings
from config.settings import get_logger

logger = get_logger(__name__)

DEFAULT_OPENREVIEW_API_URL = "https://api2.openreview.net"
FALLBACK_OPENREVIEW_V1_URL = "https://api.openreview.net"


class OpenReviewAuthError(Exception):
    """Raised when OpenReview authentication fails or is not configured."""


def get_auth_file_path() -> Path:
    """Return ``~/.research-agent/openreview_auth.json``."""
    return settings.DATA_DIR / "openreview_auth.json"


def load_openreview_auth() -> dict[str, Any]:
    """
    Load OpenReview authentication state from ``~/.research-agent/openreview_auth.json``
    or user-provided ``OPENREVIEW_TOKEN`` environment variable.
    Never uses any hardcoded or built-in credentials.
    """
    if hasattr(settings, "reload_env_settings"):
        settings.reload_env_settings()

    auth_file = get_auth_file_path()
    if auth_file.exists():
        try:
            data = json.loads(auth_file.read_text(encoding="utf-8"))
            if isinstance(data, dict) and (data.get("token") or data.get("username")):
                return data
        except Exception as e:
            logger.warning("Could not read OpenReview auth file %s: %s", auth_file, e)

    env_token = (os.getenv("OPENREVIEW_TOKEN") or "").strip()
    if env_token:
        return {
            "token": env_token,
            "user_id": os.getenv("OPENREVIEW_USER", "env-token-user"),
            "api_url": os.getenv("OPENREVIEW_API_URL", DEFAULT_OPENREVIEW_API_URL).rstrip("/"),
            "source": "env",
        }
    return {}


def save_openreview_auth(auth_data: dict[str, Any]) -> Path:
    """Persist OpenReview auth JSON to ``~/.research-agent/openreview_auth.json`` with ``0600`` permissions."""
    auth_file = get_auth_file_path()
    auth_file.parent.mkdir(parents=True, exist_ok=True)
    auth_file.write_text(json.dumps(auth_data, indent=2), encoding="utf-8")
    try:
        os.chmod(auth_file, 0o600)
    except OSError:
        pass
    return auth_file


def clear_openreview_auth() -> bool:
    """Remove ``~/.research-agent/openreview_auth.json`` if present."""
    auth_file = get_auth_file_path()
    if auth_file.exists():
        auth_file.unlink(missing_ok=True)
        return True
    return False


def is_openreview_configured() -> bool:
    """Return True if the user has logged into OpenReview via ``ra-cli login-openreview`` (or ``OPENREVIEW_TOKEN``)."""
    auth = load_openreview_auth()
    return bool(auth.get("token") or (auth.get("username") and auth.get("password")))


def get_openreview_status() -> dict[str, Any]:
    """Return a non-sensitive summary of the user's OpenReview authentication status."""
    auth = load_openreview_auth()
    if not auth:
        return {
            "configured": False,
            "user_id": None,
            "username": None,
            "api_url": DEFAULT_OPENREVIEW_API_URL,
            "auth_file": str(get_auth_file_path()),
        }
    return {
        "configured": True,
        "user_id": auth.get("user_id") or auth.get("username") or "authenticated",
        "username": auth.get("username"),
        "api_url": auth.get("api_url", DEFAULT_OPENREVIEW_API_URL),
        "logged_in_at": auth.get("logged_in_at"),
        "auth_file": str(get_auth_file_path()),
    }


def login_openreview(
    username: Optional[str] = None,
    password: Optional[str] = None,
    token: Optional[str] = None,
    api_url: str = DEFAULT_OPENREVIEW_API_URL,
    save_password_for_refresh: bool = True,
) -> dict[str, Any]:
    """
    Authenticate with OpenReview API (`POST /login`) using the user's own credentials
    (or validate a user-supplied bearer token via `GET /profiles`) and store the session
    in `~/.research-agent/openreview_auth.json`.
    """
    base_url = (api_url or DEFAULT_OPENREVIEW_API_URL).rstrip("/")

    if token and token.strip():
        clean_tok = token.strip()
        auth_record = {
            "token": clean_tok,
            "user_id": username or "token-user",
            "username": username,
            "api_url": base_url,
            "logged_in_at": datetime.now(timezone.utc).isoformat(),
        }
        save_openreview_auth(auth_record)
        return auth_record

    if not username or not password:
        raise OpenReviewAuthError("Both OpenReview username/email and password are required.")

    resp = requests.post(
        f"{base_url}/login",
        json={"id": username.strip(), "password": password},
        headers={"Content-Type": "application/json", "User-Agent": "ResearchAgent/1.0"},
        timeout=15,
    )
    if resp.status_code in (401, 403):
        detail = ""
        try:
            detail = (resp.json() or {}).get("message") or ""
        except Exception:
            detail = resp.text[:200]
        raise OpenReviewAuthError(
            f"OpenReview authentication failed (HTTP {resp.status_code})"
            + (f": {detail}" if detail else ". Please check your email/username and password.")
        )
    resp.raise_for_status()

    payload = resp.json() or {}
    jwt_token = payload.get("token")
    if not jwt_token:
        raise OpenReviewAuthError("OpenReview login response did not include an authentication token.")

    user_obj = payload.get("user") or {}
    user_id = user_obj.get("id") or (user_obj.get("user") or {}).get("id") or username.strip()

    auth_record: dict[str, Any] = {
        "token": jwt_token,
        "user_id": user_id,
        "username": username.strip(),
        "api_url": base_url,
        "logged_in_at": datetime.now(timezone.utc).isoformat(),
    }
    if save_password_for_refresh:
        auth_record["password"] = password

    save_openreview_auth(auth_record)
    logger.info("Authenticated with OpenReview as %s", user_id)
    return auth_record


def get_openreview_headers(force_refresh: bool = False) -> dict[str, str]:
    """Return HTTP headers for OpenReview requests, refreshing the token if requested and possible."""
    headers = {"User-Agent": "ResearchAgent/1.0"}
    auth = load_openreview_auth()
    if not auth:
        return headers

    if force_refresh and auth.get("username") and auth.get("password"):
        try:
            auth = login_openreview(
                username=auth["username"],
                password=auth["password"],
                api_url=auth.get("api_url", DEFAULT_OPENREVIEW_API_URL),
                save_password_for_refresh=True,
            )
        except Exception as e:
            logger.warning("OpenReview token auto-refresh failed: %s", e)

    token = auth.get("token")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _or_get(path: str, params: dict[str, Any], timeout: int = 12, allow_v1_fallback: bool = False) -> Optional[dict]:
    """Perform an authenticated GET request against OpenReview API v2 (with auto-refresh on 401)."""
    auth = load_openreview_auth()
    base_url = (auth.get("api_url") or DEFAULT_OPENREVIEW_API_URL).rstrip("/")
    headers = get_openreview_headers(force_refresh=False)

    url = f"{base_url}/{path.lstrip('/')}"
    resp = requests.get(url, params=params, headers=headers, timeout=timeout)

    if resp.status_code == 401 and auth.get("username") and auth.get("password"):
        headers = get_openreview_headers(force_refresh=True)
        resp = requests.get(url, params=params, headers=headers, timeout=timeout)

    if resp.status_code == 200:
        data = resp.json() or {}
        if data.get("notes") or not allow_v1_fallback:
            return data

    if allow_v1_fallback and base_url != FALLBACK_OPENREVIEW_V1_URL:
        v1_url = f"{FALLBACK_OPENREVIEW_V1_URL}/{path.lstrip('/')}"
        v1_resp = requests.get(v1_url, params=params, headers=headers, timeout=timeout)
        if v1_resp.status_code == 200:
            return v1_resp.json() or {}

    if resp.status_code in (401, 403):
        raise OpenReviewAuthError(
            "OpenReview session expired or unauthorized (HTTP "
            f"{resp.status_code}). Run `ra-cli login-openreview` to re-authenticate."
        )
    resp.raise_for_status()
    return resp.json() if resp.status_code == 200 else None


def extract_openreview_forum_id(value: str) -> Optional[str]:
    """
    Extract an OpenReview forum/note ID from:
    - ``https://openreview.net/forum?id=abc123XYZ``
    - ``https://openreview.net/pdf?id=abc123XYZ``
    - ``openreview:abc123XYZ``
    Returns None if ``value`` does not look like an OpenReview URL or prefixed ID.
    """
    if not value:
        return None
    val = value.strip()
    if val.lower().startswith("openreview:"):
        fid = val.split(":", 1)[1].strip()
        return fid or None
    if "openreview.net" in val.lower():
        try:
            parsed = urlparse(val)
            qs = parse_qs(parsed.query)
            if "id" in qs and qs["id"]:
                return qs["id"][0].strip()
            if "noteId" in qs and qs["noteId"]:
                return qs["noteId"][0].strip()
        except Exception:
            pass
    return None


def _unwrap_val(raw: Any) -> Any:
    """Unwrap OpenReview API v2 ``{"value": ...}`` objects or return API v1 values directly."""
    if isinstance(raw, dict) and "value" in raw:
        return raw["value"]
    return raw


def parse_openreview_note(note: dict[str, Any]) -> dict[str, Any]:
    """Normalize an OpenReview API v2 or v1 submission note into a clean dictionary."""
    content = note.get("content") or {}
    forum_id = note.get("forum") or note.get("id") or ""
    note_id = note.get("id") or forum_id

    title = str(_unwrap_val(content.get("title")) or "").strip()
    abstract = str(_unwrap_val(content.get("abstract")) or "").strip()
    tldr = str(
        _unwrap_val(content.get("TL;DR"))
        or _unwrap_val(content.get("TLDR"))
        or _unwrap_val(content.get("one-sentence_summary"))
        or ""
    ).strip()

    raw_authors = _unwrap_val(content.get("authors")) or []
    if isinstance(raw_authors, list):
        authors = [str(a).strip() for a in raw_authors if str(a).strip()]
    elif isinstance(raw_authors, str):
        authors = [a.strip() for a in raw_authors.split(",") if a.strip()]
    else:
        authors = []

    venue = str(_unwrap_val(content.get("venue")) or _unwrap_val(content.get("venueid")) or "").strip()
    keywords = _unwrap_val(content.get("keywords")) or []
    if not isinstance(keywords, list):
        keywords = [str(keywords)]

    # Year extraction from venue string or cdate/tcdate timestamp (ms since epoch)
    year: Optional[int] = None
    ym = re.search(r"\b(20\d{2})\b", venue)
    if ym:
        year = int(ym.group(1))
    else:
        ts = note.get("odate") or note.get("tcdate") or note.get("cdate")
        if isinstance(ts, (int, float)) and ts > 1_000_000_000:
            try:
                year = datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc).year
            except Exception:
                pass

    forum_url = f"https://openreview.net/forum?id={forum_id}" if forum_id else ""
    pdf_field = str(_unwrap_val(content.get("pdf")) or "").strip()
    if pdf_field.startswith("http"):
        pdf_url = pdf_field
    elif forum_id:
        pdf_url = f"https://openreview.net/pdf?id={forum_id}"
    else:
        pdf_url = ""

    return {
        "id": note_id,
        "forum_id": forum_id,
        "title": title,
        "authors": authors,
        "abstract": abstract,
        "tldr": tldr,
        "venue": venue or "OpenReview",
        "year": year,
        "keywords": keywords,
        "forum_url": forum_url,
        "pdf_url": pdf_url,
    }


def search_openreview_notes(
    query: str,
    venue: Optional[str] = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """
    Search OpenReview submissions via ``GET /notes/search``.
    Requires the user to have authenticated via ``ra-cli login-openreview``.
    """
    if not is_openreview_configured():
        raise OpenReviewAuthError(
            "OpenReview integration is not configured. Run `ra-cli login-openreview` in your terminal "
            "to log in with your OpenReview credentials."
        )

    clean_q = re.sub(r"\s+", " ", (query or "").strip())
    if not clean_q:
        return []

    fetch_limit = max(limit * 3, 15) if venue else max(limit, 5)
    params: dict[str, Any] = {
        "query": clean_q,
        "limit": min(fetch_limit, 50),
        "content": "all",
        "source": "forum",
    }

    data = _or_get("/notes/search", params=params)
    notes = (data or {}).get("notes") or []

    results: list[dict[str, Any]] = []
    venue_filter = venue.strip().lower() if venue and venue.strip() else None

    for n in notes:
        parsed = parse_openreview_note(n)
        if not parsed.get("title"):
            continue
        if venue_filter:
            v_str = (parsed.get("venue") or "").lower()
            inv_list = n.get("invitations") or ([n.get("invitation")] if n.get("invitation") else [])
            inv_str = " ".join(str(x) for x in inv_list).lower()
            if venue_filter not in v_str and venue_filter not in inv_str:
                continue
        results.append(parsed)
        if len(results) >= limit:
            break

    return results


def _extract_reply_summary(note: dict[str, Any]) -> Optional[dict[str, str]]:
    """Classify and summarize a non-submission forum note (Official Review, Meta-Review, Decision, Comment)."""
    content = note.get("content") or {}
    invitations = note.get("invitations") or ([note.get("invitation")] if note.get("invitation") else [])
    inv_str = " ".join(str(i) for i in invitations)
    inv_lower = inv_str.lower()

    # Skip the root submission note itself (where id == forum and title + abstract exist)
    if note.get("id") == note.get("forum") and _unwrap_val(content.get("abstract")):
        return None

    kind = "Comment"
    if "decision" in inv_lower or content.get("decision"):
        kind = "Program Chair / AC Decision"
    elif "meta_review" in inv_lower or "metareview" in inv_lower or content.get("metareview"):
        kind = "Meta-Review"
    elif "official_review" in inv_lower or "review" in inv_lower or content.get("rating") or content.get("review"):
        kind = "Official Review"
    elif "rebuttal" in inv_lower or "author" in inv_lower:
        kind = "Author Response"

    fields_of_interest = [
        ("Decision", ("decision", "recommendation")),
        ("Rating / Score", ("rating", "overall_assessment", "score", "soundness", "presentation", "contribution")),
        ("Confidence", ("confidence",)),
        ("Summary", ("summary", "summary_of_the_paper", "metareview")),
        ("Strengths", ("strengths", "strengths_and_weaknesses")),
        ("Weaknesses", ("weaknesses", "limitations")),
        ("Questions", ("questions", "questions_for_authors")),
        ("Review / Comment", ("review", "comment", "rebuttal")),
    ]

    extracted_parts: list[str] = []
    seen_values: set[str] = set()
    for label, keys in fields_of_interest:
        vals: list[str] = []
        for k in keys:
            v = _unwrap_val(content.get(k))
            if v is not None:
                s = str(v).strip()
                if s and s not in seen_values:
                    seen_values.add(s)
                    vals.append(f"{k}={s}" if len(keys) > 2 and len(s) < 40 else s)
        if vals:
            joined = " | ".join(vals) if len(vals) > 1 and all(len(x) < 60 for x in vals) else "\n".join(vals)
            extracted_parts.append(f"  - **{label}**: {joined[:1200]}")

    if not extracted_parts:
        return None

    signatures = note.get("signatures") or []
    sig = str(signatures[0]).split("/")[-1] if signatures else "Anonymous"
    return {
        "kind": kind,
        "author": sig,
        "body": "\n".join(extracted_parts),
    }


def get_openreview_forum_details(
    forum_id_or_url: str,
    include_reviews: bool = True,
) -> Optional[dict[str, Any]]:
    """
    Fetch full metadata, abstract, PDF link, and (optionally) official peer reviews / decision
    for an OpenReview paper by its Forum ID or URL.
    """
    fid = extract_openreview_forum_id(forum_id_or_url) or forum_id_or_url.strip()
    if not fid:
        return None

    if include_reviews:
        data = _or_get("/notes", params={"forum": fid, "limit": 100}, allow_v1_fallback=True)
    else:
        data = _or_get("/notes", params={"id": fid}, allow_v1_fallback=True)

    notes = (data or {}).get("notes") or []
    if not notes:
        return None

    root_note = next((n for n in notes if n.get("id") == fid), notes[0])
    paper = parse_openreview_note(root_note)

    reviews: list[dict[str, str]] = []
    if include_reviews and len(notes) > 1:
        # Sort chronologically (oldest first) or keep root replies clear
        other_notes = [n for n in notes if n.get("id") != root_note.get("id")]
        other_notes.sort(key=lambda x: x.get("cdate") or x.get("tcdate") or 0)
        for n in other_notes:
            summary = _extract_reply_summary(n)
            if summary:
                reviews.append(summary)

    paper["reviews"] = reviews
    return paper
