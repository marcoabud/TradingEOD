"""Fail-closed, persistent claims for one execution per account and NY session.

Claim immediately before any order submission, including sessions whose final plan
contains no orders.  A claim is deliberately consumed even if execution crashes:
do not delete it or retry that session automatically.  Manual reconciliation is
required after an interrupted execution.

GitHub Actions stores immutable markers on the dedicated ``eod-state`` branch.
The contents API's create-without-SHA operation is the atomic claim; the workflow
token needs ``contents: write``.  Local runs require an absolute ``EOD_STATE_DIR``
and use exclusive file creation.  Local state coordinates only processes sharing
that directory and does not coordinate with GitHub or another machine. Dry runs
must not claim trading sessions. Missed-window notification claims use their own
namespace and never consume or overwrite a trading-session marker.
"""

from __future__ import annotations

import base64
from datetime import date, datetime
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Mapping

import requests


STATE_BRANCH = "eod-state"
REQUEST_TIMEOUT = (5, 20)


class StateError(RuntimeError):
    """A persistent execution or notification claim could not be established safely."""


def _validated_inputs(account_id: object, session_date: date | str, paper: bool) -> tuple[str, str, str]:
    if account_id is None or not str(account_id).strip():
        raise StateError("Missing account identifier for session state")
    if not isinstance(paper, bool):
        raise StateError("Session state mode must be a boolean")
    if isinstance(session_date, datetime):
        raise StateError("Session state requires a calendar date, not a timestamp")
    day = session_date.isoformat() if isinstance(session_date, date) else str(session_date)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise StateError("Invalid session calendar date")
    try:
        date.fromisoformat(day)
    except ValueError:
        raise StateError("Invalid session calendar date") from None
    account_hash = hashlib.sha256(str(account_id).strip().encode("utf-8")).hexdigest()[:16]
    return account_hash, day, "paper" if paper else "live"


def _error(operation: str, status: int | None = None) -> StateError:
    suffix = f" (HTTP {status})" if status is not None else ""
    return StateError(f"Persistent state {operation} failed{suffix}")


def _claim_local(
    state_dir: str, marker_path: str, payload: bytes, *,
    suppress_if_exists: str | None = None,
) -> bool:
    if not state_dir or not Path(state_dir).is_absolute():
        raise StateError("Local execution requires an absolute EOD_STATE_DIR")
    if suppress_if_exists is not None:
        try:
            (Path(state_dir) / suppress_if_exists).stat()
        except FileNotFoundError:
            pass
        except OSError:
            # An unreadable execution marker never means the session was missed.
            raise _error("local suppression lookup") from None
        else:
            return False
    marker = Path(state_dir) / marker_path
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        # Never replace an existing marker, including one left by a crashed run.
        descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    except OSError:
        raise _error("local claim") from None
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        # The consumed marker intentionally remains after a failed write.
        raise _error("local write") from None
    return True


def _claim_github(
    environ: Mapping[str, str], marker_path: str, payload: bytes, day: str, mode: str,
    session: requests.Session, *, suppress_if_exists: str | None = None,
    claim_label: str = "session",
) -> bool:
    repository = environ.get("GITHUB_REPOSITORY", "")
    revision = environ.get("GITHUB_SHA", "")
    token = environ.get("GITHUB_TOKEN", "")
    if not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*/[A-Za-z0-9_-][A-Za-z0-9_.-]*", repository):
        raise StateError("Invalid or missing GITHUB_REPOSITORY for session state")
    if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", revision):
        raise StateError("Invalid or missing GITHUB_SHA for session state")
    if not token.strip() or any(char.isspace() for char in token):
        raise StateError("Invalid or missing GITHUB_TOKEN for session state")
    base = f"https://api.github.com/repos/{repository}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    def request(method: str, path: str, **kwargs: object) -> requests.Response:
        try:
            return session.request(
                method, f"{base}/{path}", headers=headers,
                timeout=REQUEST_TIMEOUT, allow_redirects=False, **kwargs,
            )
        except requests.RequestException as exc:
            # No response body, URL, token, or exception text is exposed.
            raise StateError(f"Persistent state request failed ({type(exc).__name__})") from None

    def branch_exists() -> bool:
        response = request("GET", f"git/ref/heads/{STATE_BRANCH}")
        if response.status_code == 200:
            return True
        if response.status_code == 404:
            return False
        raise _error("branch lookup", response.status_code)

    def marker_exists(path: str = marker_path) -> bool:
        response = request("GET", f"contents/{path}", params={"ref": STATE_BRANCH})
        if response.status_code == 200:
            return True
        if response.status_code == 404:
            return False
        raise _error("marker lookup", response.status_code)

    if not branch_exists():
        try:
            created = request("POST", "git/refs", json={"ref": f"refs/heads/{STATE_BRANCH}", "sha": revision})
        except StateError:
            # A response can be lost after branch creation; never replay POST.
            if not branch_exists():
                raise
        else:
            if created.status_code != 201:
                # Includes the 422 race when another process created the branch.
                if not branch_exists():
                    raise _error("branch creation", created.status_code)

    if suppress_if_exists is not None and marker_exists(suppress_if_exists):
        return False
    if marker_exists():
        return False
    body = {
        "message": f"Claim EOD {claim_label} {day} ({mode})",
        "content": base64.b64encode(payload).decode("ascii"),
        "branch": STATE_BRANCH,
    }
    try:
        claimed = request("PUT", f"contents/{marker_path}", json=body)
    except StateError:
        # Even if this process may own the marker, uncertainty grants no action.
        if marker_exists():
            return False
        raise
    if claimed.status_code == 201:
        return True
    # A competing create returns 409/422.  A server error can also follow a
    # successful mutation; only a verified marker permits a clean duplicate skip.
    if marker_exists():
        return False
    raise _error("marker creation", claimed.status_code)


