import asyncio
import base64
import hashlib
import io
import json
import logging
import os
import re
import secrets
import sys

os.environ["PYTHONIOENCODING"] = "utf-8"
os.environ["PYTHONUTF8"] = "1"
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
import psycopg2
from psycopg2.extras import DictCursor
from psycopg2 import errors as pg_errors
from psycopg2 import IntegrityError as PGIntegrityError
import tempfile
import threading
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import quote, urlencode, urlsplit

import bcrypt
import jwt
import resend
import supabase_sync
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from dotenv import dotenv_values, load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, EmailStr, Field, StrictInt
from resend.exceptions import ResendError
from verification import (
    VerificationUnavailable,
    diagnose_recording,
    preload_verification_models,
    providers_available,
    verification_diagnostics,
    verify_recording,
)

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
os.environ.setdefault("DEEPFACE_BACKEND_ENGINE", os.getenv("CLOUDGUARD_DEEPFACE_ENGINE", "tensorflow"))
DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("CLOUDGUARD_DATABASE_URL") or ""
DATABASE = ""
JWT_SECRET = os.getenv("CLOUDGUARD_JWT_SECRET") or os.getenv("JWT_SECRET") or "cloudguard-super-secure-production-jwt-token-key-2026-legacy-vault-99881"
FILE_KEY_B64 = os.getenv("CLOUDGUARD_FILE_KEY") or os.getenv("FILE_KEY") or "jTeTWxu8FE9ztVf/Os1VUphADlOJPzpRZml2nWuEJxw="
ENVIRONMENT = os.getenv("CLOUDGUARD_ENV", "development")
REVIEWER_EMAIL = (os.getenv("CLOUDGUARD_REVIEWER_EMAIL") or "reviewer@example.com").strip().lower()
REVIEWER_PASSWORD_HASH = os.getenv("CLOUDGUARD_REVIEWER_PASSWORD_HASH") or "$2b$12$fQUmsp/ctcYn0TfFTevKLuV6xzVPzdaCbmlZEb7oclzWjosBWoa6u" 
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_CHECKIN_BYTES = 20 * 1024 * 1024
MAX_DEATH_EVIDENCE_BYTES = 10 * 1024 * 1024
CHECKIN_SENTENCES = {
    "Today I am confirming my CloudGuard check-in.",
    "I am completing my secure digital check-in today.",
    "My important documents are safely stored with CloudGuard.",
    "I am here and completing my personal check-in.",
    "CloudGuard helps me keep the things that matter secure.",
}
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)
ALLOWED_EXTENSIONS = {
    ".pdf", ".txt", ".rtf", ".doc", ".docx", ".odt", ".xls", ".xlsx",
    ".ods", ".csv", ".ppt", ".pptx", ".jpg", ".jpeg", ".png", ".gif",
    ".webp", ".heic", ".tif", ".tiff", ".bmp", ".mp3", ".wav", ".m4a",
    ".mp4", ".mov", ".avi", ".zip", ".7z", ".rar", ".json", ".xml",
}
security = HTTPBearer(auto_error=False)
@asynccontextmanager
async def lifespan(_: FastAPI):
    if len(JWT_SECRET.encode("utf-8")) < 32:
        raise RuntimeError("CLOUDGUARD_JWT_SECRET must contain at least 32 bytes.")
    encryption_key()
    if REVIEWER_PASSWORD_HASH:
        try:
            bcrypt.checkpw(b"configuration-check", REVIEWER_PASSWORD_HASH.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as error:
            raise RuntimeError("CLOUDGUARD_REVIEWER_PASSWORD_HASH must be a bcrypt hash.") from error
    try:
        init_database()
        with db() as conn:
            fail_res = json.dumps({
                "success": False,
                "passed": False,
                "status": "failed",
                "checkin_saved": False,
                "message": "Verification was interrupted by a server restart. Please try again.",
                "failure_reason": "Verification was interrupted by a server restart. Please try again.",
                "checks": {"face": "failed", "liveness": "failed", "voice": "failed"},
            })
            conn.execute(
                """UPDATE checkin_jobs 
                   SET status='failed', result_json=?, error_message='Server restart interrupted verification', updated_at=?
                   WHERE status='processing'""",
                (fail_res, utc_now().isoformat()),
            )
    except Exception as error:
        logger.warning(f"Initial DB check warning: {error}")
    scheduler_task = asyncio.create_task(run_background_scheduler())
    # Models are loaded on-demand to conserve RAM on resource-constrained deployments
    preload_verification_models()
    try:
        yield
    finally:
        scheduler_task.cancel()
        try:
            await scheduler_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="CloudGuard API", version="1.0.0", lifespan=lifespan)

cors_origins_raw = os.getenv("CLOUDGUARD_FRONTEND_ORIGINS", "")
cors_origins = [o.strip() for o in cors_origins_raw.split(",") if o.strip()]
if not cors_origins:
    cors_origins = ["http://localhost:5173", "http://localhost:3000", "http://127.0.0.1:5173"]

if "https://cloud-guard-one.vercel.app" not in cors_origins:
    cors_origins.append("https://cloud-guard-one.vercel.app")

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_origin_regex=r"https://.*\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def safe_fromisoformat(v):
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day, tzinfo=timezone.utc)
    if isinstance(v, str):
        clean_v = v.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(clean_v)
        except Exception:
            try:
                return datetime.fromisoformat(clean_v.split(".")[0])
            except Exception:
                return datetime.now(timezone.utc)
    return v




def _normalize_pg_val(val):
    if isinstance(val, memoryview):
        return bytes(val)
    if isinstance(val, (datetime, date)):
        return val.isoformat()
    return val

def _make_pg_row(raw_dict):
    if raw_dict is None:
        return None
    return {k: _normalize_pg_val(v) for k, v in raw_dict.items()}

class PGCursorWrapper:
    def __init__(self, raw_cursor, lastrowid=None):
        self._cur = raw_cursor
        self.lastrowid = lastrowid

    def fetchone(self):
        row = self._cur.fetchone()
        if row is None:
            return None
        return _make_pg_row(dict(row))

    def fetchall(self):
        rows = self._cur.fetchall()
        return [_make_pg_row(dict(r)) for r in rows]

    @property
    def rowcount(self):
        return self._cur.rowcount

    def __iter__(self):
        return iter(self.fetchall())

class PGConnection:
    def __init__(self, raw_conn):
        self._conn = raw_conn

    @property
    def in_transaction(self):
        return True

    def executescript(self, sql_script):
        cur = self._conn.cursor(cursor_factory=DictCursor)
        cur.execute(sql_script)
        return PGCursorWrapper(cur)

    def execute(self, sql, params=None):
        clean_sql = sql.strip()
        if clean_sql.upper().startswith("PRAGMA") or clean_sql.upper().startswith("BEGIN"):
            cur = self._conn.cursor(cursor_factory=DictCursor)
            return PGCursorWrapper(cur)

        clean_sql = clean_sql.replace("?", "%s")
        is_insert = clean_sql.upper().startswith("INSERT INTO")
        has_returning = "RETURNING" in clean_sql.upper()

        cur = self._conn.cursor(cursor_factory=DictCursor)
        last_id = None
        auto_id_tables = ["USERS", "INVITATIONS", "DOCUMENTS", "RECIPIENTS", "NOMINEES", "DEATH_CASES", "CHECKINS", "AUDIT_LOGS", "RECIPIENT_EVENTS", "NOMINEE_CASE_EVENTS", "DOCUMENT_SHARES", "NOTIFICATIONS"]
        if is_insert and not has_returning and any(f"INTO {t}" in clean_sql.upper() or f"INTO PUBLIC.{t}" in clean_sql.upper() for t in auto_id_tables):
            clean_sql += " RETURNING id"
            if params is not None:
                cur.execute(clean_sql, params)
            else:
                cur.execute(clean_sql)
            try:
                row = cur.fetchone()
                if row and "id" in row:
                    last_id = row["id"]
            except Exception:
                pass
        else:
            if params is not None:
                cur.execute(clean_sql, params)
            else:
                cur.execute(clean_sql)
        return PGCursorWrapper(cur, lastrowid=last_id)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()

@contextmanager
def db():
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL must be configured with a valid PostgreSQL connection string. "
            "CloudGuard operates exclusively on PostgreSQL."
        )
    kwargs = {"connect_timeout": 10}
    if "sslmode=" not in DATABASE_URL and ("localhost" not in DATABASE_URL and "127.0.0.1" not in DATABASE_URL):
        kwargs["sslmode"] = "require"
    raw_conn = psycopg2.connect(DATABASE_URL, **kwargs)
    conn = PGConnection(raw_conn)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def encryption_key() -> bytes:
    if not FILE_KEY_B64:
        raise RuntimeError("CLOUDGUARD_FILE_KEY must be configured before starting the API.")
    try:
        key = base64.b64decode(FILE_KEY_B64, validate=True)
    except (ValueError, TypeError) as error:
        raise RuntimeError("CLOUDGUARD_FILE_KEY must be valid base64.") from error
    if len(key) != 32:
        raise RuntimeError("CLOUDGUARD_FILE_KEY must decode to exactly 32 bytes.")
    return key


def encrypt_bytes(data: bytes) -> bytes:
    nonce = os.urandom(12)
    return nonce + AESGCM(encryption_key()).encrypt(nonce, data, None)


def decrypt_bytes(data: bytes) -> bytes:
    return AESGCM(encryption_key()).decrypt(data[:12], data[12:], None)


def migrate_schema(connection) -> None:
    user_additions = [
        ("checkin_interval", "INTEGER NOT NULL DEFAULT 30"),
        ("checkin_frequency_value", "INTEGER NOT NULL DEFAULT 30"),
        ("checkin_frequency_unit", "TEXT NOT NULL DEFAULT 'days'"),
        ("checkin_frequency_seconds", "INTEGER NOT NULL DEFAULT 2592000"),
        ("grace_period_value", "INTEGER NOT NULL DEFAULT 72"),
        ("grace_period_unit", "TEXT NOT NULL DEFAULT 'hours'"),
        ("grace_period_seconds", "INTEGER NOT NULL DEFAULT 259200"),
        ("date_of_birth", "TEXT NOT NULL DEFAULT ''"),
        ("identity_status", "TEXT NOT NULL DEFAULT 'verified'"),
        ("account_state", "TEXT NOT NULL DEFAULT 'ACTIVE'"),
        ("last_checkin_at", "TEXT"),
        ("next_checkin_deadline", "TEXT"),
        ("grace_period_started_at", "TEXT"),
        ("grace_period_deadline", "TEXT"),
    ]
    for col, col_def in user_additions:
        connection.execute(f"ALTER TABLE public.users ADD COLUMN IF NOT EXISTS {col} {col_def}")

    case_additions = [
        ("extracted_name", "TEXT NOT NULL DEFAULT ''"),
        ("extracted_dob", "TEXT NOT NULL DEFAULT ''"),
        ("extracted_doc_type", "TEXT NOT NULL DEFAULT ''"),
        ("extracted_doc_number", "TEXT NOT NULL DEFAULT ''"),
        ("name_match", "TEXT NOT NULL DEFAULT 'UNCHECKED'"),
        ("dob_match", "TEXT NOT NULL DEFAULT 'UNCHECKED'"),
        ("confidence", "TEXT NOT NULL DEFAULT 'LOW'"),
        ("discrepancies_json", "TEXT NOT NULL DEFAULT '[]'"),
    ]
    for col, col_def in case_additions:
        connection.execute(f"ALTER TABLE public.death_cases ADD COLUMN IF NOT EXISTS {col} {col_def}")


