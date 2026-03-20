import os
import sqlite3
from dataclasses import dataclass
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken


@dataclass(frozen=True)
class RegisteredUser:
    tg_user_id: int
    phone_e164: str
    fio: str
    email: str
    ews_username: str
    ews_password: str


def _get_db_path() -> str:
    data_dir = os.environ.get("DATA_DIR", "/app/data")
    return os.path.join(data_dir, "app.db")


def _get_fernet() -> Fernet:
    key = os.environ.get("DATA_ENCRYPTION_KEY", "").strip()
    if not key:
        raise RuntimeError("DATA_ENCRYPTION_KEY is not set (required)")
    return Fernet(key.encode("utf-8"))


def init_db() -> None:
    db_path = _get_db_path()
    with sqlite3.connect(db_path) as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
              tg_user_id INTEGER PRIMARY KEY,
              phone_e164 TEXT NOT NULL,
              fio TEXT NOT NULL,
              email TEXT NOT NULL,
              ews_username TEXT NOT NULL,
              ews_password_enc TEXT NOT NULL,
              created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_users_phone ON users(phone_e164)")
        con.commit()


def upsert_user(
    *,
    tg_user_id: int,
    phone_e164: str,
    fio: str,
    email: str,
    ews_username: str,
    ews_password: str,
) -> None:
    f = _get_fernet()
    enc = f.encrypt(ews_password.encode("utf-8")).decode("utf-8")
    with sqlite3.connect(_get_db_path()) as con:
        con.execute(
            """
            INSERT INTO users(tg_user_id, phone_e164, fio, email, ews_username, ews_password_enc)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(tg_user_id) DO UPDATE SET
              phone_e164=excluded.phone_e164,
              fio=excluded.fio,
              email=excluded.email,
              ews_username=excluded.ews_username,
              ews_password_enc=excluded.ews_password_enc
            """,
            (tg_user_id, phone_e164, fio, email, ews_username, enc),
        )
        con.commit()


def get_user(tg_user_id: int) -> Optional[RegisteredUser]:
    with sqlite3.connect(_get_db_path()) as con:
        row = con.execute(
            """
            SELECT tg_user_id, phone_e164, fio, email, ews_username, ews_password_enc
            FROM users WHERE tg_user_id=?
            """,
            (tg_user_id,),
        ).fetchone()
    if not row:
        return None
    f = _get_fernet()
    try:
        pwd = f.decrypt(row[5].encode("utf-8")).decode("utf-8")
    except InvalidToken:
        raise RuntimeError("Stored password cannot be decrypted. DATA_ENCRYPTION_KEY changed?")
    return RegisteredUser(
        tg_user_id=int(row[0]),
        phone_e164=str(row[1]),
        fio=str(row[2]),
        email=str(row[3]),
        ews_username=str(row[4]),
        ews_password=pwd,
    )


def is_registered(tg_user_id: int) -> bool:
    return get_user(tg_user_id) is not None