def _claim_marker(
    environ: Mapping[str, str], session: requests.Session | None,
    marker_path: str, payload: bytes, day: str, mode: str, *,
    suppress_if_exists: str | None = None, claim_label: str = "session",
) -> bool:
    github_actions = environ.get("GITHUB_ACTIONS", "").lower()
    if github_actions not in ("", "false", "true"):
        raise StateError("Invalid GITHUB_ACTIONS environment flag")
    if github_actions != "true":
        return _claim_local(
            environ.get("EOD_STATE_DIR", ""), marker_path, payload,
            suppress_if_exists=suppress_if_exists,
        )
    if session is not None:
        return _claim_github(
            environ, marker_path, payload, day, mode, session,
            suppress_if_exists=suppress_if_exists, claim_label=claim_label,
        )
    with requests.Session() as managed_session:
        return _claim_github(
            environ, marker_path, payload, day, mode, managed_session,
            suppress_if_exists=suppress_if_exists, claim_label=claim_label,
        )


def claim_session(
    account_id: object,
    session_date: date | str,
    paper: bool,
    *,
    environ: Mapping[str, str] = os.environ,
    session: requests.Session | None = None,
) -> bool:
    """Atomically consume the session; return False if already consumed.

    Any uncertainty raises StateError or returns False after finding a marker;
    neither outcome authorizes trading.  A successful True is intentionally not
    rolled back following execution, notification, or order failures.
    """
    account_hash, day, mode = _validated_inputs(account_id, session_date, paper)
    marker_path = f"sessions/{mode}/{account_hash}/{day}.json"
    payload = (json.dumps({"session_date": day, "mode": mode}, sort_keys=True) + "\n").encode("utf-8")
    return _claim_marker(environ, session, marker_path, payload, day, mode)


def claim_missed_window(
    account_id: object,
    session_date: date | str,
    paper: bool,
    *,
    environ: Mapping[str, str] = os.environ,
    session: requests.Session | None = None,
) -> bool:
    """Reserve one missed-window alert without claiming the trading session.

    An existing execution marker suppresses the alert, even if that execution
    failed after claiming its session. Only a new notification marker returns
    True. The marker is written BEFORE sending and is retained if delivery fails:
    this is best-effort notification, with no guaranteed retry on a later run.
    Callers must still enforce calendar/window checks; this grants no orders.
    """
    account_hash, day, mode = _validated_inputs(account_id, session_date, paper)
    execution_path = f"sessions/{mode}/{account_hash}/{day}.json"
    notice_path = f"notifications/missed-window/{mode}/{account_hash}/{day}.json"
    payload = (json.dumps(
        {"session_date": day, "mode": mode, "event": "missed_window"}, sort_keys=True,
    ) + "\n").encode("utf-8")
    return _claim_marker(
        environ, session, notice_path, payload, day, mode,
        suppress_if_exists=execution_path, claim_label="missed-window notification",
    )