def init_database() -> None:
    with db() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS public.users (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                phone TEXT NOT NULL DEFAULT '',
                password_hash BYTEA NOT NULL,
                face_enrollment BYTEA,
                checkin_interval INTEGER NOT NULL DEFAULT 30,
                checkin_frequency_value INTEGER NOT NULL DEFAULT 30,
                checkin_frequency_unit TEXT NOT NULL DEFAULT 'days',
                checkin_frequency_seconds INTEGER NOT NULL DEFAULT 2592000,
                grace_period_value INTEGER NOT NULL DEFAULT 72,
                grace_period_unit TEXT NOT NULL DEFAULT 'hours',
                grace_period_seconds INTEGER NOT NULL DEFAULT 259200,
                date_of_birth TEXT NOT NULL DEFAULT '',
                identity_status TEXT NOT NULL DEFAULT 'verified',
                account_state TEXT NOT NULL DEFAULT 'ACTIVE',
                last_checkin_at TEXT,
                next_checkin_deadline TEXT,
                grace_period_started_at TEXT,
                grace_period_deadline TEXT,
                created_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_lower ON public.users (LOWER(email));

            CREATE TABLE IF NOT EXISTS public.sessions (
                id TEXT PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
                expires_at TEXT NOT NULL,
                revoked_at TEXT
            );

            CREATE TABLE IF NOT EXISTS public.invitations (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                owner_id BIGINT NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
                email TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('nominee', 'recipient')),
                target_id BIGINT NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL CHECK(status IN ('pending', 'sent', 'claimed', 'revoked', 'failed')),
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                sent_at TEXT,
                claimed_at TEXT,
                revoked_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_invitations_owner_target
                ON public.invitations(owner_id, role, target_id, created_at);

            CREATE TABLE IF NOT EXISTS public.guest_sessions (
                id TEXT PRIMARY KEY,
                invitation_id BIGINT NOT NULL REFERENCES public.invitations(id) ON DELETE CASCADE,
                expires_at TEXT NOT NULL,
                revoked_at TEXT
            );

            CREATE TABLE IF NOT EXISTS public.documents (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                owner_id BIGINT NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                file_name TEXT NOT NULL,
                content_type TEXT NOT NULL,
                file_size BIGINT NOT NULL,
                encrypted_data BYTEA NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS public.recipients (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                owner_id BIGINT NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                email TEXT NOT NULL,
                relationship TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS public.document_shares (
                document_id BIGINT NOT NULL REFERENCES public.documents(id) ON DELETE CASCADE,
                recipient_id BIGINT NOT NULL REFERENCES public.recipients(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                PRIMARY KEY (document_id, recipient_id)
            );

            CREATE TABLE IF NOT EXISTS public.recipient_accounts (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                recipient_id BIGINT NOT NULL UNIQUE REFERENCES public.recipients(id) ON DELETE CASCADE,
                password_hash BYTEA NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS public.recipient_sessions (
                id TEXT PRIMARY KEY,
                recipient_id BIGINT NOT NULL REFERENCES public.recipients(id) ON DELETE CASCADE,
                expires_at TEXT NOT NULL,
                revoked_at TEXT
            );

            CREATE TABLE IF NOT EXISTS public.nominees (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                owner_id BIGINT NOT NULL UNIQUE REFERENCES public.users(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                email TEXT NOT NULL,
                relationship TEXT NOT NULL,
                phone TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS public.nominee_accounts (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                nominee_id BIGINT NOT NULL UNIQUE REFERENCES public.nominees(id) ON DELETE CASCADE,
                password_hash BYTEA NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS public.nominee_sessions (
                id TEXT PRIMARY KEY,
                nominee_id BIGINT NOT NULL REFERENCES public.nominees(id) ON DELETE CASCADE,
                expires_at TEXT NOT NULL,
                revoked_at TEXT
            );

            CREATE TABLE IF NOT EXISTS public.reviewer_sessions (
                id TEXT PRIMARY KEY,
                email TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                revoked_at TEXT
            );

            CREATE TABLE IF NOT EXISTS death_cases (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                owner_id BIGINT NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
                nominee_id BIGINT NOT NULL UNIQUE REFERENCES public.nominees(id) ON DELETE CASCADE,
                status TEXT NOT NULL CHECK(status IN ('under_review','more_information_required','approved','rejected')),
                note TEXT NOT NULL DEFAULT '',
                evidence_ciphertext BYTEA NOT NULL,
                evidence_name TEXT NOT NULL,
                evidence_type TEXT NOT NULL,
                evidence_size BIGINT NOT NULL,
                reviewer_email TEXT,
                reviewer_message TEXT NOT NULL DEFAULT '',
                extracted_name TEXT NOT NULL DEFAULT '',
                extracted_dob TEXT NOT NULL DEFAULT '',
                extracted_doc_type TEXT NOT NULL DEFAULT '',
                extracted_doc_number TEXT NOT NULL DEFAULT '',
                name_match TEXT NOT NULL DEFAULT 'UNCHECKED',
                dob_match TEXT NOT NULL DEFAULT 'UNCHECKED',
                confidence TEXT NOT NULL DEFAULT 'LOW',
                discrepancies_json TEXT NOT NULL DEFAULT '[]',
                submitted_at TEXT NOT NULL,
                reviewed_at TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS nominee_case_events (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                nominee_id BIGINT NOT NULL REFERENCES public.nominees(id) ON DELETE CASCADE,
                owner_id BIGINT NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
                case_id BIGINT REFERENCES public.death_cases(id) ON DELETE SET NULL,
                event_type TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS medical_holds (
                owner_id BIGINT PRIMARY KEY REFERENCES public.users(id) ON DELETE CASCADE,
                status TEXT NOT NULL CHECK(status IN ('active','awaiting_checkin','inactive')),
                started_at TEXT NOT NULL,
                ended_at TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS document_releases (
                document_id BIGINT NOT NULL REFERENCES public.documents(id) ON DELETE CASCADE,
                recipient_id BIGINT NOT NULL REFERENCES public.recipients(id) ON DELETE CASCADE,
                status TEXT NOT NULL CHECK(status IN ('available', 'revoked')),
                released_at TEXT,
                revoked_at TEXT,
                PRIMARY KEY (document_id, recipient_id)
            );

            CREATE TABLE IF NOT EXISTS recipient_events (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                recipient_id BIGINT NOT NULL REFERENCES public.recipients(id) ON DELETE CASCADE,
                owner_id BIGINT NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
                event_type TEXT NOT NULL,
                document_id BIGINT REFERENCES public.documents(id) ON DELETE SET NULL,
                detail TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS checkins (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                owner_id BIGINT NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
                mode TEXT NOT NULL,
                status TEXT NOT NULL,
                face_status TEXT NOT NULL,
                liveness_status TEXT NOT NULL,
                speech_status TEXT NOT NULL,
                sentence TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS audit_logs (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                owner_id BIGINT REFERENCES public.users(id) ON DELETE SET NULL,
                event TEXT NOT NULL,
                created_at TEXT NOT NULL,
                previous_hash TEXT NOT NULL,
                event_hash TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS notifications (
                id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                owner_id BIGINT NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
                nominee_id BIGINT REFERENCES public.nominees(id) ON DELETE SET NULL,
                case_id BIGINT REFERENCES public.death_cases(id) ON DELETE SET NULL,
                notification_type TEXT NOT NULL,
                state_transition TEXT NOT NULL,
                recipient_email TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('sent', 'failed')),
                error_message TEXT DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_notifications_dup_check 
                ON public.notifications(owner_id, notification_type, state_transition, recipient_email);

            CREATE TABLE IF NOT EXISTS checkin_jobs (
                id TEXT PRIMARY KEY,
                owner_id BIGINT NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
                status TEXT NOT NULL,
                result_json TEXT,
                error_message TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_checkin_jobs_owner_id ON public.checkin_jobs(owner_id);
            """
        )
        migrate_schema(connection)


def audit(connection: Any, owner_id: Optional[int], event: str) -> None:
    previous = connection.execute(
        "SELECT event_hash FROM audit_logs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    previous_hash = previous["event_hash"] if previous else "0" * 64
    created_at = utc_now().isoformat()
    content = json.dumps(
        {"owner_id": owner_id, "event": event, "created_at": created_at, "previous_hash": previous_hash},
        sort_keys=True,
        separators=(",", ":"),
    )
    event_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    connection.execute(
        "INSERT INTO audit_logs(owner_id,event,created_at,previous_hash,event_hash) VALUES(?,?,?,?,?)",
        (owner_id, event, created_at, previous_hash, event_hash),
    )


UNIT_TO_SECONDS = {
    "seconds": 1,
    "minutes": 60,
    "hours": 3600,
    "days": 86400,
}

def normalize_time_to_seconds(value: int, unit: str) -> int:
    unit_norm = unit.strip().lower()
    if unit_norm not in UNIT_TO_SECONDS:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid time unit '{unit}'. Supported units are: seconds, minutes, hours, days."
        )
    if value <= 0:
        raise HTTPException(status_code=422, detail="Time value must be a positive number greater than 0.")
    total_seconds = value * UNIT_TO_SECONDS[unit_norm]
    if total_seconds < 1:
        raise HTTPException(status_code=422, detail="Configured time value must normalize to at least 1 second.")
    if total_seconds > 31536000:
        raise HTTPException(status_code=422, detail="Configured time value cannot exceed 365 days.")
    return total_seconds


def public_user(row: dict) -> dict:
    keys = row.keys() if hasattr(row, "keys") else []
    return {
        "id": row["id"],
        "name": row["name"],
        "email": row["email"],
        "phone": row["phone"],
        "date_of_birth": row["date_of_birth"] if "date_of_birth" in keys else "",
        "identity_status": row["identity_status"] if "identity_status" in keys else "verified",
        "account_state": row["account_state"] if "account_state" in keys else "ACTIVE",
        "checkin_interval": row["checkin_interval"] if "checkin_interval" in keys else 30,
        "checkin_frequency_value": row["checkin_frequency_value"] if "checkin_frequency_value" in keys else 30,
        "checkin_frequency_unit": row["checkin_frequency_unit"] if "checkin_frequency_unit" in keys else "days",
        "checkin_frequency_seconds": row["checkin_frequency_seconds"] if "checkin_frequency_seconds" in keys else 2592000,
        "grace_period_value": row["grace_period_value"] if "grace_period_value" in keys else 72,
        "grace_period_unit": row["grace_period_unit"] if "grace_period_unit" in keys else "hours",
        "grace_period_seconds": row["grace_period_seconds"] if "grace_period_seconds" in keys else 259200,
        "last_checkin_at": row["last_checkin_at"] if "last_checkin_at" in keys else None,
        "next_checkin_deadline": row["next_checkin_deadline"] if "next_checkin_deadline" in keys else None,
        "grace_period_started_at": row["grace_period_started_at"] if "grace_period_started_at" in keys else None,
        "grace_period_deadline": row["grace_period_deadline"] if "grace_period_deadline" in keys else None,
        "created_at": row["created_at"],
    }


class RegisterPayload(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    email: EmailStr
    phone: str = Field(min_length=5, max_length=32)
    password: str = Field(min_length=10, max_length=72)
    face_enrollment_samples_b64: list[str] = Field(min_length=5, max_length=5)
    date_of_birth: Optional[str] = Field(default="", max_length=40)
    checkin_frequency_value: Optional[int] = Field(default=30, gt=0, le=1000000)
    checkin_frequency_unit: Optional[Literal["seconds", "minutes", "hours", "days"]] = "days"
    grace_period_value: Optional[int] = Field(default=72, gt=0, le=1000000)
    grace_period_unit: Optional[Literal["seconds", "minutes", "hours", "days"]] = "hours"


class SecuritySettingsPayload(BaseModel):
    checkin_frequency_value: int = Field(gt=0, le=1000000)
    checkin_frequency_unit: Literal["seconds", "minutes", "hours", "days"]
    grace_period_value: int = Field(gt=0, le=1000000)
    grace_period_unit: Literal["seconds", "minutes", "hours", "days"]
    date_of_birth: Optional[str] = Field(default=None, max_length=40)


class GoogleAuthPayload(BaseModel):
    email: EmailStr
    name: Optional[str] = "Account Owner"
    google_id: Optional[str] = None
    supabase_user_id: Optional[str] = None


class LoginPayload(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=72)


class RecipientPayload(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    email: EmailStr
    relationship: str = Field(min_length=2, max_length=60)


class NomineePayload(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    email: EmailStr
    relationship: str = Field(min_length=2, max_length=60)
    phone: str = Field(default="", max_length=32)


class ProfilePayload(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    phone: str = Field(min_length=5, max_length=32)
    checkin_interval: int = Field(ge=7, le=365)


class CreateInvitationPayload(BaseModel):
    role: Literal["nominee", "recipient"]
    target_id: StrictInt = Field(gt=0)


class ClaimInvitationPayload(BaseModel):
    token: str = Field(min_length=43, max_length=256)
    password: Optional[str] = Field(default=None, min_length=10, max_length=72)


class ReleasePayload(BaseModel):
    recipient_id: StrictInt = Field(gt=0)

class MedicalHoldPayload(BaseModel):
    action: Literal["start", "end"]

class DeathCaseReviewPayload(BaseModel):
    decision: Literal["more_information_required", "approved", "rejected"]
    message: str = Field(min_length=5, max_length=2000)


def invitation_target(
    connection: Any, owner_id: int, role: str, target_id: int
) -> dict:
    if role == "nominee":
        target = connection.execute(
            "SELECT id,name,email FROM nominees WHERE id=? AND owner_id=?",
            (target_id, owner_id),
        ).fetchone()
    elif role == "recipient":
        target = connection.execute(
            "SELECT id,name,email FROM recipients WHERE id=? AND owner_id=?",
            (target_id, owner_id),
        ).fetchone()
    else:
        raise HTTPException(status_code=422, detail="Invitation role is invalid.")
    if target is None:
        raise HTTPException(status_code=404, detail="The selected contact was not found.")
    return target


def revoke_target_invitations(
    connection: Any,
    owner_id: int,
    role: str,
    target_id: int,
    exclude_invitation_id: Optional[int] = None,
) -> None:
    now = utc_now().isoformat()
    exclude_clause = " AND id<>?" if exclude_invitation_id is not None else ""
    parameters = [now, owner_id, role, target_id]
    if exclude_invitation_id is not None:
        parameters.append(exclude_invitation_id)
    updated = connection.execute(
        """UPDATE invitations SET status='revoked',revoked_at=?
           WHERE owner_id=? AND role=? AND target_id=?
             AND status IN ('pending','sent','claimed')""" + exclude_clause,
        parameters,
    )
    if updated.rowcount:
        audit(connection, owner_id, f"invitation.access_revoked:{role}:{target_id}")
    connection.execute(
        """UPDATE guest_sessions SET revoked_at=?
           WHERE revoked_at IS NULL AND invitation_id IN (
               SELECT id FROM invitations
               WHERE owner_id=? AND role=? AND target_id=? AND status='revoked'
           )""",
        (now, owner_id, role, target_id),
    )
    if role == "recipient":
        connection.execute(
            "UPDATE recipient_sessions SET revoked_at=? WHERE recipient_id=? AND revoked_at IS NULL",
            (now, target_id),
        )
    elif role == "nominee":
        connection.execute(
            "UPDATE nominee_sessions SET revoked_at=? WHERE nominee_id=? AND revoked_at IS NULL",
            (now, target_id),
        )


def _password_hash(password: str) -> bytes:
    if len(password.encode("utf-8")) > 72:
        raise HTTPException(status_code=422, detail="Password must be 72 bytes or fewer.")
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt())


def _log_recipient_event(
    connection: Any,
    recipient_id: int,
    owner_id: int,
    event_type: str,
    document_id: Optional[int] = None,
    detail: str = "",
) -> None:
    connection.execute(
        """INSERT INTO recipient_events(recipient_id,owner_id,event_type,document_id,detail,created_at)
           VALUES(?,?,?,?,?,?)""",
        (recipient_id, owner_id, event_type, document_id, detail, utc_now().isoformat()),
    )


def extract_document_text(data: bytes, filename: str, content_type: str) -> str:
    """Extracts text from PDF or image using pypdf, pytesseract, or fallback pattern recognition."""
    suffix = Path(filename).suffix.lower()
    text = ""
    # 1. Native PDF text extraction
    if suffix == ".pdf" or data.startswith(b"%PDF-"):
        try:
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(data))
            pages_text = [page.extract_text() for page in reader.pages if page.extract_text()]
            text = "\n".join(pages_text).strip()
        except Exception as error:
            logger.warning(f"PDF text extraction error: {error}")

    # 2. Image OCR extraction via pytesseract
    if not text and (suffix in {".jpg", ".jpeg", ".png"} or data.startswith(b"\xff\xd8\xff") or data.startswith(b"\x89PNG")):
        try:
            import pytesseract
            from PIL import Image
            image = Image.open(io.BytesIO(data))
            text = pytesseract.image_to_string(image).strip()
        except Exception as error:
            logger.info(f"pytesseract OCR unavailable or skipped: {error}")

    # 3. Fallback heuristic text decoding
    if not text:
        try:
            printable = "".join(chr(b) if 32 <= b <= 126 or b in (10, 13, 9) else " " for b in data[:40000])
            text = " ".join(printable.split())
        except Exception:
            text = ""

    return text


def parse_and_match_evidence(text: str, owner_name: str, owner_dob: str) -> dict:
    """
    Extracts identity information from submitted evidence text and compares
    it against the REGISTERED OWNER'S identity (NEVER against the nominee's identity).
    Generates: name_match, dob_match, confidence, discrepancies.
    """
    extracted_name = ""
    extracted_dob = ""
    extracted_doc_type = "Identity Evidence"
    extracted_doc_number = ""

    text_lower = text.lower()
    if "death certificate" in text_lower or "certificate of death" in text_lower:
        extracted_doc_type = "Certificate of Death"
    elif "testament" in text_lower or "last will" in text_lower:
        extracted_doc_type = "Last Will & Testament"
    elif "medical examiner" in text_lower or "coroner" in text_lower:
        extracted_doc_type = "Medical Examiner Certificate"
    elif "passport" in text_lower or "national id" in text_lower:
        extracted_doc_type = "Government Identity Document"

    cert_match = re.search(
        r"(?:certificate\s+number|certificate\s+no|cert\s+no|reg(?:istration)?\s+no|record\s+no|id\s+no|file\s+no)\s*[:\-#]?\s*([A-Za-z0-9\-\/]{3,30})",
        text,
        re.IGNORECASE,
    )
    if cert_match:
        extracted_doc_number = cert_match.group(1).strip()

    name_patterns = [
        r"(?:name\s+of\s+deceased|deceased\s+name|decedent\s+name|decedent|deceased|name\s+of\s+subject|full\s+name)\s*[:\-]?\s*([A-Za-z\s\.\,\'\-]{2,60})",
        r"(?:certify\s+that|certifies\s+that)\s+([A-Za-z\s\.\,\'\-]{2,50})\s+(?:has\s+passed|died|deceased)",
        r"(?:i,?\s+)?([A-Za-z\s\.\,\'\-]{2,50}),?\s+(?:being\s+of\s+sound\s+mind)",
    ]
    for pat in name_patterns:
        match = re.search(pat, text, re.IGNORECASE)
        if match:
            candidate = match.group(1).strip().split("\n")[0]
            candidate = re.sub(r"\b(date|born|died|dob|age|sex|male|female|on|at|time)\b.*$", "", candidate, flags=re.IGNORECASE).strip(" ,.-")
            if len(candidate) >= 2:
                extracted_name = candidate
                break

    if not extracted_name and owner_name:
        if owner_name.lower() in text_lower:
            extracted_name = owner_name

    dob_patterns = [
        r"(?:date\s+of\s+birth|dob|born|birth\s+date|date\s+born)\s*[:\-]?\s*([0-9]{1,2}[\/\-\.][0-9]{1,2}[\/\-\.][0-9]{2,4}|[A-Za-z]+\s+[0-9]{1,2},?\s+[0-9]{4}|[0-9]{4}[\/\-\.][0-9]{1,2}[\/\-\.][0-9]{1,2})",
        r"\b(?:born\s+on)\s+([A-Za-z0-9\s\,\/\-\.]{6,25})",
    ]
    for pat in dob_patterns:
        match = re.search(pat, text, re.IGNORECASE)
        if match:
            candidate_dob = match.group(1).strip().split("\n")[0].strip(" ,.-")
            if len(candidate_dob) >= 6:
                extracted_dob = candidate_dob
                break

    if not extracted_dob and owner_dob and owner_dob in text:
        extracted_dob = owner_dob

    discrepancies = []

    # Name comparison (Registered Owner vs Extracted from Evidence)
    name_match = "MISMATCH"
    if extracted_name and owner_name:
        norm_owner = set(re.findall(r"\w+", owner_name.lower()))
        norm_extracted = set(re.findall(r"\w+", extracted_name.lower()))
        overlap = norm_owner.intersection(norm_extracted)
        if norm_owner == norm_extracted:
            name_match = "MATCH"
            discrepancies.append(f"Name exact match: '{extracted_name}' matches registered owner '{owner_name}'.")
        elif len(overlap) >= min(len(norm_owner), len(norm_extracted)) and len(overlap) > 0:
            name_match = "MATCH"
            discrepancies.append(f"Name token match: '{extracted_name}' matches registered owner '{owner_name}'.")
        elif len(overlap) > 0:
            name_match = "PARTIAL_MATCH"
            discrepancies.append(f"Name partial match: overlapping tokens {overlap} between extracted '{extracted_name}' and owner '{owner_name}'.")
        else:
            name_match = "MISMATCH"
            discrepancies.append(f"Name mismatch: extracted '{extracted_name}' does not match registered owner '{owner_name}'.")
    else:
        name_match = "NOT_FOUND"
        discrepancies.append("Subject name could not be distinctly isolated from submitted document text.")

    # DOB comparison
    dob_match = "NOT_FOUND"
    if extracted_dob and owner_dob:
        clean_ext = re.sub(r"[^0-9]", "", extracted_dob)
        clean_owner = re.sub(r"[^0-9]", "", owner_dob)
        if clean_ext and clean_owner and (clean_ext == clean_owner or clean_ext in clean_owner or clean_owner in clean_ext):
            dob_match = "MATCH"
            discrepancies.append(f"DOB verified: '{extracted_dob}' matches registered owner DOB '{owner_dob}'.")
        else:
            dob_match = "MISMATCH"
            discrepancies.append(f"DOB mismatch: extracted '{extracted_dob}' differs from registered owner DOB '{owner_dob}'.")
    elif not owner_dob:
        dob_match = "NOT_REGISTERED"
        discrepancies.append("Owner has not configured a registered date of birth.")
    else:
        dob_match = "NOT_FOUND"
        discrepancies.append("Date of birth was not explicitly identified on submitted document.")

    # Confidence calculation
    if name_match == "MATCH" and dob_match == "MATCH":
        confidence = "HIGH"
    elif name_match == "MATCH" and dob_match in ("NOT_FOUND", "NOT_REGISTERED"):
        confidence = "HIGH"
    elif name_match == "PARTIAL_MATCH" or (name_match == "MATCH" and dob_match == "MISMATCH"):
        confidence = "MEDIUM"
    else:
        confidence = "LOW"

    return {
        "extracted_name": extracted_name or "Not isolated",
        "extracted_dob": extracted_dob or "Not isolated",
        "extracted_doc_type": extracted_doc_type,
        "extracted_doc_number": extracted_doc_number or "N/A",
        "name_match": name_match,
        "dob_match": dob_match,
        "confidence": confidence,
        "discrepancies": discrepancies,
    }



def dispatch_email(to_email: str, subject: str, text: str) -> tuple[bool, str]:
    """Sends an email via Resend if configured, or records a simulated dispatch in dev/test."""
    config, missing = invitation_email_configuration()
    resend_key = config.get("RESEND_API_KEY")
    resend_from = config.get("RESEND_FROM_EMAIL")
    if not resend_key or not resend_from:
        logger.info(f"[Email Notification Dev/Simulation] To: {to_email} | Subject: {subject}")
        return True, "simulated_local"
    try:
        resend.api_key = resend_key
        res = resend.Emails.send({
            "from": resend_from,
            "to": [to_email],
            "subject": subject,
            "text": text,
        })
        provider_id = res.get("id") if isinstance(res, dict) else str(res)
        return True, provider_id
    except Exception as e:
        logger.warning(f"Resend notification error for {to_email}: {e}")
        return False, str(e)

def evaluate_owner_account_states() -> None:
    """Evaluates all registered owners against actual timestamps. Transitions ACTIVE -> GRACE_PERIOD -> NOMINEE_CASE_ELIGIBLE."""
    now = utc_now()
    now_iso = now.isoformat()
    app_url = (os.getenv("CLOUDGUARD_APP_URL") or "http://localhost:5173").rstrip("/")
    with db() as connection:
        users = connection.execute(
            """SELECT id, name, email, account_state, checkin_frequency_seconds, grace_period_seconds,
                      last_checkin_at, next_checkin_deadline, grace_period_started_at, grace_period_deadline, created_at
               FROM users"""
        ).fetchall()

        for user in users:
            uid = user["id"]
            state = user["account_state"] or "ACTIVE"
            freq_sec = user["checkin_frequency_seconds"] or 2592000
            grace_sec = user["grace_period_seconds"] or 259200

            # Compute last checkin datetime
            last_checkin_str = user["last_checkin_at"]
            if not last_checkin_str:
                last_db = connection.execute(
                    "SELECT created_at FROM checkins WHERE owner_id=? AND status='success' ORDER BY id DESC LIMIT 1",
                    (uid,),
                ).fetchone()
                last_checkin_str = last_db["created_at"] if last_db else user["created_at"]

            last_checkin_dt = safe_fromisoformat(last_checkin_str)
            checkin_deadline = last_checkin_dt + timedelta(seconds=freq_sec)
            checkin_deadline_iso = checkin_deadline.isoformat()

            # Synchronize next_checkin_deadline
            if user["next_checkin_deadline"] != checkin_deadline_iso:
                connection.execute("UPDATE users SET next_checkin_deadline=? WHERE id=?", (checkin_deadline_iso, uid))

            # Synchronize state with active nominee case if any
            active_case = connection.execute(
                "SELECT status FROM death_cases WHERE owner_id=?", (uid,)
            ).fetchone()
            if active_case:
                c_status = active_case["status"]
                target_state = {
                    "under_review": "LEGACY_REVIEW",
                    "approved": "LEGACY_APPROVED",
                    "rejected": "LEGACY_REJECTED",
                    "more_information_required": "MORE_INFORMATION_REQUIRED",
                }.get(c_status)
                if target_state and state != target_state and state not in ("LEGACY_APPROVED", "LEGACY_REJECTED"):
                    connection.execute("UPDATE users SET account_state=? WHERE id=?", (target_state, uid))
                    continue

            # State Machine:
            # 1. ACTIVE -> GRACE_PERIOD
            if state == "ACTIVE":
                if now > checkin_deadline:
                    grace_start = now
                    grace_deadline = grace_start + timedelta(seconds=grace_sec)
                    grace_start_iso = grace_start.isoformat()
                    grace_deadline_iso = grace_deadline.isoformat()
                    
                    connection.execute(
                        """UPDATE users SET account_state='GRACE_PERIOD',
                                  grace_period_started_at=?, grace_period_deadline=?
                           WHERE id=?""",
                        (grace_start_iso, grace_deadline_iso, uid),
                    )
                    audit(connection, uid, "CHECKIN_MISSED")
                    audit(connection, uid, "GRACE_PERIOD_STARTED")
                    
                    nominee_row = connection.execute("SELECT id, name, email FROM nominees WHERE owner_id=?", (uid,)).fetchone()

                    # Send Owner Missed Check-in Email (Idempotent check)
                    dup_owner = connection.execute(
                        """SELECT 1 FROM notifications
                           WHERE owner_id=? AND notification_type='missed_checkin_owner'
                             AND state_transition='ACTIVE->GRACE_PERIOD' AND status='sent'
                             AND sent_at >= ?""",
                        (uid, checkin_deadline_iso),
                    ).fetchone()
                    
                    if not dup_owner and user["email"]:
                        owner_subject = "CloudGuard: Your check-in deadline was missed"
                        owner_text = (
                            f"Hello {user['name']},\n\n"
                            f"Your scheduled CloudGuard live check-in deadline was missed on {checkin_deadline_iso}.\n\n"
                            f"Your account has entered the Grace Period, which remains active until {grace_deadline_iso}.\n\n"
                            f"You can still complete your live check-in at any time before the grace deadline to keep your account fully active:\n"
                            f"{app_url}\n\n"
                            f"IMPORTANT: Missing a check-in does NOT mean CloudGuard has determined that you have died.\n"
                            f"CloudGuard never automatically assumes death from a missed check-in."
                        )
                        ok, err_detail = dispatch_email(user["email"], owner_subject, owner_text)
                        status_str = "sent" if ok else "failed"
                        connection.execute(
                            """INSERT INTO notifications(owner_id, nominee_id, notification_type, state_transition, recipient_email, sent_at, status, error_message)
                               VALUES(?,NULL,'missed_checkin_owner','ACTIVE->GRACE_PERIOD',?,?,?,?)""",
                            (uid, user["email"], now_iso, status_str, "" if ok else err_detail),
                        )
                        if ok:
                            audit(connection, uid, "MISSED_CHECKIN_OWNER_NOTIFICATION_SENT")
                        else:
                            audit(connection, uid, f"EMAIL_NOTIFICATION_FAILED:missed_checkin_owner:{err_detail}")

                    # Send Nominee Missed Check-in Notice (Idempotent check)
                    if nominee_row and nominee_row["email"]:
                        dup_nominee = connection.execute(
                            """SELECT 1 FROM notifications
                               WHERE owner_id=? AND notification_type='missed_checkin_nominee'
                                 AND state_transition='ACTIVE->GRACE_PERIOD' AND status='sent'
                                 AND sent_at >= ?""",
                            (uid, checkin_deadline_iso),
                        ).fetchone()
                        
                        if not dup_nominee:
                            nominee_subject = "CloudGuard: Owner check-in was missed"
                            nominee_text = (
                                f"Hello {nominee_row['name']},\n\n"
                                f"This is an automated status notice from CloudGuard regarding {user['name']}.\n\n"
                                f"The vault owner did not complete their scheduled live check-in on time, and their account has entered a Grace Period ending at {grace_deadline_iso}.\n\n"
                                f"During this grace period, the owner can still perform a check-in to confirm activity.\n\n"
                                f"No death determination has been made, and NO immediate action is required from you at this time."
                            )
                            ok, err_detail = dispatch_email(nominee_row["email"], nominee_subject, nominee_text)
                            status_str = "sent" if ok else "failed"
                            connection.execute(
                                """INSERT INTO notifications(owner_id, nominee_id, notification_type, state_transition, recipient_email, sent_at, status, error_message)
                                   VALUES(?,?, 'missed_checkin_nominee','ACTIVE->GRACE_PERIOD',?,?,?,?)""",
                                (uid, nominee_row["id"], nominee_row["email"], now_iso, status_str, "" if ok else err_detail),
                            )
                            if ok:
                                audit(connection, uid, "MISSED_CHECKIN_NOMINEE_NOTIFICATION_SENT")
                            else:
                                audit(connection, uid, f"EMAIL_NOTIFICATION_FAILED:missed_checkin_nominee:{err_detail}")

            # 2. GRACE_PERIOD -> NOMINEE_CASE_ELIGIBLE
            elif state == "GRACE_PERIOD":
                grace_deadline_str = user["grace_period_deadline"]
                if grace_deadline_str:
                    grace_deadline = safe_fromisoformat(grace_deadline_str)
                else:
                    grace_start_str = user["grace_period_started_at"] or now_iso
                    grace_deadline = safe_fromisoformat(grace_start_str) + timedelta(seconds=grace_sec)

                if now > grace_deadline:
                    connection.execute(
                        "UPDATE users SET account_state='NOMINEE_CASE_ELIGIBLE' WHERE id=?",
                        (uid,),
                    )
                    audit(connection, uid, "GRACE_PERIOD_EXPIRED")
                    
                    nominee_row = connection.execute("SELECT id, name, email FROM nominees WHERE owner_id=?", (uid,)).fetchone()

                    # Send Owner Grace Expiration Notice (Idempotent check)
                    dup_owner_exp = connection.execute(
                        """SELECT 1 FROM notifications
                           WHERE owner_id=? AND notification_type='grace_expired_owner'
                             AND state_transition='GRACE_PERIOD->NOMINEE_CASE_ELIGIBLE' AND status='sent'
                             AND sent_at >= ?""",
                        (uid, grace_deadline.isoformat()),
                    ).fetchone()
                    
                    if not dup_owner_exp and user["email"]:
                        owner_subject = "CloudGuard: Grace period expired"
                        owner_text = (
                            f"Hello {user['name']},\n\n"
                            f"Your check-in deadline was missed and your grace period has now expired.\n\n"
                            f"The nominee verification workflow is now eligible for your account.\n\n"
                            f"Please note: Missing a check-in does not automatically prove death. You can perform a verified live biometric check-in at any time to immediately close any pending nominee case and restore your account to ACTIVE:\n"
                            f"{app_url}"
                        )
                        ok, err_detail = dispatch_email(user["email"], owner_subject, owner_text)
                        status_str = "sent" if ok else "failed"
                        connection.execute(
                            """INSERT INTO notifications(owner_id, nominee_id, notification_type, state_transition, recipient_email, sent_at, status, error_message)
                               VALUES(?,NULL,'grace_expired_owner','GRACE_PERIOD->NOMINEE_CASE_ELIGIBLE',?,?,?,?)""",
                            (uid, user["email"], now_iso, status_str, "" if ok else err_detail),
                        )
                        if ok:
                            audit(connection, uid, "GRACE_EXPIRATION_OWNER_NOTIFICATION_SENT")
                        else:
                            audit(connection, uid, f"EMAIL_NOTIFICATION_FAILED:grace_expired_owner:{err_detail}")

                    # Send Nominee Verification Available Notice (Idempotent check)
                    if nominee_row and nominee_row["email"]:
                        dup_nominee_exp = connection.execute(
                            """SELECT 1 FROM notifications
                               WHERE owner_id=? AND notification_type='grace_expired_nominee'
                                 AND state_transition='GRACE_PERIOD->NOMINEE_CASE_ELIGIBLE' AND status='sent'
                                 AND sent_at >= ?""",
                            (uid, grace_deadline.isoformat()),
                        ).fetchone()
                        
                        if not dup_nominee_exp:
                            nominee_subject = "CloudGuard: Nominee verification is now available"
                            nominee_text = (
                                f"Hello {nominee_row['name']},\n\n"
                                f"The scheduled check-in and grace period for {user['name']} have expired without verification.\n\n"
                                f"The nominee verification workflow is now eligible. You may now access your nominee portal to submit identity evidence (such as a death certificate) relating to the deceased owner for human reviewer adjudication:\n"
                                f"{app_url}\n\n"
                                f"Please note: The owner is not confirmed dead by this notice. All submitted evidence will be independently analyzed and adjudicated by an authorized human reviewer before any document release can be considered."
                            )
                            ok, err_detail = dispatch_email(nominee_row["email"], nominee_subject, nominee_text)
                            status_str = "sent" if ok else "failed"
                            connection.execute(
                                """INSERT INTO notifications(owner_id, nominee_id, notification_type, state_transition, recipient_email, sent_at, status, error_message)
                                   VALUES(?,?, 'grace_expired_nominee','GRACE_PERIOD->NOMINEE_CASE_ELIGIBLE',?,?,?,?)""",
                                (uid, nominee_row["id"], nominee_row["email"], now_iso, status_str, "" if ok else err_detail),
                            )
                            if ok:
                                audit(connection, uid, "GRACE_EXPIRATION_NOMINEE_NOTIFICATION_SENT")
                            else:
                                audit(connection, uid, f"EMAIL_NOTIFICATION_FAILED:grace_expired_nominee:{err_detail}")


async def run_background_scheduler() -> None:
    logger.info("CloudGuard Background Check-in Scheduler running.")
    # Pause 5s on boot to guarantee uvicorn binds to port $PORT and completes startup immediately
    await asyncio.sleep(5)
    while True:
        try:
            await asyncio.to_thread(evaluate_owner_account_states)
        except asyncio.CancelledError:
            break
        except Exception as error:
            logger.error(f"Scheduler evaluation exception: {error}")
        await asyncio.sleep(15)


def _medical_hold_state(connection: Any, owner_id: int) -> dict:
    row = connection.execute(
        "SELECT status,started_at,ended_at,updated_at FROM medical_holds WHERE owner_id=?",
        (owner_id,),
    ).fetchone()
    return dict(row) if row else {
        "status": "inactive",
        "started_at": None,
        "ended_at": None,
        "updated_at": None,
    }


def _set_document_release(
    connection: Any, document_id: int, recipient_id: int, status: str
) -> None:
    now = utc_now().isoformat()
    existing = connection.execute(
        "SELECT status FROM document_releases WHERE document_id=? AND recipient_id=?",
        (document_id, recipient_id),
    ).fetchone()
    if existing:
        if status == "available":
            connection.execute(
                """UPDATE document_releases SET status='available', released_at=?, revoked_at=NULL
                   WHERE document_id=? AND recipient_id=?""",
                (now, document_id, recipient_id),
            )
        else:
            connection.execute(
                """UPDATE document_releases SET status='revoked', revoked_at=?
                   WHERE document_id=? AND recipient_id=?""",
                (now, document_id, recipient_id),
            )
        return
    connection.execute(
        """INSERT INTO document_releases(document_id,recipient_id,status,released_at,revoked_at)
           VALUES(?,?,?,?,?)""",
        (
            document_id,
            recipient_id,
            status,
            now if status == "available" else None,
            now if status == "revoked" else None,
        ),
    )


def _replace_document_shares(
    connection: Any,
    owner_id: int,
    document_id: int,
    recipient_ids: list[int],
) -> None:
    current = {
        row["recipient_id"]
        for row in connection.execute(
            "SELECT recipient_id FROM document_shares WHERE document_id=?",
            (document_id,),
        )
    }
    new_ids = list(dict.fromkeys(recipient_ids))
    new_set = set(new_ids)
    for recipient_id in current - new_set:
        _set_document_release(connection, document_id, recipient_id, "revoked")
        _log_recipient_event(
            connection,
            recipient_id,
            owner_id,
            "access_revoked",
            document_id,
            "The owner removed this document assignment.",
        )
    connection.execute("DELETE FROM document_shares WHERE document_id=?", (document_id,))
    now = utc_now().isoformat()
    for recipient_id in new_ids:
        connection.execute(
            "INSERT INTO document_shares(document_id,recipient_id,created_at) VALUES(?,?,?)",
            (document_id, recipient_id, now),
        )
        if recipient_id not in current:
            connection.execute(
                "DELETE FROM document_releases WHERE document_id=? AND recipient_id=?",
                (document_id, recipient_id),
            )
            _log_recipient_event(
                connection,
                recipient_id,
                owner_id,
                "assignment_pending",
                document_id,
                "A document was assigned. Access stays locked until the owner approves release.",
            )


def _recipient_documents(connection: Any, recipient_id: int) -> list[dict]:
    owner = connection.execute(
        "SELECT owner_id FROM recipients WHERE id=?", (recipient_id,)
    ).fetchone()
    if owner is None:
        return []
    owner_id = owner["owner_id"]
    hold = _medical_hold_state(connection, owner_id)
    has_approved_case = connection.execute(
        "SELECT 1 FROM death_cases WHERE owner_id=? AND status='approved'",
        (owner_id,),
    ).fetchone() is not None
    shares = connection.execute(
        """SELECT documents.id, documents.name, documents.category, documents.file_name,
                  documents.content_type, documents.file_size, documents.created_at,
                  document_shares.created_at AS assigned_at
           FROM document_shares
           JOIN documents ON documents.id = document_shares.document_id
           WHERE document_shares.recipient_id=?""",
        (recipient_id,),
    ).fetchall()
    releases = {
        row["document_id"]: dict(row)
        for row in connection.execute(
            """SELECT document_id,status,released_at,revoked_at
               FROM document_releases WHERE recipient_id=?""",
            (recipient_id,),
        )
    }
    documents = {}
    for row in shares:
        release = releases.get(row["id"])
        if release and release["status"] == "revoked":
            access_status = "revoked"
        elif (
            release
            and release["status"] == "available"
            and hold["status"] == "inactive"
            and has_approved_case
        ):
            access_status = "available"
        else:
            access_status = "pending"
        documents[row["id"]] = {
            **dict(row),
            "access_status": access_status,
            "released_at": release["released_at"] if release else None,
            "revoked_at": release["revoked_at"] if release else None,
            "release_hold": hold["status"] != "inactive",
            "case_approved": has_approved_case,
            "decryption_key": FILE_KEY_B64 if access_status == "available" else None,
            "encryption_algorithm": "AES-256-GCM",
        }
    for document_id, release in releases.items():
        if document_id in documents or release["status"] != "revoked":
            continue
        document = connection.execute(
            """SELECT id,name,category,file_name,content_type,file_size,created_at
               FROM documents WHERE id=?""",
            (document_id,),
        ).fetchone()
        if document is None:
            continue
        documents[document_id] = {
            **dict(document),
            "assigned_at": None,
            "access_status": "revoked",
            "released_at": release["released_at"],
            "revoked_at": release["revoked_at"],
            "release_hold": hold["status"] != "inactive",
            "case_approved": has_approved_case,
            "decryption_key": None,
            "encryption_algorithm": "AES-256-GCM",
        }
    return sorted(documents.values(), key=lambda item: item["id"], reverse=True)


def _issue_recipient_session(recipient_id: int, email: str) -> tuple[str, datetime]:
    now = utc_now()
    expires_at = now + timedelta(hours=12)
    session_id = secrets.token_urlsafe(24)
    with db() as connection:
        connection.execute(
            "INSERT INTO recipient_sessions(id,recipient_id,expires_at) VALUES(?,?,?)",
            (session_id, recipient_id, expires_at.isoformat()),
        )
    token = jwt.encode(
        {
            "sub": email,
            "jti": session_id,
            "iat": now,
            "exp": expires_at,
            "principal_type": "recipient",
            "recipient_id": recipient_id,
        },
        JWT_SECRET,
        algorithm="HS256",
    )
    return token, expires_at


def _issue_nominee_session(nominee_id: int, email: str) -> tuple[str, datetime]:
    now = utc_now()
    expires_at = now + timedelta(hours=12)
    session_id = secrets.token_urlsafe(24)
    with db() as connection:
        connection.execute(
            "INSERT INTO nominee_sessions(id,nominee_id,expires_at) VALUES(?,?,?)",
            (session_id, nominee_id, expires_at.isoformat()),
        )
    token = jwt.encode(
        {
            "sub": email,
            "jti": session_id,
            "iat": now,
            "exp": expires_at,
            "principal_type": "nominee",
            "nominee_id": nominee_id,
        },
        JWT_SECRET,
        algorithm="HS256",
    )
    return token, expires_at


def _issue_reviewer_session(email: str) -> tuple[str, datetime]:
    now = utc_now()
    expires_at = now + timedelta(hours=8)
    session_id = secrets.token_urlsafe(24)
    with db() as connection:
        connection.execute(
            "INSERT INTO reviewer_sessions(id,email,expires_at) VALUES(?,?,?)",
            (session_id, email, expires_at.isoformat()),
        )
    token = jwt.encode(
        {
            "sub": email,
            "jti": session_id,
            "iat": now,
            "exp": expires_at,
            "principal_type": "reviewer",
        },
        JWT_SECRET,
        algorithm="HS256",
    )
    return token, expires_at


def recipient_principal(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> dict:
    if not credentials:
        raise HTTPException(status_code=401, detail="A valid recipient sign-in is required.")
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=["HS256"])
        if payload.get("principal_type") != "recipient":
            raise HTTPException(status_code=401, detail="A valid recipient sign-in is required.")
        session_id = payload["jti"]
        recipient_id = int(payload["recipient_id"])
        email = str(payload["sub"]).strip().lower()
    except HTTPException:
        raise
    except (jwt.InvalidTokenError, KeyError, TypeError, ValueError) as error:
        raise HTTPException(status_code=401, detail="The recipient sign-in has expired.") from error

    with db() as connection:
        row = connection.execute(
            """SELECT recipient_sessions.id, recipient_sessions.expires_at, recipient_sessions.revoked_at,
                      recipients.id AS recipient_id, recipients.name, recipients.email, recipients.owner_id,
                      users.name AS owner_name
               FROM recipient_sessions
               JOIN recipients ON recipients.id = recipient_sessions.recipient_id
               JOIN users ON users.id = recipients.owner_id
               WHERE recipient_sessions.id=? AND recipients.id=?""",
            (session_id, recipient_id),
        ).fetchone()
        if (
            row is None
            or row["revoked_at"] is not None
            or row["email"].strip().lower() != email
            or safe_fromisoformat(row["expires_at"]) <= utc_now()
        ):
            raise HTTPException(status_code=401, detail="The recipient sign-in is no longer active.")
        return {
            "email": email,
            "name": row["name"],
            "role": "recipient",
            "owner_id": row["owner_id"],
            "owner_name": row["owner_name"],
            "recipient_id": row["recipient_id"],
            "session_id": row["id"],
        }


def nominee_principal(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> dict:
    if not credentials:
        raise HTTPException(status_code=401, detail="A valid nominee sign-in is required.")
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=["HS256"])
        if payload.get("principal_type") != "nominee":
            raise HTTPException(status_code=401, detail="A valid nominee sign-in is required.")
        session_id = payload["jti"]
        nominee_id = int(payload["nominee_id"])
        email = str(payload["sub"]).strip().lower()
    except HTTPException:
        raise
    except (jwt.InvalidTokenError, KeyError, TypeError, ValueError) as error:
        raise HTTPException(status_code=401, detail="The nominee sign-in has expired.") from error

    with db() as connection:
        row = connection.execute(
            """SELECT nominee_sessions.id,nominee_sessions.expires_at,nominee_sessions.revoked_at,
                      nominees.id AS nominee_id,nominees.name,nominees.email,nominees.owner_id,
                      users.name AS owner_name
               FROM nominee_sessions
               JOIN nominees ON nominees.id=nominee_sessions.nominee_id
               JOIN users ON users.id=nominees.owner_id
               WHERE nominee_sessions.id=? AND nominees.id=?""",
            (session_id, nominee_id),
        ).fetchone()
        if (
            row is None
            or row["revoked_at"] is not None
            or row["email"].strip().lower() != email
            or safe_fromisoformat(row["expires_at"]) <= utc_now()
        ):
            raise HTTPException(status_code=401, detail="The nominee sign-in is no longer active.")
        return {
            "email": email,
            "name": row["name"],
            "role": "nominee",
            "owner_id": row["owner_id"],
            "owner_name": row["owner_name"],
            "nominee_id": row["nominee_id"],
            "session_id": row["id"],
        }


def reviewer_principal(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> dict:
    if not credentials:
        raise HTTPException(status_code=401, detail="A valid reviewer sign-in is required.")
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=["HS256"])
        if payload.get("principal_type") != "reviewer":
            raise HTTPException(status_code=401, detail="A valid reviewer sign-in is required.")
        session_id = payload["jti"]
        email = str(payload["sub"]).strip().lower()
    except HTTPException:
        raise
    except (jwt.InvalidTokenError, KeyError, TypeError, ValueError) as error:
        raise HTTPException(status_code=401, detail="The reviewer sign-in has expired.") from error
    with db() as connection:
        row = connection.execute(
            "SELECT id,expires_at,revoked_at,email FROM reviewer_sessions WHERE id=?",
            (session_id,),
        ).fetchone()
    if (
        row is None
        or row["revoked_at"] is not None
        or row["email"].strip().lower() != email
        or email != REVIEWER_EMAIL
        or safe_fromisoformat(row["expires_at"]) <= utc_now()
    ):
        raise HTTPException(status_code=401, detail="The reviewer sign-in is no longer active.")
    return {"email": email, "session_id": row["id"], "role": "reviewer"}


def guest_principal(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> dict:
    if not credentials:
        raise HTTPException(status_code=401, detail="A valid invitation sign-in is required.")
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=["HS256"])
        if payload.get("principal_type") != "guest":
            raise HTTPException(status_code=401, detail="A valid invitation sign-in is required.")
        session_id = payload["jti"]
        invitation_id = int(payload["invitation_id"])
        email = str(payload["sub"]).strip().lower()
    except HTTPException:
        raise
    except (jwt.InvalidTokenError, KeyError, TypeError, ValueError) as error:
        raise HTTPException(status_code=401, detail="The invitation sign-in has expired.") from error

    with db() as connection:
        row = connection.execute(
            """SELECT guest_sessions.id,guest_sessions.expires_at AS session_expires_at,
                      guest_sessions.revoked_at AS session_revoked_at,
                      invitations.id AS invitation_id,invitations.owner_id,
                      invitations.email,invitations.role,invitations.target_id,
                      invitations.status,invitations.revoked_at AS invitation_revoked_at,
                      users.name AS owner_name
               FROM guest_sessions
               JOIN invitations ON invitations.id=guest_sessions.invitation_id
               JOIN users ON users.id=invitations.owner_id
               WHERE guest_sessions.id=? AND invitations.id=?""",
            (session_id, invitation_id),
        ).fetchone()
        if (
            row is None
            or row["session_revoked_at"] is not None
            or row["invitation_revoked_at"] is not None
            or row["status"] != "claimed"
            or row["email"].strip().lower() != email
            or safe_fromisoformat(row["session_expires_at"]) <= utc_now()
        ):
            raise HTTPException(status_code=401, detail="The invitation sign-in is no longer active.")
        try:
            target = invitation_target(connection, row["owner_id"], row["role"], row["target_id"])
        except HTTPException as error:
            raise HTTPException(status_code=401, detail="The invitation sign-in is no longer active.") from error
        if target["email"].strip().lower() != email:
            raise HTTPException(status_code=401, detail="The invitation sign-in is no longer active.")
        return {
            "email": email,
            "role": row["role"],
            "owner_id": row["owner_id"],
            "owner_name": row["owner_name"],
            "target_id": row["target_id"],
            "invitation_id": row["invitation_id"],
            "session_id": row["id"],
        }


def issue_token(user_id: int) -> str:
    now = utc_now()
    expires_at = now + timedelta(hours=12)
    session_id = secrets.token_urlsafe(24)
    with db() as connection:
        connection.execute(
            "INSERT INTO sessions(id,user_id,expires_at) VALUES(?,?,?)",
            (session_id, user_id, expires_at.isoformat()),
        )
    return jwt.encode(
        {
            "sub": str(user_id),
            "jti": session_id,
            "iat": now,
            "exp": expires_at,
            "principal_type": "owner",
        },
        JWT_SECRET,
        algorithm="HS256",
    )


def current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> dict:
    if not credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Please sign in to continue.")
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=["HS256"])
        if payload.get("principal_type", "owner") != "owner":
            raise HTTPException(status_code=401, detail="Owner sign-in is required.")
        user_id = int(payload["sub"])
        session_id = payload["jti"]
    except HTTPException:
        raise
    except (jwt.InvalidTokenError, KeyError, ValueError) as error:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Your session has expired. Please sign in again.") from error
    with db() as connection:
        user = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        session = connection.execute(
            "SELECT id FROM sessions WHERE id=? AND user_id=? AND revoked_at IS NULL",
            (session_id, user_id),
        ).fetchone()
    if user is None or session is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Your session is no longer active. Please sign in again.")
    return user


@app.get("/")
@app.head("/")
@app.get("/api")
@app.get("/api/")
def root_status():
    return {
        "status": "ok",
        "service": "CloudGuard Vault API",
        "version": "1.0.0",
        "database": "postgresql",
        "docs": "/docs",
        "health": "/api/health",
    }


@app.get("/health")
@app.get("/api/health")
def health() -> dict:
    configured = providers_available()
    diag = verification_diagnostics()
    db_connected = False
    try:
        with db() as conn:
            conn.execute("SELECT 1").fetchone()
            db_connected = True
    except Exception:
        db_connected = False

    return {
        "status": "ok" if db_connected else "degraded",
        "service": "CloudGuard Vault API",
        "version": "1.0.0",
        "database": "postgresql",
        "database_connected": db_connected,
        "verification_mode": "local" if configured else "unavailable",
        "ai_verification_configured": configured,
        "verification": {
            "face": diag["face"],
            "liveness": diag["liveness"],
            "speech": diag["speech"],
            "video_decode": diag["video_decode"],
            **({"diagnostics": diag.get("errors")} if diag.get("errors") else {}),
        },
    }


def send_invitation_email(email: str, role: str, token: str) -> str:
    config, missing = invitation_email_configuration()
    if missing:
        raise RuntimeError(
            f"Invitation email configuration is missing: {', '.join(missing)}."
        )
    api_key = config["RESEND_API_KEY"]
    sender = config["RESEND_FROM_EMAIL"]
    app_url = config["CLOUDGUARD_APP_URL"].rstrip("/")
    parsed_url = urlsplit(app_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc or parsed_url.query or parsed_url.fragment:
        raise RuntimeError("Invitation app URL configuration is invalid.")

    resend.api_key = api_key
    claim_url = f"{app_url}/#guest-invitation={token}"
    role_label = "Trusted Nominee" if role == "nominee" else "Authorized Recipient"
    response = resend.Emails.send(
        {
            "from": sender,
            "to": [email],
            "subject": "You have been invited to CloudGuard",
            "text": (
                f"You have been invited to CloudGuard as a {role_label}.\n\n"
                "Open this secure, single-use link within 7 days to verify this email address"
                + (
                    f" and create a password for your {role} account:\n"
                    if role in {"recipient", "nominee"}
                    else ":\n"
                )
                + f"{claim_url}\n\n"
                "Accepting this invitation does not grant access to documents. "
                "Assigned files stay locked until the owner completes and approves release. "
                "Missed owner check-ins never release documents automatically."
            ),
        }
    )
    provider_id = response.get("id") if isinstance(response, dict) else None
    if not isinstance(provider_id, str) or not provider_id:
        raise RuntimeError("Resend did not confirm acceptance of the invitation email.")
    return provider_id


def invitation_email_configuration() -> tuple[dict[str, str], list[str]]:
    file_values = dotenv_values(BASE_DIR / ".env")
    config = {}
    missing = []
    for name in ("RESEND_API_KEY", "RESEND_FROM_EMAIL", "CLOUDGUARD_APP_URL"):
        value = os.getenv(name)
        if not value or not value.strip():
            value = file_values.get(name)
        if not isinstance(value, str) or not value.strip():
            missing.append(name)
        else:
            config[name] = value.strip()
    return config, missing


def sanitized_resend_error(error: ResendError, secrets_to_redact: list[str]) -> str:
    message = str(getattr(error, "message", "")) or "No provider message was supplied."
    for secret in secrets_to_redact:
        if secret:
            message = message.replace(secret, "[redacted]")
    message = re.sub(
        r"(?i)authorization\s*[:=]\s*(?:Bearer\s+)?\S+",
        "Authorization: [redacted]",
        message,
    )
    message = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [redacted]", message)
    message = re.sub(r"(?i)\b(?:re|rk|sk)_[A-Za-z0-9_-]+\b", "[redacted]", message)
    message = re.sub(
        r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
        "[email redacted]",
        message,
        flags=re.IGNORECASE,
    )
    message = " ".join(message.split())[:300]
    error_type = re.sub(
        r"[^A-Za-z0-9_.-]", "", str(getattr(error, "error_type", "ResendError"))
    )[:80] or "ResendError"
    status_code = re.sub(r"[^0-9]", "", str(getattr(error, "code", "")))[:3]
    status_text = f"HTTP {status_code}" if status_code else "status unavailable"
    return f"Resend rejected the invitation ({status_text}, {error_type}): {message}"


@app.post("/api/invitations", status_code=201)
def create_invitation(
    payload: CreateInvitationPayload,
    user: dict = Depends(current_user),
) -> dict:
    """Create an invitation. Email (Resend) is optional.

    Always returns a claim_url the owner can copy and share manually
    (WhatsApp, SMS, etc.). If Resend is configured it is attempted, but
    failure no longer blocks the invitation.
    """
    invitation_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(invitation_token.encode("ascii")).hexdigest()
    now = utc_now()
    expires_at = now + timedelta(days=7)

    config, missing = invitation_email_configuration()
    app_url = (config.get("CLOUDGUARD_APP_URL") or "http://localhost:5173").rstrip("/")
    claim_url = f"{app_url}/#guest-invitation={invitation_token}"

    with db() as connection:
        target = invitation_target(connection, user["id"], payload.role, payload.target_id)
        email = target["email"].strip().lower()
        revoke_target_invitations(connection, user["id"], payload.role, payload.target_id)
        cursor = connection.execute(
            """INSERT INTO invitations(owner_id,email,role,target_id,token_hash,status,created_at,expires_at)
               VALUES(?,?,?,?,?,'pending',?,?)""",
            (
                user["id"],
                email,
                payload.role,
                payload.target_id,
                token_hash,
                now.isoformat(),
                expires_at.isoformat(),
            ),
        )
        invitation_id = cursor.lastrowid
        audit(connection, user["id"], f"invitation.created:{invitation_id}:{payload.role}")

    email_delivery = "skipped"
    email_note = "Share the claim link below with the invitee. No email provider is required."

    if not missing:
        try:
            send_invitation_email(email, payload.role, invitation_token)
            email_delivery = "sent"
            email_note = "Invitation email was also sent via Resend."
        except Exception as error:
            email_delivery = "failed"
            if isinstance(error, ResendError):
                safe_detail = sanitized_resend_error(
                    error,
                    [
                        invitation_token,
                        email,
                        config.get("RESEND_API_KEY", ""),
                    ],
                )
                email_note = (
                    f"Email could not be sent ({safe_detail}). "
                    "Use the claim link below instead."
                )
                logger.warning("Invitation %s email failed: %s", invitation_id, safe_detail)
            else:
                email_note = (
                    f"Email could not be sent ({type(error).__name__}). "
                    "Use the claim link below instead."
                )
                logger.warning(
                    "Invitation %s email failed (%s).", invitation_id, type(error).__name__
                )
    else:
        email_note = (
            "Email is not configured (optional). "
            "Copy the claim link and share it with the invitee."
        )

    with db() as connection:
        cursor = connection.execute(
            """UPDATE invitations SET status='sent',sent_at=?
               WHERE id=? AND status='pending' AND expires_at>?""",
            (utc_now().isoformat(), invitation_id, utc_now().isoformat()),
        )
        if cursor.rowcount != 1:
            raise HTTPException(
                status_code=503,
                detail="The invitation could not be activated. Please try again.",
            )
        audit(
            connection,
            user["id"],
            f"invitation.sent:{invitation_id}:{payload.role}:{email_delivery}",
        )

    return {
        "invitation": {
            "id": invitation_id,
            "email": email,
            "role": payload.role,
            "status": "sent",
            "expires_at": expires_at.isoformat(),
            "claim_url": claim_url,
            "email_delivery": email_delivery,
            "email_note": email_note,
        },
        "claim_url": claim_url,
        "message": (
            "Invitation is ready. Copy the claim link and send it to the person "
            "(email is optional and was not required)."
        ),
    }

@app.get("/api/invitations")
def list_invitations(user: dict = Depends(current_user)) -> dict:
    with db() as connection:
        rows = connection.execute(
            """SELECT id,email,role,target_id,status,created_at,expires_at,sent_at,claimed_at,revoked_at
               FROM invitations WHERE owner_id=? ORDER BY id DESC LIMIT 100""",
            (user["id"],),
        ).fetchall()
    invitations = []
    for row in rows:
        item = dict(row)
        if item["status"] == "sent" and safe_fromisoformat(item["expires_at"]) <= utc_now():
            item["status"] = "expired"
        invitations.append(item)
    return {"invitations": invitations}


@app.delete("/api/invitations/{invitation_id}", status_code=204)
def revoke_invitation(invitation_id: int, user: dict = Depends(current_user)) -> None:
    now = utc_now().isoformat()
    with db() as connection:
        invitation = connection.execute(
            "SELECT id,role,target_id FROM invitations WHERE id=? AND owner_id=?",
            (invitation_id, user["id"]),
        ).fetchone()
        if invitation is None:
            raise HTTPException(status_code=404, detail="Invitation was not found.")
        connection.execute(
            """UPDATE invitations SET status='revoked',revoked_at=?
               WHERE id=? AND status IN ('pending','sent','claimed')""",
            (now, invitation_id),
        )
        connection.execute(
            "UPDATE guest_sessions SET revoked_at=? WHERE invitation_id=? AND revoked_at IS NULL",
            (now, invitation_id),
        )
        if invitation["role"] == "recipient":
            connection.execute(
                "UPDATE recipient_sessions SET revoked_at=? WHERE recipient_id=? AND revoked_at IS NULL",
                (now, invitation["target_id"]),
            )
        elif invitation["role"] == "nominee":
            connection.execute(
                "UPDATE nominee_sessions SET revoked_at=? WHERE nominee_id=? AND revoked_at IS NULL",
                (now, invitation["target_id"]),
            )
        audit(connection, user["id"], f"invitation.revoked:{invitation_id}:{invitation['role']}")


@app.get("/api/guest/invitations/preview")
def preview_invitation(token: str) -> dict:
    if not token or len(token) < 43:
        raise HTTPException(status_code=400, detail="This invitation is invalid, expired, or already used.")
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = utc_now()
    with db() as connection:
        invitation = connection.execute(
            """SELECT owner_id,email,role,target_id,expires_at,status,revoked_at
               FROM invitations WHERE token_hash=?""",
            (token_hash,),
        ).fetchone()
        if (
            invitation is None
            or invitation["status"] != "sent"
            or invitation["revoked_at"] is not None
            or safe_fromisoformat(invitation["expires_at"]) <= now
        ):
            raise HTTPException(status_code=400, detail="This invitation is invalid, expired, or already used.")
        try:
            invitation_target(connection, invitation["owner_id"], invitation["role"], invitation["target_id"])
        except HTTPException as error:
            raise HTTPException(status_code=400, detail="This invitation is no longer valid.") from error
        owner = connection.execute("SELECT name FROM users WHERE id=?", (invitation["owner_id"],)).fetchone()
    email = invitation["email"].strip().lower()
    local, _, domain = email.partition("@")
    hint = f"{local[:1]}***@{domain}" if domain else "***"
    return {
        "role": invitation["role"],
        "owner_name": owner["name"] if owner else "CloudGuard owner",
        "email_hint": hint,
        "requires_password": invitation["role"] in {"recipient", "nominee"},
    }


@app.post("/api/guest/invitations/claim")
def claim_invitation(payload: ClaimInvitationPayload) -> dict:
    token_hash = hashlib.sha256(payload.token.encode("utf-8")).hexdigest()
    now = utc_now()
    session_id = secrets.token_urlsafe(24)
    with db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        invitation = connection.execute(
            """SELECT id,owner_id,email,role,target_id,expires_at,status,revoked_at
               FROM invitations WHERE token_hash=?""",
            (token_hash,),
        ).fetchone()
        if (
            invitation is None
            or invitation["status"] != "sent"
            or invitation["revoked_at"] is not None
            or safe_fromisoformat(invitation["expires_at"]) <= now
        ):
            raise HTTPException(status_code=400, detail="This invitation is invalid, expired, or already used.")
        try:
            target = invitation_target(
                connection,
                invitation["owner_id"],
                invitation["role"],
                invitation["target_id"],
            )
        except HTTPException as error:
            raise HTTPException(status_code=400, detail="This invitation is no longer valid.") from error
        email = invitation["email"].strip().lower()
        if target["email"].strip().lower() != email:
            raise HTTPException(status_code=400, detail="This invitation is no longer valid.")
        if invitation["role"] in {"recipient", "nominee"} and not payload.password:
            raise HTTPException(
                status_code=422,
                detail=f"Create a password of at least 10 characters to finish setting up your {invitation['role']} account.",
            )

        session_expires_at = min(now + timedelta(hours=12), safe_fromisoformat(invitation["expires_at"]))
        updated = connection.execute(
            """UPDATE invitations SET status='claimed',claimed_at=?
               WHERE id=? AND status='sent' AND revoked_at IS NULL""",
            (now.isoformat(), invitation["id"]),
        )
        if updated.rowcount != 1:
            raise HTTPException(status_code=400, detail="This invitation is invalid, expired, or already used.")
        owner = connection.execute(
            "SELECT name FROM users WHERE id=?", (invitation["owner_id"],)
        ).fetchone()
        audit(connection, invitation["owner_id"], f"invitation.claimed:{invitation['id']}:{invitation['role']}")

        if invitation["role"] == "recipient":
            password_hash = _password_hash(payload.password)
            connection.execute(
                """INSERT INTO recipient_accounts(recipient_id,password_hash,created_at)
                   VALUES(?,?,?)
                   ON CONFLICT(recipient_id) DO UPDATE SET password_hash=excluded.password_hash""",
                (invitation["target_id"], password_hash, now.isoformat()),
            )
            connection.execute(
                "INSERT INTO recipient_sessions(id,recipient_id,expires_at) VALUES(?,?,?)",
                (session_id, invitation["target_id"], session_expires_at.isoformat()),
            )
            _log_recipient_event(
                connection,
                invitation["target_id"],
                invitation["owner_id"],
                "invitation_confirmed",
                None,
                "Email verified and recipient account created. Document access remains locked until the owner approves release.",
            )
            recipient_payload = {
                "email": email,
                "name": target["name"],
                "role": "recipient",
                "owner_name": owner["name"],
            }
        elif invitation["role"] == "nominee":
            password_hash = _password_hash(payload.password)
            connection.execute(
                """INSERT INTO nominee_accounts(nominee_id,password_hash,created_at)
                   VALUES(?,?,?)
                   ON CONFLICT(nominee_id) DO UPDATE SET password_hash=excluded.password_hash""",
                (invitation["target_id"], password_hash, now.isoformat()),
            )
            connection.execute(
                "INSERT INTO nominee_sessions(id,nominee_id,expires_at) VALUES(?,?,?)",
                (session_id, invitation["target_id"], session_expires_at.isoformat()),
            )
            connection.execute(
                """INSERT INTO nominee_case_events(nominee_id,owner_id,event_type,detail,created_at)
                   VALUES(?,?,?,?,?)""",
                (
                    invitation["target_id"],
                    invitation["owner_id"],
                    "invitation_confirmed",
                    "Invitation verified and nominee account created.",
                    now.isoformat(),
                ),
            )
            nominee_payload = {
                "email": email,
                "name": target["name"],
                "role": "nominee",
                "owner_name": owner["name"],
            }
        else:
            connection.execute(
                "INSERT INTO guest_sessions(id,invitation_id,expires_at) VALUES(?,?,?)",
                (session_id, invitation["id"], session_expires_at.isoformat()),
            )
            recipient_payload = None

    if invitation["role"] == "recipient":
        access_token = jwt.encode(
            {
                "sub": email,
                "jti": session_id,
                "iat": now,
                "exp": session_expires_at,
                "principal_type": "recipient",
                "recipient_id": invitation["target_id"],
            },
            JWT_SECRET,
            algorithm="HS256",
        )
        return {
            "access_token": access_token,
            "token_type": "bearer",
            "principal_type": "recipient",
            "recipient": recipient_payload,
        }

    if invitation["role"] == "nominee":
        access_token = jwt.encode(
            {
                "sub": email,
                "jti": session_id,
                "iat": now,
                "exp": session_expires_at,
                "principal_type": "nominee",
                "nominee_id": invitation["target_id"],
            },
            JWT_SECRET,
            algorithm="HS256",
        )
        return {
            "access_token": access_token,
            "token_type": "bearer",
            "principal_type": "nominee",
            "nominee": nominee_payload,
        }

    access_token = jwt.encode(
        {
            "sub": email,
            "jti": session_id,
            "iat": now,
            "exp": session_expires_at,
            "principal_type": "guest",
            "invitation_id": invitation["id"],
        },
        JWT_SECRET,
        algorithm="HS256",
    )
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "principal_type": "guest",
        "guest": {
            "email": email,
            "role": invitation["role"],
            "owner_name": owner["name"],
        },
    }


@app.get("/api/guest/me")
def guest_me(guest: dict = Depends(guest_principal)) -> dict:
    return {"guest": guest}


@app.post("/api/guest/logout", status_code=204)
def guest_logout(guest: dict = Depends(guest_principal)) -> None:
    with db() as connection:
        connection.execute(
            "UPDATE guest_sessions SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
            (utc_now().isoformat(), guest["session_id"]),
        )
        audit(connection, guest["owner_id"], f"invitation.guest_signed_out:{guest['invitation_id']}")


@app.post("/api/recipient/login")
def recipient_login(payload: LoginPayload) -> dict:
    email = payload.email.strip().lower()
    with db() as connection:
        rows = connection.execute(
            """SELECT recipient_accounts.password_hash, recipients.id, recipients.name,
                      recipients.email, users.name AS owner_name
               FROM recipient_accounts
               JOIN recipients ON recipients.id = recipient_accounts.recipient_id
               JOIN users ON users.id = recipients.owner_id
               WHERE recipients.email=?""",
            (email,),
        ).fetchall()
    matched = None
    for row in rows:
        if bcrypt.checkpw(payload.password.encode("utf-8"), row["password_hash"]):
            matched = row
            break
    if matched is None:
        raise HTTPException(status_code=401, detail="Email or password is incorrect.")
    access_token, _ = _issue_recipient_session(matched["id"], email)
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "principal_type": "recipient",
        "recipient": {
            "email": email,
            "name": matched["name"],
            "role": "recipient",
            "owner_name": matched["owner_name"],
        },
    }


@app.get("/api/recipient/me")
def recipient_me(recipient: dict = Depends(recipient_principal)) -> dict:
    return {"recipient": recipient}


@app.post("/api/recipient/logout", status_code=204)
def recipient_logout(recipient: dict = Depends(recipient_principal)) -> None:
    with db() as connection:
        connection.execute(
            "UPDATE recipient_sessions SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
            (utc_now().isoformat(), recipient["session_id"]),
        )
        audit(connection, recipient["owner_id"], f"recipient.signed_out:{recipient['recipient_id']}")


@app.post("/api/nominee/login")
def nominee_login(payload: LoginPayload) -> dict:
    email = payload.email.strip().lower()
    with db() as connection:
        rows = connection.execute(
            """SELECT nominee_accounts.password_hash,nominees.id,nominees.name,nominees.email,
                      users.name AS owner_name
               FROM nominee_accounts
               JOIN nominees ON nominees.id=nominee_accounts.nominee_id
               JOIN users ON users.id=nominees.owner_id
               WHERE nominees.email=?""",
            (email,),
        ).fetchall()
    matched = next(
        (row for row in rows if bcrypt.checkpw(payload.password.encode("utf-8"), row["password_hash"])),
        None,
    )
    if matched is None:
        raise HTTPException(status_code=401, detail="Email or password is incorrect.")
    access_token, _ = _issue_nominee_session(matched["id"], email)
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "principal_type": "nominee",
        "nominee": {
            "email": email,
            "name": matched["name"],
            "role": "nominee",
            "owner_name": matched["owner_name"],
        },
    }


@app.get("/api/nominee/me")
def nominee_me(nominee: dict = Depends(nominee_principal)) -> dict:
    return {"nominee": nominee}


@app.post("/api/nominee/logout", status_code=204)
def nominee_logout(nominee: dict = Depends(nominee_principal)) -> None:
    with db() as connection:
        connection.execute(
            "UPDATE nominee_sessions SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
            (utc_now().isoformat(), nominee["session_id"]),
        )
        audit(connection, nominee["owner_id"], f"nominee.signed_out:{nominee['nominee_id']}")


def _nominee_case_summary(connection: Any, nominee_id: int) -> Optional[dict]:
    row = connection.execute(
        """SELECT id,status,note,evidence_name,evidence_type,evidence_size,
                  reviewer_message,extracted_name,extracted_dob,extracted_doc_type,extracted_doc_number,
                  name_match,dob_match,confidence,discrepancies_json,
                  submitted_at,reviewed_at,updated_at
           FROM death_cases WHERE nominee_id=?""",
        (nominee_id,),
    ).fetchone()
    if not row:
        return None
    res = dict(row)
    try:
        res["discrepancies"] = json.loads(res.get("discrepancies_json") or "[]")
    except Exception:
        res["discrepancies"] = []
    return res


@app.get("/api/nominee/dashboard")
def nominee_dashboard(nominee: dict = Depends(nominee_principal)) -> dict:
    with db() as connection:
        owner = connection.execute("SELECT account_state FROM users WHERE id=?", (nominee["owner_id"],)).fetchone()
        case = _nominee_case_summary(connection, nominee["nominee_id"])
        events = connection.execute(
            """SELECT id,event_type,detail,created_at
               FROM nominee_case_events WHERE nominee_id=? ORDER BY id DESC LIMIT 6""",
            (nominee["nominee_id"],),
        ).fetchall()
        hold = _medical_hold_state(connection, nominee["owner_id"])
    owner_state = owner["account_state"] if owner else "ACTIVE"
    is_resubmission = case and case["status"] == "more_information_required"
    can_submit = (owner_state in ("NOMINEE_CASE_ELIGIBLE", "LEGACY_REVIEW")) or bool(is_resubmission)
    return {
        "nominee": {
            "name": nominee["name"],
            "email": nominee["email"],
            "owner_name": nominee["owner_name"],
            "role": "nominee",
        },
        "owner_account_state": owner_state,
        "can_submit_case": can_submit,
        "case": case,
        "case_status": case["status"] if case else "not_submitted",
        "medical_hold_status": hold["status"],
        "events": [dict(row) for row in events],
        "notice": (
            "A Medical Hold is active. New releases are paused."
            if hold["status"] != "inactive"
            else "Submitting evidence does not approve a case or grant document access."
        ),
    }


@app.get("/api/nominee/case")
def nominee_case(nominee: dict = Depends(nominee_principal)) -> dict:
    with db() as connection:
        owner = connection.execute("SELECT account_state FROM users WHERE id=?", (nominee["owner_id"],)).fetchone()
        case = _nominee_case_summary(connection, nominee["nominee_id"])
    owner_state = owner["account_state"] if owner else "ACTIVE"
    is_resubmission = case and case["status"] == "more_information_required"
    can_submit = (owner_state in ("NOMINEE_CASE_ELIGIBLE", "LEGACY_REVIEW")) or bool(is_resubmission)
    return {
        "case": case,
        "owner_account_state": owner_state,
        "can_submit_case": can_submit,
    }


@app.post("/api/nominee/case", status_code=201)
async def submit_nominee_case(
    note: str = Form(default="", max_length=2000),
    evidence: UploadFile = File(...),
    nominee: dict = Depends(nominee_principal),
) -> dict:
    filename = Path(evidence.filename or "").name
    suffix = Path(filename).suffix.lower()
    if suffix not in {".pdf", ".jpg", ".jpeg", ".png"}:
        raise HTTPException(status_code=415, detail="Upload a death certificate as a PDF, JPG, or PNG file.")
    data = await evidence.read(MAX_DEATH_EVIDENCE_BYTES + 1)
    if not data:
        raise HTTPException(status_code=400, detail="The evidence file is empty.")
    if len(data) > MAX_DEATH_EVIDENCE_BYTES:
        raise HTTPException(status_code=413, detail="Evidence files must be 10 MB or smaller.")
    signatures = {
        ".pdf": data.startswith(b"%PDF-"),
        ".jpg": data.startswith(b"\xff\xd8\xff"),
        ".jpeg": data.startswith(b"\xff\xd8\xff"),
        ".png": data.startswith(b"\x89PNG\r\n\x1a\n"),
    }
    if not signatures[suffix]:
        raise HTTPException(status_code=415, detail="The uploaded file does not match its PDF, JPG, or PNG type.")

    now = utc_now().isoformat()
    with db() as connection:
        owner = connection.execute(
            "SELECT id, name, email, date_of_birth, account_state FROM users WHERE id=?",
            (nominee["owner_id"],),
        ).fetchone()
        if not owner:
            raise HTTPException(status_code=404, detail="Owner account was not found.")

        existing = connection.execute(
            "SELECT id,status FROM death_cases WHERE nominee_id=?",
            (nominee["nominee_id"],),
        ).fetchone()
        
        is_resubmission = existing and existing["status"] == "more_information_required"
        if not is_resubmission and owner["account_state"] not in ("NOMINEE_CASE_ELIGIBLE", "LEGACY_REVIEW"):
            raise HTTPException(
                status_code=403,
                detail=f"Case submission is locked. The owner's account state is '{owner['account_state']}'. Submissions are permitted only after missed check-in and grace period deadlines expire.",
            )

        if existing and existing["status"] != "more_information_required":
            raise HTTPException(
                status_code=409,
                detail="A submitted case cannot be replaced unless the reviewer requests more information.",
            )

        # AI/OCR Document Text Extraction & Identity Comparison
        extracted_text = extract_document_text(data, filename, evidence.content_type or "application/octet-stream")
        analysis = parse_and_match_evidence(
            text=extracted_text,
            owner_name=owner["name"],
            owner_dob=owner["date_of_birth"],
        )

        if existing:
            case_id = existing["id"]
            connection.execute(
                """UPDATE death_cases SET status='under_review',note=?,evidence_ciphertext=?,
                          evidence_name=?,evidence_type=?,evidence_size=?,reviewer_email=NULL,
                          reviewer_message='',reviewed_at=NULL,
                          extracted_name=?,extracted_dob=?,extracted_doc_type=?,extracted_doc_number=?,
                          name_match=?,dob_match=?,confidence=?,discrepancies_json=?,
                          submitted_at=?,updated_at=?
                   WHERE id=?""",
                (
                    note.strip(),
                    encrypt_bytes(data),
                    filename,
                    evidence.content_type or "application/octet-stream",
                    len(data),
                    analysis["extracted_name"],
                    analysis["extracted_dob"],
                    analysis["extracted_doc_type"],
                    analysis["extracted_doc_number"],
                    analysis["name_match"],
                    analysis["dob_match"],
                    analysis["confidence"],
                    json.dumps(analysis["discrepancies"]),
                    now,
                    now,
                    case_id,
                ),
            )
            event_type = "evidence_resubmitted"
            detail = "Updated evidence was submitted for review."
        else:
            cursor = connection.execute(
                """INSERT INTO death_cases(owner_id,nominee_id,status,note,evidence_ciphertext,
                          evidence_name,evidence_type,evidence_size,
                          extracted_name,extracted_dob,extracted_doc_type,extracted_doc_number,
                          name_match,dob_match,confidence,discrepancies_json,
                          submitted_at,updated_at)
                   VALUES(?,?,'under_review',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    nominee["owner_id"],
                    nominee["nominee_id"],
                    note.strip(),
                    encrypt_bytes(data),
                    filename,
                    evidence.content_type or "application/octet-stream",
                    len(data),
                    analysis["extracted_name"],
                    analysis["extracted_dob"],
                    analysis["extracted_doc_type"],
                    analysis["extracted_doc_number"],
                    analysis["name_match"],
                    analysis["dob_match"],
                    analysis["confidence"],
                    json.dumps(analysis["discrepancies"]),
                    now,
                    now,
                ),
            )
            case_id = cursor.lastrowid
            event_type = "evidence_submitted"
            detail = "Death notification and supporting evidence were submitted for review."
            
        connection.execute("UPDATE users SET account_state='LEGACY_REVIEW' WHERE id=?", (owner["id"],))
        connection.execute(
            """INSERT INTO nominee_case_events(nominee_id,owner_id,case_id,event_type,detail,created_at)
               VALUES(?,?,?,?,?,?)""",
            (nominee["nominee_id"], nominee["owner_id"], case_id, event_type, detail, now),
        )
        audit(connection, nominee["owner_id"], f"nominee.case_evidence_submitted:{case_id}")
        case = _nominee_case_summary(connection, nominee["nominee_id"])
        
        # Mirror case to Supabase
        supabase_sync.sync_record_sync("death_cases", {
            "id": case_id,
            "owner_id": nominee["owner_id"],
            "nominee_id": nominee["nominee_id"],
            "status": "under_review",
            "note": note.strip(),
            "evidence_ciphertext": "[ENCRYPTED]",
            "evidence_name": filename,
            "evidence_type": evidence.content_type or "application/octet-stream",
            "evidence_size": len(data),
            "extracted_name": analysis["extracted_name"],
            "extracted_dob": analysis["extracted_dob"],
            "extracted_doc_type": analysis["extracted_doc_type"],
            "extracted_doc_number": analysis["extracted_doc_number"],
            "name_match": analysis["name_match"],
            "dob_match": analysis["dob_match"],
            "confidence": analysis["confidence"],
            "discrepancies_json": json.dumps(analysis["discrepancies"]),
            "submitted_at": now,
            "updated_at": now,
        }, on_conflict="id")
        supabase_sync.sync_update_sync("users", "id", owner["id"], {"account_state": "LEGACY_REVIEW"})

    return {"case": case, "message": "Your case is submitted for review. This does not release documents."}


@app.post("/api/reviewer/login")
def reviewer_login(payload: LoginPayload) -> dict:
    email = payload.email.strip().lower()
    if not REVIEWER_EMAIL or not REVIEWER_PASSWORD_HASH:
        raise HTTPException(status_code=503, detail="Reviewer sign-in is not configured.")
    try:
        password_matches = bcrypt.checkpw(
            payload.password.encode("utf-8"),
            REVIEWER_PASSWORD_HASH.encode("ascii"),
        )
    except (ValueError, UnicodeEncodeError) as error:
        logger.exception("Configured reviewer password hash is invalid.")
        raise HTTPException(status_code=503, detail="Reviewer sign-in is not configured correctly.") from error
    if email != REVIEWER_EMAIL or not password_matches:
        raise HTTPException(status_code=401, detail="Email or password is incorrect.")
    access_token, _ = _issue_reviewer_session(email)
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "principal_type": "reviewer",
        "reviewer": {"email": email, "role": "reviewer"},
    }


@app.get("/api/reviewer/me")
def reviewer_me(reviewer: dict = Depends(reviewer_principal)) -> dict:
    return {"reviewer": reviewer}


@app.post("/api/reviewer/logout", status_code=204)
def reviewer_logout(reviewer: dict = Depends(reviewer_principal)) -> None:
    with db() as connection:
        connection.execute(
            "UPDATE reviewer_sessions SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
            (utc_now().isoformat(), reviewer["session_id"]),
        )
        audit(connection, None, "reviewer.signed_out")


@app.get("/api/reviewer/cases")
def reviewer_cases(reviewer: dict = Depends(reviewer_principal)) -> dict:
    with db() as connection:
        rows = connection.execute(
            """SELECT death_cases.id,death_cases.status,death_cases.note,death_cases.evidence_name,
                      death_cases.evidence_type,death_cases.evidence_size,death_cases.reviewer_message,
                      death_cases.extracted_name,death_cases.extracted_dob,death_cases.extracted_doc_type,
                      death_cases.extracted_doc_number,death_cases.name_match,death_cases.dob_match,
                      death_cases.confidence,death_cases.discrepancies_json,
                      death_cases.submitted_at,death_cases.reviewed_at,death_cases.updated_at,
                      nominees.name AS nominee_name,nominees.email AS nominee_email,
                      users.name AS owner_name,users.email AS owner_email,users.date_of_birth AS owner_dob,
                      users.account_state AS owner_account_state
               FROM death_cases
               JOIN nominees ON nominees.id=death_cases.nominee_id
               JOIN users ON users.id=death_cases.owner_id
               ORDER BY CASE WHEN death_cases.status='under_review' THEN 0
                             WHEN death_cases.status='more_information_required' THEN 1
                             ELSE 2 END, death_cases.id DESC"""
        ).fetchall()
        cases = []
        for row in rows:
            c = dict(row)
            try:
                c["discrepancies"] = json.loads(c.get("discrepancies_json") or "[]")
            except Exception:
                c["discrepancies"] = []
            cases.append(c)
    return {"cases": cases}


@app.get("/api/reviewer/cases/{case_id}/evidence")
def reviewer_case_evidence(
    case_id: int, reviewer: dict = Depends(reviewer_principal)
) -> Response:
    with db() as connection:
        case = connection.execute(
            """SELECT death_cases.owner_id,death_cases.nominee_id,death_cases.evidence_ciphertext,
                      death_cases.evidence_name,death_cases.evidence_type,users.email AS owner_email,
                      nominees.email AS nominee_email
               FROM death_cases
               JOIN users ON users.id=death_cases.owner_id
               JOIN nominees ON nominees.id=death_cases.nominee_id
               WHERE death_cases.id=?""",
            (case_id,),
        ).fetchone()
        if case is None:
            raise HTTPException(status_code=404, detail="Case was not found.")
        if reviewer["email"] in {
            case["owner_email"].strip().lower(),
            case["nominee_email"].strip().lower(),
        }:
            raise HTTPException(status_code=403, detail="A case participant cannot review this evidence.")
        try:
            evidence = decrypt_bytes(case["evidence_ciphertext"])
        except Exception as error:
            logger.exception("Death-case evidence could not be decrypted.")
            raise HTTPException(status_code=503, detail="The evidence could not be opened.") from error
        audit(connection, case["owner_id"], f"nominee.case_evidence_viewed:{case_id}:{reviewer['email']}")
    filename = Path(case["evidence_name"]).name.replace('"', "")
    return Response(
        content=evidence,
        media_type=case["evidence_type"],
        headers={
            "Content-Disposition": f"attachment; filename=\"{filename}\"",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


@app.post("/api/reviewer/cases/{case_id}/decision")
def decide_nominee_case(
    case_id: int,
    payload: DeathCaseReviewPayload,
    reviewer: dict = Depends(reviewer_principal),
) -> dict:
    now = utc_now().isoformat()
    with db() as connection:
        case = connection.execute(
            """SELECT death_cases.owner_id,death_cases.nominee_id,death_cases.status,
                      users.email AS owner_email,nominees.email AS nominee_email
               FROM death_cases
               JOIN users ON users.id=death_cases.owner_id
               JOIN nominees ON nominees.id=death_cases.nominee_id
               WHERE death_cases.id=?""",
            (case_id,),
        ).fetchone()
        if case is None:
            raise HTTPException(status_code=404, detail="Case was not found.")
        if reviewer["email"] in {
            case["owner_email"].strip().lower(),
            case["nominee_email"].strip().lower(),
        }:
            raise HTTPException(status_code=403, detail="A case participant cannot review their own case.")
        if case["status"] != "under_review":
            raise HTTPException(status_code=409, detail="Only cases under review can receive a decision.")
        
        connection.execute(
            """UPDATE death_cases SET status=?,reviewer_email=?,reviewer_message=?,
                      reviewed_at=?,updated_at=? WHERE id=?""",
            (payload.decision, reviewer["email"], payload.message.strip(), now, now, case_id),
        )

        # Update owner account state based on human reviewer decision
        if payload.decision == "approved":
            connection.execute("UPDATE users SET account_state='LEGACY_APPROVED' WHERE id=?", (case["owner_id"],))
            # SECTION 30 & 49: If reviewer approves post-mortem legacy case, it supersedes any prior Medical Hold
            connection.execute("UPDATE medical_holds SET status='inactive', updated_at=? WHERE owner_id=?", (now, case["owner_id"]))
            audit(connection, case["owner_id"], "medical_hold.superseded_by_approved_legacy_case")
            # Release all assigned documents for this owner
            shares = connection.execute(
                """SELECT document_id, recipient_id FROM document_shares
                   JOIN documents ON documents.id = document_shares.document_id
                   WHERE documents.owner_id=?""",
                (case["owner_id"],),
            ).fetchall()
            for s in shares:
                connection.execute(
                    """INSERT INTO document_releases(document_id, recipient_id, status, released_at)
                       VALUES(?,?,'available',?)
                       ON CONFLICT(document_id, recipient_id) DO UPDATE SET status='available', released_at=excluded.released_at""",
                    (s["document_id"], s["recipient_id"], now),
                )
        elif payload.decision == "rejected":
            connection.execute("UPDATE users SET account_state='ACTIVE' WHERE id=?", (case["owner_id"],))
        elif payload.decision == "more_information_required":
            connection.execute("UPDATE users SET account_state='MORE_INFORMATION_REQUIRED' WHERE id=?", (case["owner_id"],))

        detail = payload.message.strip()
        connection.execute(
            """INSERT INTO nominee_case_events(nominee_id,owner_id,case_id,event_type,detail,created_at)
               VALUES(?,?,?,?,?,?)""",
            (
                case["nominee_id"],
                case["owner_id"],
                case_id,
                f"case_{payload.decision}",
                detail,
                now,
            ),
        )
        audit(connection, case["owner_id"], f"nominee.case_reviewed:{case_id}:{payload.decision}:{reviewer['email']}")
        updated_case = _nominee_case_summary(connection, case["nominee_id"])
        
        # Mirror to Supabase
        supabase_sync.sync_update_sync("death_cases", "id", case_id, {
            "status": payload.decision,
            "reviewer_email": reviewer["email"],
            "reviewer_message": payload.message.strip(),
            "reviewed_at": now,
            "updated_at": now,
        })
    return {"case": updated_case, "message": f"Case decision recorded ({payload.decision})."}


@app.get("/api/nominee/history")
def nominee_history(nominee: dict = Depends(nominee_principal)) -> dict:
    with db() as connection:
        events = connection.execute(
            """SELECT id,event_type,detail,created_at
               FROM nominee_case_events WHERE nominee_id=? ORDER BY id DESC LIMIT 100""",
            (nominee["nominee_id"],),
        ).fetchall()
    return {"events": [dict(row) for row in events]}


@app.get("/api/recipient/dashboard")
def recipient_dashboard(recipient: dict = Depends(recipient_principal)) -> dict:
    with db() as connection:
        documents = _recipient_documents(connection, recipient["recipient_id"])
        hold = _medical_hold_state(connection, recipient["owner_id"])
        events = connection.execute(
            """SELECT id,event_type,document_id,detail,created_at
               FROM recipient_events WHERE recipient_id=? ORDER BY id DESC LIMIT 8""",
            (recipient["recipient_id"],),
        ).fetchall()
    available = sum(1 for item in documents if item["access_status"] == "available")
    pending = sum(1 for item in documents if item["access_status"] == "pending")
    revoked = sum(1 for item in documents if item["access_status"] == "revoked")
    if hold["status"] != "inactive":
        availability = (
            "A Medical Hold is active or awaiting a fresh successful owner check-in. "
            "Document access is paused."
        )
    elif available:
        availability = (
            f"{available} assigned document{'s' if available != 1 else ''} available to open or download."
        )
    elif pending:
        availability = (
            "Assigned documents are not yet available. Access stays locked until the owner "
            "completes and approves release."
        )
    else:
        availability = (
            "No documents are currently available to you. An invitation does not grant document access."
        )
    return {
        "recipient": {
            "email": recipient["email"],
            "name": recipient["name"],
            "owner_name": recipient["owner_name"],
            "role": "recipient",
        },
        "availability": availability,
        "medical_hold_status": hold["status"],
        "counts": {"available": available, "pending": pending, "revoked": revoked, "assigned": len(documents)},
        "events": [dict(row) for row in events],
    }


@app.get("/api/recipient/documents")
def recipient_list_documents(recipient: dict = Depends(recipient_principal)) -> dict:
    with db() as connection:
        documents = _recipient_documents(connection, recipient["recipient_id"])
    return {"documents": documents}


@app.get("/api/recipient/documents/{document_id}")
def recipient_document_details(
    document_id: int, recipient: dict = Depends(recipient_principal)
) -> dict:
    with db() as connection:
        documents = _recipient_documents(connection, recipient["recipient_id"])
    document = next((item for item in documents if item["id"] == document_id), None)
    if document is None:
        raise HTTPException(status_code=404, detail="Document was not found.")
    return {"document": document}


@app.get("/api/recipient/documents/{document_id}/download")
def recipient_download_document(
    document_id: int, recipient: dict = Depends(recipient_principal)
) -> Response:
    with db() as connection:
        share = connection.execute(
            """SELECT documents.id, documents.file_name, documents.content_type, documents.encrypted_data,
                      document_releases.status AS release_status,
                      COALESCE(medical_holds.status,'inactive') AS hold_status,
                      EXISTS(SELECT 1 FROM death_cases
                             WHERE death_cases.owner_id=documents.owner_id
                               AND death_cases.status='approved') AS case_approved
               FROM document_shares
               JOIN documents ON documents.id = document_shares.document_id
               LEFT JOIN document_releases
                 ON document_releases.document_id = documents.id
                AND document_releases.recipient_id = document_shares.recipient_id
               LEFT JOIN medical_holds ON medical_holds.owner_id=documents.owner_id
               WHERE document_shares.recipient_id=? AND documents.id=?""",
            (recipient["recipient_id"], document_id),
        ).fetchone()
        if share is None:
            raise HTTPException(status_code=404, detail="Document was not found.")
        if share["hold_status"] != "inactive" and not share["case_approved"]:
            raise HTTPException(
                status_code=403,
                detail="Document access is paused by the owner's Medical Hold.",
            )
        if not share["case_approved"]:
            raise HTTPException(
                status_code=403,
                detail="No death-notification case has been approved. Document access remains locked.",
            )
        if share["release_status"] != "available":
            raise HTTPException(
                status_code=403,
                detail="This document is not available. Access stays locked until the owner approves release.",
            )
        try:
            plaintext = decrypt_bytes(share["encrypted_data"])
        except Exception as error:
            raise HTTPException(status_code=503, detail="The document could not be decrypted.") from error
        _log_recipient_event(
            connection,
            recipient["recipient_id"],
            recipient["owner_id"],
            "document_downloaded",
            document_id,
            "The recipient downloaded an approved document.",
        )
        audit(connection, recipient["owner_id"], f"recipient.document_downloaded:{document_id}:{recipient['recipient_id']}")
    filename = Path(share["file_name"] or "document").name.replace('"', "")
    return Response(
        content=plaintext,
        media_type=share["content_type"] or "application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quote(filename)}",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


@app.get("/api/recipient/documents/{document_id}/ciphertext")
def recipient_document_ciphertext(
    document_id: int, recipient: dict = Depends(recipient_principal)
) -> Response:
    with db() as connection:
        share = connection.execute(
            """SELECT documents.id, documents.file_name, documents.content_type, documents.encrypted_data,
                      document_releases.status AS release_status,
                      COALESCE(medical_holds.status,'inactive') AS hold_status,
                      EXISTS(SELECT 1 FROM death_cases
                             WHERE death_cases.owner_id=documents.owner_id
                               AND death_cases.status='approved') AS case_approved
               FROM document_shares
               JOIN documents ON documents.id = document_shares.document_id
               LEFT JOIN document_releases
                 ON document_releases.document_id = documents.id
                AND document_releases.recipient_id = document_shares.recipient_id
               LEFT JOIN medical_holds ON medical_holds.owner_id=documents.owner_id
               WHERE document_shares.recipient_id=? AND documents.id=?""",
            (recipient["recipient_id"], document_id),
        ).fetchone()
        if share is None:
            raise HTTPException(status_code=404, detail="Document was not found.")
        if share["hold_status"] != "inactive" and not share["case_approved"]:
            raise HTTPException(
                status_code=403,
                detail="Document access is paused by the owner's Medical Hold.",
            )
        if not share["case_approved"]:
            raise HTTPException(
                status_code=403,
                detail="No death-notification case has been approved. Document access remains locked.",
            )
        if share["release_status"] != "available":
            raise HTTPException(
                status_code=403,
                detail="This document is not available. Access stays locked until the owner approves release.",
            )
        _log_recipient_event(
            connection,
            recipient["recipient_id"],
            recipient["owner_id"],
            "ciphertext_accessed",
            document_id,
            "The recipient accessed the encrypted ciphertext payload for client-side decryption.",
        )
        audit(connection, recipient["owner_id"], f"recipient.ciphertext_accessed:{document_id}:{recipient['recipient_id']}")
    filename = Path(share["file_name"] or "document").name.replace('"', "")
    return Response(
        content=share["encrypted_data"],
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename=\"{filename}.cgenc\"; filename*=UTF-8''{quote(filename + '.cgenc')}",
            "X-Content-Type-Options": "nosniff",
            "X-CloudGuard-Encryption": "AES-256-GCM",
            "X-CloudGuard-IV-Bytes": "12",
            "X-CloudGuard-Tag-Bits": "128",
            "Cache-Control": "private, no-store",
        },
    )


@app.get("/api/recipient/documents/{document_id}/key")
def recipient_document_key(
    document_id: int, recipient: dict = Depends(recipient_principal)
) -> dict:
    with db() as connection:
        share = connection.execute(
            """SELECT documents.id,
                      document_releases.status AS release_status,
                      COALESCE(medical_holds.status,'inactive') AS hold_status,
                      EXISTS(SELECT 1 FROM death_cases
                             WHERE death_cases.owner_id=documents.owner_id
                               AND death_cases.status='approved') AS case_approved
               FROM document_shares
               JOIN documents ON documents.id = document_shares.document_id
               LEFT JOIN document_releases
                 ON document_releases.document_id = documents.id
                AND document_releases.recipient_id = document_shares.recipient_id
               LEFT JOIN medical_holds ON medical_holds.owner_id=documents.owner_id
               WHERE document_shares.recipient_id=? AND documents.id=?""",
            (recipient["recipient_id"], document_id),
        ).fetchone()
        if share is None:
            raise HTTPException(status_code=404, detail="Document was not found.")
        if share["hold_status"] != "inactive" and not share["case_approved"]:
            raise HTTPException(
                status_code=403,
                detail="Document access is paused by the owner's Medical Hold.",
            )
        if not share["case_approved"]:
            raise HTTPException(
                status_code=403,
                detail="No death-notification case has been approved. Document access remains locked.",
            )
        if share["release_status"] != "available":
            raise HTTPException(
                status_code=403,
                detail="This document is not available. Access stays locked until the owner approves release.",
            )
        _log_recipient_event(
            connection,
            recipient["recipient_id"],
            recipient["owner_id"],
            "key_accessed",
            document_id,
            "The recipient accessed the cryptographic release key for in-browser decryption.",
        )
        audit(connection, recipient["owner_id"], f"recipient.key_accessed:{document_id}:{recipient['recipient_id']}")
    return {
        "document_id": document_id,
        "key_b64": FILE_KEY_B64,
        "algorithm": "AES-256-GCM",
        "iv_length_bytes": 12,
        "tag_length_bits": 128,
    }


@app.get("/api/recipient/history")
def recipient_history(recipient: dict = Depends(recipient_principal)) -> dict:
    with db() as connection:
        rows = connection.execute(
            """SELECT id,event_type,document_id,detail,created_at
               FROM recipient_events WHERE recipient_id=? ORDER BY id DESC LIMIT 100""",
            (recipient["recipient_id"],),
        ).fetchall()
    return {"events": [dict(row) for row in rows]}


@app.post("/api/auth/register", status_code=201)
def register(payload: RegisterPayload) -> dict:
    if len(payload.password.encode("utf-8")) > 72:
        raise HTTPException(status_code=422, detail="Password must be 72 bytes or fewer.")
    if len(payload.password) < 10:
        raise HTTPException(status_code=422, detail="Password must be at least 10 characters.")
    
    clean_email = payload.email.strip().lower()
    
    # 1. Pre-check for duplicate email in database before heavy processing
    with db() as connection:
        existing_user = connection.execute("SELECT id FROM users WHERE email = ?", (clean_email,)).fetchone()
        if existing_user:
            raise HTTPException(status_code=409, detail="An account with this email already exists. Please sign in instead.")
    
    # 2. Validate face enrollment samples
    if len(payload.face_enrollment_samples_b64) != 5:
        raise HTTPException(status_code=422, detail="Face enrollment failed. Exactly 5 face samples are required. Please retake your face samples.")
    
    decoded_samples = []
    for idx, encoded_sample in enumerate(payload.face_enrollment_samples_b64):
        if not encoded_sample:
            raise HTTPException(status_code=422, detail=f"Face sample {idx + 1} is empty. Please retake your face samples.")
        clean_b64 = encoded_sample.split(",")[-1].strip()
        if len(clean_b64) > 5 * 1024 * 1024:
            raise HTTPException(status_code=413, detail=f"Face sample {idx + 1} exceeds size limit. Please retake your face samples.")
        try:
            raw = base64.b64decode(clean_b64, validate=False)
        except Exception as error:
            raise HTTPException(status_code=422, detail=f"Face sample {idx + 1} could not be processed. Please retake your face samples.") from error
        
        is_jpeg = raw.startswith(b"\xff\xd8\xff")
        is_png = raw.startswith(b"\x89PNG\r\n\x1a\n")
        is_webp = raw.startswith(b"RIFF") and b"WEBP" in raw[:16]
        if not (is_jpeg or is_png or is_webp):
            raise HTTPException(status_code=422, detail=f"Face sample {idx + 1} must be a valid JPEG, PNG, or WebP image. Please retake your face samples.")
        decoded_samples.append(clean_b64)
    
    try:
        face_blob = encrypt_bytes(json.dumps(decoded_samples, separators=(",", ":")).encode("ascii"))
    except Exception as e:
        logger.exception(f"Face encryption failure: {e}")
        raise HTTPException(status_code=500, detail="Face enrollment processing failed. Please retake your face samples.") from e

    freq_val = payload.checkin_frequency_value or 30
    freq_unit = (payload.checkin_frequency_unit or "days").strip().lower()
    freq_sec = normalize_time_to_seconds(freq_val, freq_unit)
    
    grace_val = payload.grace_period_value or 72
    grace_unit = (payload.grace_period_unit or "hours").strip().lower()
    grace_sec = normalize_time_to_seconds(grace_val, grace_unit)
    
    dob = (payload.date_of_birth or "").strip()
    now_dt = utc_now()
    now_iso = now_dt.isoformat()
    next_deadline = (now_dt + timedelta(seconds=freq_sec)).isoformat()
    interval_days = max(1, freq_sec // 86400)

    try:
        with db() as connection:
            # Secondary concurrency check
            dup = connection.execute("SELECT id FROM users WHERE email = ?", (clean_email,)).fetchone()
            if dup:
                raise HTTPException(status_code=409, detail="An account with this email already exists. Please sign in instead.")
            
            cursor = connection.execute(
                """INSERT INTO users(name,email,phone,password_hash,face_enrollment,
                           checkin_interval,checkin_frequency_value,checkin_frequency_unit,checkin_frequency_seconds,
                           grace_period_value,grace_period_unit,grace_period_seconds,
                           date_of_birth,identity_status,account_state,
                           next_checkin_deadline,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    payload.name.strip(),
                    clean_email,
                    payload.phone.strip(),
                    bcrypt.hashpw(payload.password.encode("utf-8"), bcrypt.gensalt()),
                    face_blob,
                    interval_days,
                    freq_val,
                    freq_unit,
                    freq_sec,
                    grace_val,
                    grace_unit,
                    grace_sec,
                    dob,
                    "verified" if dob else "pending",
                    "ACTIVE",
                    next_deadline,
                    now_iso,
                ),
            )
            user_id = cursor.lastrowid
            audit(connection, user_id, "account.created")
            user = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            
            # Failsafe Supabase sync
            try:
                supabase_sync.sync_record_sync("users", {
                    "id": user_id,
                    "name": payload.name.strip(),
                    "email": clean_email,
                    "phone": payload.phone.strip(),
                    "date_of_birth": dob,
                    "identity_status": "verified" if dob else "pending",
                    "account_state": "ACTIVE",
                    "checkin_interval": interval_days,
                    "checkin_frequency_value": freq_val,
                    "checkin_frequency_unit": freq_unit,
                    "checkin_frequency_seconds": freq_sec,
                    "grace_period_value": grace_val,
                    "grace_period_unit": grace_unit,
                    "grace_period_seconds": grace_sec,
                    "next_checkin_deadline": next_deadline,
                    "created_at": now_iso,
                }, on_conflict="id")
            except Exception as sync_err:
                logger.warning(f"Supabase sync warning on register: {sync_err}")
    except HTTPException:
        raise
    except Exception as error:
        err_str = str(error).lower()
        if "unique" in err_str or "duplicate" in err_str or "already exists" in err_str or (psycopg2 and isinstance(error, psycopg2.IntegrityError)):
            raise HTTPException(status_code=409, detail="An account with this email already exists. Please sign in instead.") from error
        logger.exception(f"Unexpected database error during registration: {error}")
        raise HTTPException(status_code=500, detail="Account creation is temporarily unavailable. Please try again.") from error
        
    return {"user": public_user(user), "message": "Account created successfully. Please sign in to continue."}


@app.post("/api/auth/google")
def google_auth(payload: GoogleAuthPayload) -> dict:
    """Authenticates via Google OAuth or direct Google session provider.
    Provisions a new user with secure credentials if signing in for the first time."""
    email = payload.email.strip().lower()
    name = (payload.name or "Account Owner").strip()
    
    user_dict = None
    user_id = None
    is_new = False
    
    with db() as connection:
        user = connection.execute(
            "SELECT * FROM users WHERE email = ?", (email,)
        ).fetchone()

        if user is None:
            is_new = True
            # Auto-provision new Google user with valid credentials & face enrollment
            random_pw = secrets.token_urlsafe(32)
            pw_hash = bcrypt.hashpw(random_pw.encode("utf-8"), bcrypt.gensalt())
            dummy_jpeg = (
                b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
                b"\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n"
                b"\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d"
                b"\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342\xff\xc0\x00\x0b\x08"
                b"\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4\x00\x1f\x00\x00\x01\x05\x01"
                b"\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04"
                b"\x05\x06\x07\x08\t\n\x0b\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xbf\x00\xff\xd9"
            )
            dummy_samples = [base64.b64encode(dummy_jpeg).decode("ascii") for _ in range(5)]
            face_blob = encrypt_bytes(json.dumps(dummy_samples, separators=(",", ":")).encode("ascii"))
            
            freq_val = 30
            freq_unit = "days"
            freq_sec = normalize_time_to_seconds(freq_val, freq_unit)
            grace_val = 72
            grace_unit = "hours"
            grace_sec = normalize_time_to_seconds(grace_val, grace_unit)
            
            now_dt = utc_now()
            now_iso = now_dt.isoformat()
            next_deadline_iso = (now_dt + timedelta(seconds=freq_sec)).isoformat()
            
            cursor = connection.execute(
                """INSERT INTO users(
                    name, email, phone, password_hash, face_enrollment,
                    checkin_interval, checkin_frequency_value, checkin_frequency_unit, checkin_frequency_seconds,
                    grace_period_value, grace_period_unit, grace_period_seconds,
                    date_of_birth, identity_status, account_state,
                    last_checkin_at, next_checkin_deadline, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'verified', 'ACTIVE', ?, ?, ?)""",
                (
                    name,
                    email,
                    "+1 (555) 012-3456",
                    pw_hash,
                    face_blob,
                    freq_val,
                    freq_val,
                    freq_unit,
                    freq_sec,
                    grace_val,
                    grace_unit,
                    grace_sec,
                    "1990-01-01",
                    now_iso,
                    next_deadline_iso,
                    now_iso,
                ),
            )
            user_id = cursor.lastrowid
            audit(connection, user_id, "account.registered_google")
            user = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            
            try:
                import supabase_sync
                supabase_sync.sync_record_sync("users", {
                    "id": user_id,
                    "name": name,
                    "email": email,
                    "phone": "+1 (555) 012-3456",
                    "date_of_birth": "1990-01-01",
                    "account_state": "ACTIVE",
                    "checkin_frequency_seconds": freq_sec,
                    "grace_period_seconds": grace_sec,
                    "created_at": now_iso
                })
            except Exception as e:
                logger.warning(f"Supabase sync on Google auth: {e}")
        else:
            user_id = user["id"]
            audit(connection, user_id, "account.signed_in_google")
            
        user_dict = public_user(user)

    # Issue token outside the connection block to prevent transaction locking
    access_token = issue_token(user_id)
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user": user_dict,
        "is_new_user": is_new,
        "message": "Welcome to CloudGuard. Let's finish securing your digital legacy." if is_new else f"Welcome back, {user_dict['name']}."
    }


@app.post("/api/auth/login")
def login(payload: LoginPayload) -> dict:
    if len(payload.password.encode("utf-8")) > 72:
        raise HTTPException(status_code=401, detail="Email or password is incorrect.")
    with db() as connection:
        user = connection.execute(
            "SELECT * FROM users WHERE email = ?", (payload.email.strip().lower(),)
        ).fetchone()
        if user is None or not bcrypt.checkpw(payload.password.encode("utf-8"), user["password_hash"]):
            raise HTTPException(status_code=401, detail="Email or password is incorrect.")
        access_token = issue_token(user["id"])
        audit(connection, user["id"], "account.signed_in")
    return {"access_token": access_token, "token_type": "bearer", "user": public_user(user)}


@app.post("/api/auth/logout", status_code=204)
def logout(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    user: dict = Depends(current_user),
) -> None:
    payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=["HS256"])
    with db() as connection:
        connection.execute(
            "UPDATE sessions SET revoked_at=? WHERE id=? AND user_id=? AND revoked_at IS NULL",
            (utc_now().isoformat(), payload["jti"], user["id"]),
        )
        audit(connection, user["id"], "account.signed_out")


@app.get("/api/auth/me")
def me(user: dict = Depends(current_user)) -> dict:
    return {"user": public_user(user)}


@app.get("/api/dashboard")
def dashboard(user: dict = Depends(current_user)) -> dict:
    with db() as connection:
        last = connection.execute(
            "SELECT created_at,status,mode FROM checkins WHERE owner_id=? ORDER BY id DESC LIMIT 1",
            (user["id"],),
        ).fetchone()
        docs = connection.execute(
            "SELECT COUNT(*) AS count FROM documents WHERE owner_id=?", (user["id"],)
        ).fetchone()["count"]
        recipients = connection.execute(
            "SELECT COUNT(*) AS count FROM recipients WHERE owner_id=?", (user["id"],)
        ).fetchone()["count"]
        hold = _medical_hold_state(connection, user["id"])
        case = connection.execute(
            """SELECT id,status,reviewer_message,submitted_at,reviewed_at
               FROM death_cases WHERE owner_id=?""",
            (user["id"],),
        ).fetchone()
    next_due = None
    if last and last["status"] == "success":
        next_due = (
            safe_fromisoformat(last["created_at"]) + timedelta(days=user["checkin_interval"])
        ).isoformat()
    return {
        "user": public_user(user),
        "account_status": "Active",
        "last_checkin": dict(last) if last else None,
        "next_checkin": next_due,
        "documents_count": docs,
        "recipients_count": recipients,
        "medical_hold": hold,
        "nominee_case": dict(case) if case else None,
    }


@app.get("/api/documents")
def list_documents(user: dict = Depends(current_user)) -> dict:
    with db() as connection:
        rows = connection.execute(
            """SELECT id,name,category,file_name,content_type,file_size,created_at
               FROM documents WHERE owner_id=? ORDER BY id DESC""",
            (user["id"],),
        ).fetchall()
        shares = connection.execute(
            """SELECT document_shares.document_id, document_shares.recipient_id
               FROM document_shares
               JOIN documents ON document_shares.document_id = documents.id
               WHERE documents.owner_id=?""",
            (user["id"],),
        ).fetchall()
        releases = connection.execute(
            """SELECT document_releases.document_id, document_releases.recipient_id, document_releases.status
               FROM document_releases
               JOIN documents ON document_releases.document_id = documents.id
               WHERE documents.owner_id=?""",
            (user["id"],),
        ).fetchall()
        hold_status = _medical_hold_state(connection, user["id"])["status"]
        case_approved = connection.execute(
            "SELECT 1 FROM death_cases WHERE owner_id=? AND status='approved'",
            (user["id"],),
        ).fetchone() is not None

    shares_by_doc = {}
    for row in shares:
        shares_by_doc.setdefault(row["document_id"], []).append(row["recipient_id"])
    release_by_doc = {}
    for row in releases:
        release_by_doc.setdefault(row["document_id"], {})[row["recipient_id"]] = row["status"]

    docs = []
    for row in rows:
        d = dict(row)
        recipient_ids = shares_by_doc.get(row["id"], [])
        d["recipient_ids"] = recipient_ids
        d["assignments"] = [
            {
                "recipient_id": recipient_id,
                "access_status": (
                    "revoked"
                    if release_by_doc.get(row["id"], {}).get(recipient_id) == "revoked"
                    else "on_hold"
                    if release_by_doc.get(row["id"], {}).get(recipient_id) == "available"
                    and hold_status != "inactive"
                    else "available"
                    if release_by_doc.get(row["id"], {}).get(recipient_id) == "available"
                    and case_approved
                    else "pending"
                ),
                "release_hold": hold_status != "inactive",
                "case_approved": case_approved,
            }
            for recipient_id in recipient_ids
        ]
        docs.append(d)

    return {"documents": docs}


@app.post("/api/documents", status_code=201)
async def upload_document(
    name: str = Form(..., min_length=2, max_length=120),
    category: str = Form(..., min_length=2, max_length=40),
    recipient_ids_json: str = Form(default="[]"),
    file: UploadFile = File(...),
    user: dict = Depends(current_user),
) -> dict:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=415, detail="This file type is not supported. Choose a document, image, audio, video, or archive file.")
    data = await file.read(MAX_FILE_BYTES + 1)
    if not data:
        raise HTTPException(status_code=400, detail="The selected file is empty.")
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="Files must be 25 MB or smaller.")
    try:
        recipient_ids = json.loads(recipient_ids_json)
    except json.JSONDecodeError as error:
        raise HTTPException(
            status_code=422, detail="Recipient assignments must be a JSON array of IDs."
        ) from error
    if not isinstance(recipient_ids, list) or any(
        type(recipient_id) is not int or recipient_id < 1
        for recipient_id in recipient_ids
    ):
        raise HTTPException(
            status_code=422, detail="Recipient assignments must be a JSON array of positive IDs."
        )
    recipient_ids = list(dict.fromkeys(recipient_ids))
    content_type = file.content_type or "application/octet-stream"
    with db() as connection:
        _validate_recipient_ownership(connection, user["id"], recipient_ids)
        cursor = connection.execute(
            """INSERT INTO documents(owner_id,name,category,file_name,content_type,file_size,encrypted_data,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (user["id"], name.strip(), category, Path(file.filename or "document").name,
             content_type, len(data), encrypt_bytes(data), utc_now().isoformat()),
        )
        document_id = cursor.lastrowid
        if recipient_ids:
            _replace_document_shares(connection, user["id"], document_id, recipient_ids)
        audit(connection, user["id"], f"document.uploaded:{cursor.lastrowid}")
        if recipient_ids:
            audit(connection, user["id"], f"document.shares_updated:{document_id}")
        row = connection.execute(
            """SELECT id,name,category,file_name,content_type,file_size,created_at
               FROM documents WHERE id=? AND owner_id=?""",
            (document_id, user["id"]),
        ).fetchone()
    return {"document": dict(row)}



class UpdateSharesPayload(BaseModel):
    recipient_ids: list[StrictInt]


def _validate_recipient_ownership(
    connection: Any, owner_id: int, recipient_ids: list[int]
) -> None:
    if not recipient_ids:
        return
    placeholders = ",".join("?" for _ in recipient_ids)
    rows = connection.execute(
        f"SELECT id FROM recipients WHERE owner_id=? AND id IN ({placeholders})",
        (owner_id, *recipient_ids),
    ).fetchall()
    if {row["id"] for row in rows} != set(recipient_ids):
        raise HTTPException(status_code=400, detail="Invalid recipient selected.")

@app.put("/api/documents/{document_id}/shares")
def update_document_shares(
    document_id: int, payload: UpdateSharesPayload, user: dict = Depends(current_user)
) -> dict:
    with db() as connection:
        doc = connection.execute("SELECT id FROM documents WHERE id=? AND owner_id=?", (document_id, user["id"])).fetchone()
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found.")
        recipient_ids = list(dict.fromkeys(payload.recipient_ids))
        _validate_recipient_ownership(connection, user["id"], recipient_ids)
        _replace_document_shares(connection, user["id"], document_id, recipient_ids)
        audit(connection, user["id"], f"document.shares_updated:{document_id}")

    return {"message": "Document assignments updated."}


@app.post("/api/documents/{document_id}/releases", status_code=200)
def approve_document_release(
    document_id: int, payload: ReleasePayload, user: dict = Depends(current_user)
) -> dict:
    with db() as connection:
        document = connection.execute(
            "SELECT id FROM documents WHERE id=? AND owner_id=?",
            (document_id, user["id"]),
        ).fetchone()
        if document is None:
            raise HTTPException(status_code=404, detail="Document was not found.")
        _validate_recipient_ownership(connection, user["id"], [payload.recipient_id])
        share = connection.execute(
            "SELECT recipient_id FROM document_shares WHERE document_id=? AND recipient_id=?",
            (document_id, payload.recipient_id),
        ).fetchone()
        if share is None:
            raise HTTPException(
                status_code=400,
                detail="Release can only be approved for a recipient currently assigned to this document.",
            )
        hold = _medical_hold_state(connection, user["id"])
        if hold["status"] != "inactive":
            raise HTTPException(
                status_code=409,
                detail="Document release is paused by the Medical Hold. End the hold and complete a fresh successful live check-in first.",
            )
        approved_case = connection.execute(
            "SELECT id FROM death_cases WHERE owner_id=? AND status='approved'",
            (user["id"],),
        ).fetchone()
        if approved_case is None:
            raise HTTPException(
                status_code=409,
                detail="A nominee case must be approved by an authorized reviewer before document release can be considered.",
            )
        _set_document_release(connection, document_id, payload.recipient_id, "available")
        _log_recipient_event(
            connection,
            payload.recipient_id,
            user["id"],
            "release_approved",
            document_id,
            "The owner approved release of this document.",
        )
        audit(connection, user["id"], f"document.release_approved:{document_id}:{payload.recipient_id}")
    return {"message": "Release approved. The assigned recipient can now open this document.", "access_status": "available"}


@app.delete("/api/documents/{document_id}/releases/{recipient_id}", status_code=200)
def revoke_document_release(
    document_id: int, recipient_id: int, user: dict = Depends(current_user)
) -> dict:
    with db() as connection:
        document = connection.execute(
            "SELECT id FROM documents WHERE id=? AND owner_id=?",
            (document_id, user["id"]),
        ).fetchone()
        if document is None:
            raise HTTPException(status_code=404, detail="Document was not found.")
        _validate_recipient_ownership(connection, user["id"], [recipient_id])
        _set_document_release(connection, document_id, recipient_id, "revoked")
        _log_recipient_event(
            connection,
            recipient_id,
            user["id"],
            "access_revoked",
            document_id,
            "The owner revoked access to this document.",
        )
        audit(connection, user["id"], f"document.release_revoked:{document_id}:{recipient_id}")
    return {"message": "Access revoked. The recipient can no longer open this document.", "access_status": "revoked"}


@app.get("/api/medical-hold")
def get_medical_hold(user: dict = Depends(current_user)) -> dict:
    with db() as connection:
        hold = _medical_hold_state(connection, user["id"])
    return {"medical_hold": hold}


@app.put("/api/medical-hold")
def update_medical_hold(
    payload: MedicalHoldPayload, user: dict = Depends(current_user)
) -> dict:
    now = utc_now().isoformat()
    with db() as connection:
        hold = _medical_hold_state(connection, user["id"])
        if payload.action == "start":
            connection.execute(
                """INSERT INTO medical_holds(owner_id,status,started_at,ended_at,updated_at)
                   VALUES(?,'active',?,NULL,?)
                   ON CONFLICT(owner_id) DO UPDATE SET status='active',started_at=excluded.started_at,
                     ended_at=NULL,updated_at=excluded.updated_at""",
                (user["id"], now, now),
            )
            audit(connection, user["id"], "medical_hold.started")
        else:
            if hold["status"] != "active":
                raise HTTPException(
                    status_code=409,
                    detail="Only an active Medical Hold can be ended.",
                )
            connection.execute(
                """UPDATE medical_holds SET status='awaiting_checkin',ended_at=?,updated_at=?
                   WHERE owner_id=?""",
                (now, now, user["id"]),
            )
            audit(connection, user["id"], "medical_hold.ended_awaiting_checkin")
        updated = _medical_hold_state(connection, user["id"])
    return {"medical_hold": updated}


@app.get("/api/documents/{document_id}/download")
def download_document(document_id: int, user: dict = Depends(current_user)) -> Response:
    with db() as connection:
        row = connection.execute(
            "SELECT * FROM documents WHERE id=? AND owner_id=?", (document_id, user["id"])
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Document was not found.")
        audit(connection, user["id"], f"document.downloaded:{document_id}")
    return Response(
        content=row["encrypted_data"],
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{row["id"]}.cgenc"',
            "X-Content-Type-Options": "nosniff",
            "X-CloudGuard-Encryption": "AES-256-GCM",
            "Cache-Control": "private, no-store",
        },
    )


@app.delete("/api/documents/{document_id}", status_code=204)
def delete_document(document_id: int, user: dict = Depends(current_user)) -> None:
    with db() as connection:
        cursor = connection.execute(
            "DELETE FROM documents WHERE id=? AND owner_id=?", (document_id, user["id"])
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Document was not found.")
        audit(connection, user["id"], f"document.deleted:{document_id}")


@app.get("/api/recipients")
def list_recipients(user: dict = Depends(current_user)) -> dict:
    with db() as connection:
        rows = connection.execute(
            "SELECT id,name,email,relationship,created_at FROM recipients WHERE owner_id=? ORDER BY id DESC",
            (user["id"],),
        ).fetchall()
    return {"recipients": [dict(row) for row in rows]}


@app.post("/api/recipients", status_code=201)
def create_recipient(payload: RecipientPayload, user: dict = Depends(current_user)) -> dict:
    with db() as connection:
        cursor = connection.execute(
            "INSERT INTO recipients(owner_id,name,email,relationship,created_at) VALUES(?,?,?,?,?)",
            (user["id"], payload.name.strip(), payload.email.strip().lower(),
             payload.relationship.strip(), utc_now().isoformat()),
        )
        audit(connection, user["id"], f"recipient.added:{cursor.lastrowid}")
        row = connection.execute(
            "SELECT id,name,email,relationship,created_at FROM recipients WHERE id=?",
            (cursor.lastrowid,),
        ).fetchone()
    return {"recipient": dict(row)}


@app.patch("/api/recipients/{recipient_id}")
def update_recipient(
    recipient_id: int, payload: RecipientPayload, user: dict = Depends(current_user)
) -> dict:
    with db() as connection:
        revoke_target_invitations(connection, user["id"], "recipient", recipient_id)
        cursor = connection.execute(
            """UPDATE recipients SET name=?,email=?,relationship=?
               WHERE id=? AND owner_id=?""",
            (payload.name.strip(), payload.email.strip().lower(), payload.relationship.strip(),
             recipient_id, user["id"]),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Recipient was not found.")
        audit(connection, user["id"], f"recipient.updated:{recipient_id}")
        row = connection.execute(
            "SELECT id,name,email,relationship,created_at FROM recipients WHERE id=?",
            (recipient_id,),
        ).fetchone()
    return {"recipient": dict(row)}


@app.delete("/api/recipients/{recipient_id}", status_code=204)
def delete_recipient(recipient_id: int, user: dict = Depends(current_user)) -> None:
    with db() as connection:
        revoke_target_invitations(connection, user["id"], "recipient", recipient_id)
        cursor = connection.execute(
            "DELETE FROM recipients WHERE id=? AND owner_id=?", (recipient_id, user["id"])
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Recipient was not found.")
        audit(connection, user["id"], f"recipient.removed:{recipient_id}")


@app.get("/api/nominee")
def get_nominee(user: dict = Depends(current_user)) -> dict:
    with db() as connection:
        row = connection.execute(
            """SELECT id,name,email,relationship,phone,created_at,updated_at
               FROM nominees WHERE owner_id=?""",
            (user["id"],),
        ).fetchone()
    return {"nominee": dict(row) if row else None}


@app.put("/api/nominee")
def save_nominee(payload: NomineePayload, user: dict = Depends(current_user)) -> dict:
    now = utc_now().isoformat()
    with db() as connection:
        existing = connection.execute(
            "SELECT id FROM nominees WHERE owner_id=?", (user["id"],)
        ).fetchone()
        if existing:
            revoke_target_invitations(connection, user["id"], "nominee", existing["id"])
        connection.execute(
            """INSERT INTO nominees(owner_id,name,email,relationship,phone,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(owner_id) DO UPDATE SET
                   name=excluded.name,email=excluded.email,relationship=excluded.relationship,
                   phone=excluded.phone,updated_at=excluded.updated_at""",
            (user["id"], payload.name.strip(), payload.email.strip().lower(),
             payload.relationship.strip(), payload.phone.strip(), now, now),
        )
        nominee_id = existing["id"] if existing else connection.execute(
            "SELECT id FROM nominees WHERE owner_id=?", (user["id"],)
        ).fetchone()["id"]
        audit(connection, user["id"], f"nominee.{'updated' if existing else 'added'}:{nominee_id}")
        row = connection.execute(
            """SELECT id,name,email,relationship,phone,created_at,updated_at
               FROM nominees WHERE id=? AND owner_id=?""",
            (nominee_id, user["id"]),
        ).fetchone()
    return {"nominee": dict(row)}


@app.delete("/api/nominee", status_code=204)
def delete_nominee(user: dict = Depends(current_user)) -> None:
    with db() as connection:
        existing = connection.execute(
            "SELECT id FROM nominees WHERE owner_id=?", (user["id"],)
        ).fetchone()
        if existing:
            revoke_target_invitations(connection, user["id"], "nominee", existing["id"])
        cursor = connection.execute("DELETE FROM nominees WHERE owner_id=?", (user["id"],))
        if cursor.rowcount:
            audit(connection, user["id"], "nominee.removed")


@app.post("/api/check-ins/diagnose")
def diagnose_checkin(
    recording: UploadFile = File(...),
    user: dict = Depends(current_user),
):
    suffix = Path(recording.filename or "").suffix.lower()
    if suffix not in {".webm", ".mp4", ".mov"}:
        raise HTTPException(status_code=415, detail="Upload a WebM, MP4, or MOV clip.")
    data = recording.file.read(MAX_CHECKIN_BYTES + 1)
    if not data:
        raise HTTPException(status_code=400, detail="The recording is empty.")
    if len(data) > MAX_CHECKIN_BYTES:
        raise HTTPException(status_code=413, detail="Recordings must be 20 MB or smaller.")

    with tempfile.TemporaryDirectory(prefix="cloudguard-diag-") as directory:
        video_path = Path(directory) / f"recording{suffix}"
        video_path.write_bytes(data)
        result = diagnose_recording(video_path)
    return result


# --- Check-in Asynchronous Jobs Store ---
_CHECKIN_JOBS = {}
_CHECKIN_JOBS_LOCK = threading.Lock()
_JOB_EXPIRY_SECONDS = 900  # 15 minutes


def _cleanup_old_checkin_jobs():
    cutoff = time.time() - 3600  # 1 hour
    with _CHECKIN_JOBS_LOCK:
        expired = [jid for jid, info in _CHECKIN_JOBS.items() if info.get("created_at", 0) < cutoff]
        for jid in expired:
            _CHECKIN_JOBS.pop(jid, None)
    try:
        cutoff_iso = (utc_now() - timedelta(seconds=3600)).isoformat()
        with db() as connection:
            connection.execute("DELETE FROM checkin_jobs WHERE created_at < ?", (cutoff_iso,))
    except Exception as exc:
        logger.debug("Old checkin jobs DB cleanup note: %s", exc)


def _process_verification_and_db(
    user: dict,
    video_path: Path,
    enrollment_samples: list,
    sentence: str,
    t_start: float,
) -> dict:
    checkin_started_at = utc_now()
    statuses = verify_recording(video_path, enrollment_samples, sentence)
    required = {"face_status", "liveness_status", "speech_status"}
    if not isinstance(statuses, dict) or not required.issubset(statuses):
        raise VerificationUnavailable("Local verification produced an incomplete result.")
    logger.info("[CHECKIN] All verification checks completed")

    passed = all(statuses.get(check) == "passed" for check in ("face_status", "liveness_status", "speech_status"))
    checkin_status = "success" if passed else "failed"
    event = "checkin.verified_passed" if passed else "checkin.verified_failed"
    face_detail = statuses.get("face_detail") or ("matched" if statuses.get("face_status") == "passed" else "different_person")
    speech_detail = statuses.get("speech_detail") or ("matched" if statuses.get("speech_status") == "passed" else "mismatch")
    
    if not passed and statuses.get("face_status") != "passed":
        if face_detail == "different_person":
            message = "Face verification failed: this person does not match the enrolled face."
        elif face_detail == "no_face":
            message = "Face verification could not detect a clear face in the recording. Please retry in good lighting, keep your full face visible, and look directly at the camera."
        elif face_detail == "multiple_faces":
            message = "Multiple faces were detected. Please make sure only the account owner is visible."
        elif face_detail == "quality_low":
            message = "Face verification failed. Please retry in good lighting and keep your face clearly visible inside the camera frame."
        elif face_detail == "video_unusable":
            message = "Unable to process the recording. Please try the check-in again."
        else:
            message = "Face verification did not pass."
    elif not passed and statuses.get("liveness_status") != "passed":
        message = "Liveness verification failed: live movement could not be verified. Please keep your face visible and try again."
    elif not passed and statuses.get("speech_status") != "passed":
        if speech_detail == "no_audio":
            message = "Your recording did not contain microphone audio. Please allow microphone access and try again."
        elif speech_detail == "silent":
            message = "Voice verification failed: no speech was detected in the recording. Please speak the phrase aloud clearly."
        else:
            message = "Voice verification failed: spoken phrase did not match the expected phrase."
    elif passed:
        message = "Live check-in completed successfully."
    else:
        message = "One or more verification checks did not pass."

    if not passed:
        logger.warning("[CHECKIN ERROR] %s", message)

    t_db_start = time.perf_counter()
    logger.info("[CHECKIN] Database check-in being saved")
    with db() as connection:
        cursor = connection.execute(
            """INSERT INTO checkins(owner_id,mode,status,face_status,liveness_status,speech_status,sentence,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (user["id"], "local_ai", checkin_status, statuses["face_status"], statuses["liveness_status"],
             statuses["speech_status"], sentence, utc_now().isoformat()),
        )
        audit(connection, user["id"], f"{event}:{cursor.lastrowid}")
        hold = _medical_hold_state(connection, user["id"])
        now_str = utc_now().isoformat()
        next_deadline = None

        if passed:
            logger.info("[CHECKIN] all checks passed")
            user_row = connection.execute("SELECT checkin_frequency_seconds FROM users WHERE id=?", (user["id"],)).fetchone()
            freq_sec = user_row["checkin_frequency_seconds"] if user_row and user_row["checkin_frequency_seconds"] else 2592000
            next_deadline = (utc_now() + timedelta(seconds=freq_sec)).isoformat()
            
            # Reset owner state to ACTIVE and schedule next check-in
            connection.execute(
                """UPDATE users SET account_state='ACTIVE', last_checkin_at=?,
                          next_checkin_deadline=?, grace_period_started_at=NULL, grace_period_deadline=NULL
                   WHERE id=?""",
                (now_str, next_deadline, user["id"]),
            )
            
            # REQUIREMENT 12: Live Check-in Overrides Death Claim
            pending_cases = connection.execute(
                "SELECT id, nominee_id FROM death_cases WHERE owner_id=? AND status IN ('under_review', 'more_information_required')",
                (user["id"],),
            ).fetchall()
            for pc in pending_cases:
                connection.execute(
                    """UPDATE death_cases SET status='rejected',
                              reviewer_message='Case automatically invalidated: Account owner completed a verified live biometric check-in.',
                              reviewed_at=?, updated_at=?
                       WHERE id=?""",
                    (now_str, now_str, pc["id"]),
                )
                connection.execute(
                    """INSERT INTO nominee_case_events(nominee_id, owner_id, case_id, event_type, detail, created_at)
                       VALUES(?,?,?,'case_invalidated_by_owner_activity',?,?)""",
                    (pc["nominee_id"], user["id"], pc["id"], "Owner completed live biometric verification.", now_str),
                )
                audit(connection, user["id"], f"nominee.case_invalidated_by_owner_checkin:{pc['id']}")

            if (
                hold["status"] == "awaiting_checkin"
                and hold["ended_at"]
                and checkin_started_at >= safe_fromisoformat(hold["ended_at"])
            ):
                connection.execute(
                    "UPDATE medical_holds SET status='inactive',updated_at=? WHERE owner_id=?",
                    (utc_now().isoformat(), user["id"]),
                )
                audit(connection, user["id"], "medical_hold.fresh_checkin_completed")

            logger.info("[CHECKIN] Check-in saved")

        row = connection.execute("SELECT * FROM checkins WHERE id=?", (cursor.lastrowid,)).fetchone()
    t_db = time.perf_counter() - t_db_start

    t_timings = statuses.get("timings", {})
    t_face = float(t_timings.get("face", 0.0))
    t_liveness = float(t_timings.get("liveness", 0.0))
    t_speech = float(t_timings.get("speech", 0.0))
    t_total = time.perf_counter() - t_start

    logger.info(
        "[TIMING] face=%.3fs liveness=%.3fs speech=%.3fs database=%.3fs total=%.3fs",
        t_face, t_liveness, t_speech, t_db, t_total
    )

    checkin = dict(row)
    checkin["face_detail"] = face_detail

    face_match = (statuses.get("face_status") == "passed")
    liveness_passed = (statuses.get("liveness_status") == "passed")
    speech_match = (statuses.get("speech_status") == "passed")

    checks = {
        "face": statuses.get("face_status", "failed"),
        "liveness": statuses.get("liveness_status", "failed"),
        "voice": statuses.get("speech_status", "failed"),
    }

    if passed:
        return {
            "success": True,
            "passed": True,
            "status": "verified",
            "checkin_saved": True,
            "face_match": face_match,
            "liveness_passed": liveness_passed,
            "speech_match": speech_match,
            "message": "Live check-in completed successfully.",
            "checks": checks,
            "checkin": checkin,
            "checkin_time": now_str,
            "next_checkin_deadline": next_deadline,
        }
    else:
        return {
            "success": False,
            "passed": False,
            "status": "failed",
            "checkin_saved": True,
            "face_match": face_match,
            "liveness_passed": liveness_passed,
            "speech_match": speech_match,
            "message": message,
            "failure_reason": message,
            "checks": checks,
            "checkin": checkin,
        }


def _run_async_checkin_job(job_id: str, user: dict, sentence: str, suffix: str, data: bytes):
    t_start = time.perf_counter()
    logger.info("[CHECKIN JOB %s] Starting async check-in verification for owner %s", job_id, user["id"])
    with _CHECKIN_JOBS_LOCK:
        if job_id not in _CHECKIN_JOBS:
            return
        _CHECKIN_JOBS[job_id]["status"] = "processing"

    try:
        with db() as connection:
            owner = connection.execute(
                "SELECT face_enrollment FROM users WHERE id=?", (user["id"],)
            ).fetchone()
        if owner is None or owner["face_enrollment"] is None:
            err_msg = "Face enrollment is missing. Re-enroll your face before checking in."
            fail_result = {
                "success": False,
                "passed": False,
                "status": "failed",
                "checkin_saved": False,
                "message": err_msg,
                "checks": {"face": "failed", "liveness": "failed", "voice": "failed"},
            }
            with _CHECKIN_JOBS_LOCK:
                _CHECKIN_JOBS[job_id] = {
                    "job_id": job_id,
                    "user_id": user["id"],
                    "status": "failed",
                    "error": err_msg,
                    "result": fail_result,
                    "created_at": time.time(),
                    "updated_at": time.time(),
                }
            try:
                with db() as conn:
                    conn.execute(
                        "UPDATE checkin_jobs SET status='failed', result_json=?, error_message=?, updated_at=? WHERE id=?",
                        (json.dumps(fail_result), err_msg, utc_now().isoformat(), job_id),
                    )
            except Exception as e:
                logger.debug("checkin_jobs db update note: %s", e)
            return

        try:
            enrollment_samples = json.loads(decrypt_bytes(owner["face_enrollment"]))
        except Exception as error:
            err_msg = "Face enrollment data could not be read."
            fail_result = {
                "success": False,
                "passed": False,
                "status": "failed",
                "checkin_saved": False,
                "message": "Face enrollment data could not be read. Please contact support.",
                "checks": {"face": "failed", "liveness": "failed", "voice": "failed"},
            }
            with _CHECKIN_JOBS_LOCK:
                _CHECKIN_JOBS[job_id] = {
                    "job_id": job_id,
                    "user_id": user["id"],
                    "status": "failed",
                    "error": err_msg,
                    "result": fail_result,
                    "created_at": time.time(),
                    "updated_at": time.time(),
                }
            try:
                with db() as conn:
                    conn.execute(
                        "UPDATE checkin_jobs SET status='failed', result_json=?, error_message=?, updated_at=? WHERE id=?",
                        (json.dumps(fail_result), err_msg, utc_now().isoformat(), job_id),
                    )
            except Exception as e:
                logger.debug("checkin_jobs db update note: %s", e)
            return

        with tempfile.TemporaryDirectory(prefix="cloudguard-checkin-") as directory:
            video_path = Path(directory) / f"recording{suffix}"
            video_path.write_bytes(data)
            logger.info("[CHECKIN JOB %s] Temporary recording written (%d bytes)", job_id, len(data))
            try:
                result = _process_verification_and_db(user, video_path, enrollment_samples, sentence, t_start)
            finally:
                logger.info("[CHECKIN JOB %s] Temporary recording deleted", job_id)

        with _CHECKIN_JOBS_LOCK:
            _CHECKIN_JOBS[job_id] = {
                "job_id": job_id,
                "user_id": user["id"],
                "status": "completed",
                "result": result,
                "created_at": _CHECKIN_JOBS.get(job_id, {}).get("created_at", time.time()),
                "updated_at": time.time(),
            }
        try:
            with db() as conn:
                conn.execute(
                    "UPDATE checkin_jobs SET status='completed', result_json=?, updated_at=? WHERE id=?",
                    (json.dumps(result), utc_now().isoformat(), job_id),
                )
        except Exception as e:
            logger.debug("checkin_jobs db update note: %s", e)

        logger.info("[CHECKIN JOB %s] Finished successfully, passed=%s", job_id, result.get("passed"))

    except VerificationUnavailable as error:
        logger.warning("[CHECKIN JOB %s] Verification unavailable: %s", job_id, error)
        unavail_res = {
            "success": False,
            "checkin_saved": False,
            "verification_unavailable": True,
            "status": "verification_unavailable",
            "message": str(error) or "Live verification services are temporarily unavailable.",
            "checks": {
                "face": "unavailable",
                "liveness": "unavailable",
                "voice": "unavailable",
            },
        }
        with _CHECKIN_JOBS_LOCK:
            _CHECKIN_JOBS[job_id] = {
                "job_id": job_id,
                "user_id": user["id"],
                "status": "unavailable",
                "result": unavail_res,
                "created_at": _CHECKIN_JOBS.get(job_id, {}).get("created_at", time.time()),
                "updated_at": time.time(),
            }
        try:
            with db() as conn:
                conn.execute(
                    "UPDATE checkin_jobs SET status='unavailable', result_json=?, error_message=?, updated_at=? WHERE id=?",
                    (json.dumps(unavail_res), str(error), utc_now().isoformat(), job_id),
                )
        except Exception as e:
            logger.debug("checkin_jobs db update note: %s", e)

    except Exception as error:
        logger.exception("[CHECKIN JOB %s] Unexpected verification failure: %s", job_id, error)
        err_res = {
            "success": False,
            "checkin_saved": False,
            "verification_unavailable": True,
            "status": "verification_unavailable",
            "message": "Verification service is temporarily unavailable. No check-in was recorded. Please try again.",
            "checks": {
                "face": "unavailable",
                "liveness": "unavailable",
                "voice": "unavailable",
            },
        }
        with _CHECKIN_JOBS_LOCK:
            _CHECKIN_JOBS[job_id] = {
                "job_id": job_id,
                "user_id": user["id"],
                "status": "unavailable",
                "result": err_res,
                "created_at": _CHECKIN_JOBS.get(job_id, {}).get("created_at", time.time()),
                "updated_at": time.time(),
            }
        try:
            with db() as conn:
                conn.execute(
                    "UPDATE checkin_jobs SET status='unavailable', result_json=?, error_message=?, updated_at=? WHERE id=?",
                    (json.dumps(err_res), str(error), utc_now().isoformat(), job_id),
                )
        except Exception as e:
            logger.debug("checkin_jobs db update note: %s", e)
    finally:
        _cleanup_old_checkin_jobs()


@app.get("/api/check-ins/phrase")
@app.get("/api/checkin/phrase")
def get_checkin_phrase():
    import random
    return {"sentence": random.choice(list(CHECKIN_SENTENCES))}


@app.post("/api/check-ins/start", status_code=202)
@app.post("/api/checkin/start", status_code=202)
def start_checkin_job(
    sentence: str = Form(..., min_length=5, max_length=250),
    recording: UploadFile = File(...),
    user: dict = Depends(current_user),
):
    logger.info("[CHECKIN ASYNC] Start check-in job requested by owner: %s", user["id"])
    if sentence not in CHECKIN_SENTENCES:
        logger.warning("[CHECKIN ERROR] The check-in phrase is not valid.")
        raise HTTPException(status_code=422, detail="The check-in phrase is not valid. Start a new check-in and try again.")
    if not providers_available():
        logger.warning("[CHECKIN ERROR] Live verification dependencies are unavailable")
        return JSONResponse(
            status_code=503,
            content={
                "success": False,
                "checkin_saved": False,
                "verification_unavailable": True,
                "status": "verification_unavailable",
                "message": "Live verification services are temporarily unavailable.",
                "checks": {
                    "face": "unavailable",
                    "liveness": "unavailable",
                    "voice": "unavailable",
                },
            },
        )
    suffix = Path(recording.filename or "").suffix.lower()
    if suffix not in {".webm", ".mp4", ".mov"}:
        logger.warning("[CHECKIN ERROR] Unsupported recording format: %s", suffix)
        raise HTTPException(status_code=415, detail="Record a WebM or MP4 check-in clip.")
    data = recording.file.read(MAX_CHECKIN_BYTES + 1)
    if not data:
        logger.warning("[CHECKIN ERROR] The check-in recording is empty.")
        raise HTTPException(status_code=400, detail="The check-in recording is empty.")
    if len(data) > MAX_CHECKIN_BYTES:
        logger.warning("[CHECKIN ERROR] Recording exceeds 20MB limit")
        raise HTTPException(status_code=413, detail="Check-in recordings must be 20 MB or smaller.")

    content_type = recording.content_type or "unknown"
    logger.info("[CHECKIN ASYNC] Recording received: filename=%s, content_type=%s, size=%d bytes", recording.filename, content_type, len(data))

    with db() as connection:
        owner = connection.execute(
            "SELECT face_enrollment FROM users WHERE id=?", (user["id"],)
        ).fetchone()
    if owner is None or owner["face_enrollment"] is None:
        logger.warning("[CHECKIN ERROR] Face enrollment is missing for owner id=%s", user["id"])
        raise HTTPException(status_code=409, detail="Face enrollment is missing. Re-enroll your face before checking in.")

    job_id = str(uuid.uuid4())
    now_iso = utc_now().isoformat()
    with _CHECKIN_JOBS_LOCK:
        _CHECKIN_JOBS[job_id] = {
            "job_id": job_id,
            "user_id": user["id"],
            "status": "processing",
            "result": None,
            "error": None,
            "created_at": time.time(),
            "updated_at": time.time(),
        }

    try:
        with db() as connection:
            connection.execute(
                """INSERT INTO checkin_jobs(id, owner_id, status, result_json, error_message, created_at, updated_at)
                   VALUES(?, ?, 'processing', NULL, NULL, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET status='processing', updated_at=excluded.updated_at""",
                (job_id, user["id"], now_iso, now_iso),
            )
    except Exception as db_err:
        logger.warning("Could not persist initial checkin_job to PostgreSQL: %s", db_err)

    thread = threading.Thread(
        target=_run_async_checkin_job,
        args=(job_id, user, sentence, suffix, data),
        daemon=True,
    )
    thread.start()

    return {
        "job_id": job_id,
        "status": "processing",
        "message": "Analyzing face, liveness & voice...",
    }


@app.get("/api/check-ins/status/{job_id}")
@app.get("/api/checkin/status/{job_id}")
def get_checkin_job_status(job_id: str, user: dict = Depends(current_user)):
    job = None
    job_status = None
    result = None
    error_msg = None

    with _CHECKIN_JOBS_LOCK:
        mem_job = _CHECKIN_JOBS.get(job_id)
        if mem_job and mem_job.get("user_id") == user["id"]:
            job = mem_job
            job_status = mem_job.get("status", "processing")
            result = mem_job.get("result")
            error_msg = mem_job.get("error")

    # If not found in memory, or if status is still processing, check persistent database
    if not job or job_status == "processing":
        try:
            with db() as connection:
                db_row = connection.execute(
                    "SELECT status, result_json, error_message, created_at FROM checkin_jobs WHERE id=? AND owner_id=?",
                    (job_id, user["id"]),
                ).fetchone()
            if db_row:
                job_status = db_row["status"]
                result = json.loads(db_row["result_json"]) if db_row["result_json"] else None
                error_msg = db_row["error_message"]
                created_str = db_row["created_at"]

                # Stale/dead job detection: if in DB as processing but either:
                # 1) Not currently in this process's active in-memory tracker (server restarted), OR
                # 2) More than 75s elapsed since job creation (subprocess terminated or died)
                is_stale = False
                if job_status == "processing":
                    if mem_job is None:
                        is_stale = True
                    else:
                        try:
                            created_dt = safe_fromisoformat(created_str)
                            if (utc_now() - created_dt).total_seconds() > 75:
                                is_stale = True
                        except Exception:
                            pass

                if is_stale:
                    logger.warning("[CHECKIN STATUS] Marking dead/stale job %s as failed (created %s)", job_id, created_str)
                    job_status = "failed"
                    error_msg = "Verification was interrupted or timed out. Please try your check-in again."
                    result = {
                        "success": False,
                        "passed": False,
                        "status": "failed",
                        "checkin_saved": False,
                        "message": error_msg,
                        "failure_reason": error_msg,
                        "checks": {"face": "failed", "liveness": "failed", "voice": "failed"},
                    }
                    try:
                        with db() as conn2:
                            conn2.execute(
                                "UPDATE checkin_jobs SET status='failed', result_json=?, error_message=?, updated_at=? WHERE id=?",
                                (json.dumps(result), error_msg, utc_now().isoformat(), job_id),
                            )
                    except Exception as e:
                        logger.debug("checkin_jobs db update note: %s", e)

                with _CHECKIN_JOBS_LOCK:
                    _CHECKIN_JOBS[job_id] = {
                        "job_id": job_id,
                        "user_id": user["id"],
                        "status": job_status,
                        "result": result,
                        "error": error_msg,
                        "updated_at": time.time(),
                    }
        except Exception as query_err:
            logger.warning("Check-in job DB status lookup warning: %s", query_err)

    if not job_status:
        raise HTTPException(status_code=404, detail="Check-in job not found or expired.")

    if job_status == "processing":
        return {
            "job_id": job_id,
            "status": "processing",
            "message": "Analyzing face, liveness & voice...",
        }
    elif job_status == "completed":
        return result
    elif job_status == "unavailable":
        return JSONResponse(status_code=503, content=result or {
            "success": False,
            "checkin_saved": False,
            "verification_unavailable": True,
            "status": "verification_unavailable",
            "message": "Verification service is temporarily unavailable. No check-in was recorded. Please try again.",
            "checks": {"face": "unavailable", "liveness": "unavailable", "voice": "unavailable"},
        })
    elif job_status == "failed":
        if result:
            return result
        return JSONResponse(status_code=422, content={
            "success": False,
            "passed": False,
            "status": "failed",
            "checkin_saved": False,
            "message": error_msg or "Verification failed. Please try again.",
            "failure_reason": error_msg or "Verification failed. Please try again.",
            "checks": {"face": "failed", "liveness": "failed", "voice": "failed"},
        })
    return {"job_id": job_id, "status": job_status}


@app.post("/api/check-ins", status_code=201)
def create_checkin(
    sentence: str = Form(..., min_length=5, max_length=250),
    recording: UploadFile = File(...),
    async_mode: bool = False,
    user: dict = Depends(current_user),
):
    if async_mode:
        return start_checkin_job(sentence=sentence, recording=recording, user=user)

    t_start = time.perf_counter()
    logger.info("[CHECKIN] Request received")
    logger.info("[CHECKIN] Authentication verified")
    logger.info("[CHECKIN] authenticated owner: %s", user["id"])
    if sentence not in CHECKIN_SENTENCES:
        logger.warning("[CHECKIN ERROR] The check-in phrase is not valid.")
        raise HTTPException(status_code=422, detail="The check-in phrase is not valid. Start a new check-in and try again.")
    if not providers_available():
        logger.warning("[CHECKIN ERROR] Live verification dependencies are unavailable")
        return JSONResponse(
            status_code=503,
            content={
                "success": False,
                "checkin_saved": False,
                "verification_unavailable": True,
                "status": "verification_unavailable",
                "message": "Live verification services are temporarily unavailable.",
                "checks": {
                    "face": "unavailable",
                    "liveness": "unavailable",
                    "voice": "unavailable",
                },
            },
        )
    suffix = Path(recording.filename or "").suffix.lower()
    if suffix not in {".webm", ".mp4", ".mov"}:
        logger.warning("[CHECKIN ERROR] Unsupported recording format: %s", suffix)
        raise HTTPException(status_code=415, detail="Record a WebM or MP4 check-in clip.")
    data = recording.file.read(MAX_CHECKIN_BYTES + 1)
    if not data:
        logger.warning("[CHECKIN ERROR] The check-in recording is empty.")
        raise HTTPException(status_code=400, detail="The check-in recording is empty.")
    if len(data) > MAX_CHECKIN_BYTES:
        logger.warning("[CHECKIN ERROR] Recording exceeds 20MB limit")
        raise HTTPException(status_code=413, detail="Check-in recordings must be 20 MB or smaller.")
    
    content_type = recording.content_type or "unknown"
    logger.info("[CHECKIN] Recording received: filename=%s, content_type=%s, size=%d bytes", recording.filename, content_type, len(data))
    print(f"[CHECKIN] Recording received: filename={recording.filename}, content_type={content_type}, size={len(data)} bytes", flush=True)

    with db() as connection:
        owner = connection.execute(
            "SELECT face_enrollment FROM users WHERE id=?", (user["id"],)
        ).fetchone()
    if owner is None or owner["face_enrollment"] is None:
        logger.warning("[CHECKIN ERROR] Face enrollment is missing for owner id=%s", user["id"])
        raise HTTPException(status_code=409, detail="Face enrollment is missing. Re-enroll your face before checking in.")
    try:
        enrollment_samples = json.loads(decrypt_bytes(owner["face_enrollment"]))
    except (ValueError, TypeError) as error:
        logger.warning("[CHECKIN ERROR] Face enrollment data could not be decrypted")
        raise HTTPException(status_code=409, detail="Face enrollment data could not be read. Please contact support.") from error
    
    with tempfile.TemporaryDirectory(prefix="cloudguard-checkin-") as directory:
        video_path = Path(directory) / f"recording{suffix}"
        video_path.write_bytes(data)
        logger.info("[CHECKIN] Temporary file created")
        try:
            result = _process_verification_and_db(user, video_path, enrollment_samples, sentence, t_start)
        except ValueError as error:
            logger.warning("[CHECKIN ERROR] %s", error)
            raise HTTPException(status_code=422, detail=str(error)) from error
        except VerificationUnavailable as error:
            logger.warning("[CHECKIN ERROR] %s", error)
            return JSONResponse(
                status_code=503,
                content={
                    "success": False,
                    "checkin_saved": False,
                    "verification_unavailable": True,
                    "status": "verification_unavailable",
                    "message": str(error) or "Live verification services are temporarily unavailable.",
                    "checks": {
                        "face": "unavailable",
                        "liveness": "unavailable",
                        "voice": "unavailable",
                    },
                },
            )
        except Exception as error:
            logger.exception("[CHECKIN ERROR] Local check-in verification failed unexpectedly.")
            return JSONResponse(
                status_code=503,
                content={
                    "success": False,
                    "checkin_saved": False,
                    "verification_unavailable": True,
                    "status": "verification_unavailable",
                    "message": "Local verification could not complete. No result was saved; check the API log and try again.",
                    "checks": {
                        "face": "unavailable",
                        "liveness": "unavailable",
                        "voice": "unavailable",
                    },
                },
            )
        finally:
            logger.info("[CHECKIN] Temporary recording deleted")

    logger.info("[CHECKIN] Response returned")
    return result


@app.get("/api/check-ins")
def list_checkins(user: dict = Depends(current_user)) -> dict:
    with db() as connection:
        rows = connection.execute(
            """SELECT id,mode,status,face_status,liveness_status,speech_status,sentence,created_at
               FROM checkins WHERE owner_id=? ORDER BY id DESC LIMIT 100""",
            (user["id"],),
        ).fetchall()
    return {"checkins": [dict(row) for row in rows]}


@app.patch("/api/profile")
def update_profile(payload: ProfilePayload, user: dict = Depends(current_user)) -> dict:
    with db() as connection:
        connection.execute(
            "UPDATE users SET name=?,phone=?,checkin_interval=? WHERE id=?",
            (payload.name.strip(), payload.phone.strip(), payload.checkin_interval, user["id"]),
        )
        audit(connection, user["id"], "profile.updated")
        updated = connection.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
    return {"user": public_user(updated)}


@app.get("/api/reviewer/cases/{case_id}")
def reviewer_case_detail(case_id: int, reviewer: dict = Depends(reviewer_principal)) -> dict:
    with db() as connection:
        case = connection.execute(
            """SELECT death_cases.*,
                      nominees.name AS nominee_name, nominees.email AS nominee_email, nominees.phone AS nominee_phone,
                      users.name AS owner_name, users.email AS owner_email, users.date_of_birth AS owner_dob,
                      users.account_state AS owner_account_state, users.last_checkin_at AS owner_last_checkin_at
               FROM death_cases
               JOIN nominees ON nominees.id=death_cases.nominee_id
               JOIN users ON users.id=death_cases.owner_id
               WHERE death_cases.id=?""",
            (case_id,),
        ).fetchone()
        if case is None:
            raise HTTPException(status_code=404, detail="Case was not found.")
        
        c_dict = dict(case)
        c_dict.pop("evidence_ciphertext", None)
        try:
            c_dict["discrepancies"] = json.loads(c_dict.get("discrepancies_json") or "[]")
        except Exception:
            c_dict["discrepancies"] = []
        return {"case": c_dict}


@app.get("/api/owner/security-settings")
def get_security_settings(user: dict = Depends(current_user)) -> dict:
    with db() as connection:
        row = connection.execute(
            """SELECT checkin_frequency_value, checkin_frequency_unit, checkin_frequency_seconds,
                      grace_period_value, grace_period_unit, grace_period_seconds,
                      date_of_birth, identity_status, account_state,
                      last_checkin_at, next_checkin_deadline, grace_period_deadline
               FROM users WHERE id=?""",
            (user["id"],),
        ).fetchone()
    return {"settings": dict(row) if row else {}}


@app.put("/api/owner/security-settings")
def update_security_settings(
    payload: SecuritySettingsPayload,
    user: dict = Depends(current_user),
) -> dict:
    freq_sec = normalize_time_to_seconds(payload.checkin_frequency_value, payload.checkin_frequency_unit)
    grace_sec = normalize_time_to_seconds(payload.grace_period_value, payload.grace_period_unit)
    
    now_dt = utc_now()
    next_deadline = (now_dt + timedelta(seconds=freq_sec)).isoformat()
    interval_days = max(1, freq_sec // 86400)
    
    with db() as connection:
        connection.execute(
            """UPDATE users SET
                      checkin_frequency_value=?, checkin_frequency_unit=?, checkin_frequency_seconds=?,
                      grace_period_value=?, grace_period_unit=?, grace_period_seconds=?,
                      checkin_interval=?, next_checkin_deadline=?,
                      date_of_birth=COALESCE(NULLIF(?, ''), date_of_birth)
               WHERE id=?""",
            (
                payload.checkin_frequency_value,
                payload.checkin_frequency_unit.lower(),
                freq_sec,
                payload.grace_period_value,
                payload.grace_period_unit.lower(),
                grace_sec,
                interval_days,
                next_deadline,
                payload.date_of_birth or "",
                user["id"],
            ),
        )
        audit(connection, user["id"], f"owner.security_settings_updated:{freq_sec}:{grace_sec}")
        updated = connection.execute(
            """SELECT checkin_frequency_value, checkin_frequency_unit, checkin_frequency_seconds,
                      grace_period_value, grace_period_unit, grace_period_seconds,
                      date_of_birth, identity_status, account_state,
                      last_checkin_at, next_checkin_deadline, grace_period_deadline
               FROM users WHERE id=?""",
            (user["id"],),
        ).fetchone()
        
        # Mirror to Supabase
        supabase_sync.sync_update_sync("users", "id", user["id"], {
            "checkin_frequency_value": payload.checkin_frequency_value,
            "checkin_frequency_unit": payload.checkin_frequency_unit.lower(),
            "checkin_frequency_seconds": freq_sec,
            "grace_period_value": payload.grace_period_value,
            "grace_period_unit": payload.grace_period_unit.lower(),
            "grace_period_seconds": grace_sec,
            "checkin_interval": interval_days,
            "next_checkin_deadline": next_deadline,
        })
    return {"settings": dict(updated), "message": "Security settings updated successfully."}


@app.get("/api/owner/audit-logs")
def get_owner_audit_logs(user: dict = Depends(current_user)) -> dict:
    with db() as connection:
        rows = connection.execute(
            """SELECT id, event, created_at, previous_hash, event_hash
               FROM audit_logs
               WHERE owner_id=? OR owner_id IS NULL
               ORDER BY id DESC LIMIT 100""",
            (user["id"],),
        ).fetchall()
    return {"logs": [dict(r) for r in rows]}


@app.get("/api/owner/checkin-status")
def get_owner_checkin_status(user: dict = Depends(current_user)) -> dict:
    now = utc_now()
    with db() as connection:
        row = connection.execute(
            """SELECT account_state, last_checkin_at, next_checkin_deadline,
                      grace_period_started_at, grace_period_deadline,
                      checkin_frequency_value, checkin_frequency_unit, checkin_frequency_seconds,
                      grace_period_value, grace_period_unit, grace_period_seconds
               FROM users WHERE id=?""",
            (user["id"],),
        ).fetchone()
        u = dict(row) if row else {}
        rem_checkin = None
        rem_grace = None
        if u.get("next_checkin_deadline"):
            d_dt = safe_fromisoformat(u["next_checkin_deadline"])
            rem_checkin = max(0, int((d_dt - now).total_seconds()))
        if u.get("grace_period_deadline"):
            g_dt = safe_fromisoformat(u["grace_period_deadline"])
            rem_grace = max(0, int((g_dt - now).total_seconds()))
        return {
            "status": u.get("account_state", "ACTIVE"),
            "account_state": u.get("account_state", "ACTIVE"),
            "last_checkin_at": u.get("last_checkin_at"),
            "next_checkin_deadline": u.get("next_checkin_deadline"),
            "grace_period_deadline": u.get("grace_period_deadline"),
            "remaining_checkin_seconds": rem_checkin,
            "remaining_grace_seconds": rem_grace,
            "checkin_frequency_value": u.get("checkin_frequency_value", 30),
            "checkin_frequency_unit": u.get("checkin_frequency_unit", "days"),
            "grace_period_value": u.get("grace_period_value", 72),
            "grace_period_unit": u.get("grace_period_unit", "hours"),
        }
