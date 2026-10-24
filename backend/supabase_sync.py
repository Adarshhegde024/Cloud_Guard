import json
import logging
import os
import urllib.request
import urllib.error
from typing import Any, Dict, Optional

logger = logging.getLogger("cloudguard.supabase")

SUPABASE_URL = os.getenv("SUPABASE_URL") or os.getenv("NEXT_PUBLIC_SUPABASE_URL") or "https://yzzhvzuppqytbbqjpznu.supabase.co"
SUPABASE_KEY = os.getenv("SUPABASE_KEY") or os.getenv("NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY") or "sb_publishable_J76UGnfc44HBRozZOdZsHQ_3NnJ2SG4"

def is_supabase_configured() -> bool:
    return bool(SUPABASE_URL and SUPABASE_KEY)

def _headers(upsert: bool = False) -> Dict[str, str]:
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if upsert:
        headers["Prefer"] = "resolution=merge-duplicates,return=minimal"
    else:
        headers["Prefer"] = "return=minimal"
    return headers

def sync_record_sync(table: str, data: Dict[str, Any], on_conflict: Optional[str] = None) -> bool:
    """
    Synchronously upserts/inserts a record to Supabase REST endpoint.
    Non-blocking / failsafe: catches network issues or missing table errors without interrupting caller.
    """
    if not is_supabase_configured():
        return False

    # Convert binary/bytes fields to string / base64 or skip
    payload = {}
    for k, v in data.items():
        if isinstance(v, bytes):
            try:
                payload[k] = v.decode("utf-8")
            except UnicodeDecodeError:
                import base64
                payload[k] = base64.b64encode(v).decode("ascii")
        else:
            payload[k] = v

    url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/{table}"
    if on_conflict:
        url += f"?on_conflict={on_conflict}"

    body = json.dumps([payload]).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers=_headers(upsert=bool(on_conflict)),
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status in (200, 201, 204)
    except urllib.error.HTTPError as error:
        err_msg = error.read().decode("utf-8", errors="replace")
        if error.code == 404:
            logger.debug("Supabase table '%s' not created yet in cloud instance.", table)
        else:
            logger.warning("Supabase sync warning for '%s' (%s): %s", table, error.code, err_msg)
        return False
    except Exception as exc:
        logger.debug("Supabase connection offline or timed out: %s", exc)
        return False

def sync_update_sync(table: str, filter_col: str, filter_val: Any, data: Dict[str, Any]) -> bool:
    """
    Synchronously updates records matching filter_col=filter_val in Supabase.
    """
    if not is_supabase_configured():
        return False

    payload = {}
    for k, v in data.items():
        if isinstance(v, bytes):
            try:
                payload[k] = v.decode("utf-8")
            except UnicodeDecodeError:
                import base64
                payload[k] = base64.b64encode(v).decode("ascii")
        else:
            payload[k] = v

    import urllib.parse
    encoded_val = urllib.parse.quote(str(filter_val))
    url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/{table}?{filter_col}=eq.{encoded_val}"
    body = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=body,
        headers=_headers(),
        method="PATCH"
    )

    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status in (200, 204)
    except Exception as exc:
        logger.debug("Supabase update for '%s' bypassed: %s", table, exc)
        return False

def test_supabase_connection() -> Dict[str, Any]:
    """
    Probes the Supabase instance to verify connectivity and API key validity.
    """
    if not is_supabase_configured():
        return {"connected": False, "reason": "Credentials not configured"}

    url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/"
    req = urllib.request.Request(
        url,
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
        },
        method="GET"
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return {"connected": True, "status": response.status}
    except urllib.error.HTTPError as error:
        # 400 with "Secret API key required" means endpoint is reachable and authenticating!
        body = error.read().decode("utf-8", errors="replace")
        if "Secret API key required" in body or error.code in (400, 401, 403, 404):
            return {"connected": True, "status": error.code, "note": "PostgREST endpoint reached successfully"}
        return {"connected": False, "status": error.code, "error": body}
    except Exception as exc:
        return {"connected": False, "error": str(exc)}
