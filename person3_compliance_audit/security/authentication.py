
"""
Authentication Service

Provides:
1. Trusted user registration
2. Secure password hashing
3. Password verification
4. Session creation
5. Session validation
6. Session expiry
7. Logout and session revocation
8. Account deactivation
9. Administrator-only account provisioning

Roles are stored in the identity database.
The login caller cannot choose or override their role.
"""

import hashlib
import hmac
import secrets
import sqlite3
import uuid

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path


# --------------------------------------------------
# DATABASE CONFIGURATION
# --------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BASE_DIR / "identity.db"

VALID_ROLES = {
    "CUSTOMER",
    "REGULATOR",
    "ADMIN"
}

PBKDF2_ITERATIONS = 600_000


# --------------------------------------------------
# AUTHENTICATION SERVICE
# --------------------------------------------------

class AuthenticationService:

    def __init__(self, db_path=None, session_hours=8):

        if session_hours <= 0:
            raise ValueError("Session duration must be positive")

        self.db_path = Path(db_path or DEFAULT_DB_PATH)

        self.db_path.parent.mkdir(
            parents=True,
            exist_ok=True
        )

        self.session_hours = session_hours

        self.create_tables()

    # --------------------------------------------------
    # DATABASE CONNECTION
    # --------------------------------------------------

    @contextmanager
    def get_connection(self):

        conn = sqlite3.connect(
            self.db_path,
            timeout=30
        )

        conn.row_factory = sqlite3.Row

        conn.execute("PRAGMA foreign_keys = ON")

        try:
            yield conn
            conn.commit()

        except Exception:
            conn.rollback()
            raise

        finally:
            conn.close()

    # --------------------------------------------------
    # CREATE DATABASE TABLES
    # --------------------------------------------------

    def create_tables(self):

        with self.get_connection() as conn:

            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (

                    user_id TEXT PRIMARY KEY,

                    password_hash TEXT NOT NULL,

                    salt TEXT NOT NULL,

                    role TEXT NOT NULL
                        CHECK(role IN (
                            'CUSTOMER',
                            'REGULATOR',
                            'ADMIN'
                        )),

                    is_active INTEGER NOT NULL DEFAULT 1,

                    created_at TEXT NOT NULL
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (

                    session_id TEXT PRIMARY KEY,

                    user_id TEXT NOT NULL,

                    token_hash TEXT NOT NULL UNIQUE,

                    created_at TEXT NOT NULL,

                    expires_at TEXT NOT NULL,

                    revoked INTEGER NOT NULL DEFAULT 0,

                    FOREIGN KEY(user_id)
                        REFERENCES users(user_id)
                )
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS
                idx_sessions_user_id
                ON sessions(user_id)
            """)

    # --------------------------------------------------
    # PASSWORD HASHING
    # --------------------------------------------------

    @staticmethod
    def _hash_password(password, salt):

        return hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            PBKDF2_ITERATIONS
        ).hex()

    # --------------------------------------------------
    # SESSION TOKEN HASHING
    # --------------------------------------------------

    @staticmethod
    def _hash_token(token):

        return hashlib.sha256(
            token.encode("utf-8")
        ).hexdigest()

    # --------------------------------------------------
    # INTERNAL USER CREATION
    # --------------------------------------------------

    def _insert_user(self, conn, user_id, password, role):
        """Insert a user into the identity database."""

        if not user_id or not str(user_id).strip():
            raise ValueError("User ID is required")

        if not isinstance(password, str) or len(password) < 12:
            raise ValueError(
                "Password must contain at least 12 characters"
            )

        role = str(role).strip().upper()

        if role not in VALID_ROLES:
            raise ValueError("Invalid role")

        user_id = str(user_id).strip()

        salt = secrets.token_bytes(16)

        password_hash = self._hash_password(
            password,
            salt
        )

        try:

            conn.execute("""
                INSERT INTO users (
                    user_id,
                    password_hash,
                    salt,
                    role,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?)
            """, (
                user_id,
                password_hash,
                salt.hex(),
                role,
                datetime.now(timezone.utc).isoformat()
            ))

        except sqlite3.IntegrityError as exc:

            raise ValueError(
                "User already exists or registration is invalid"
            ) from exc

        return {
            "registered": True,
            "user_id": user_id,
            "role": role
        }

    # --------------------------------------------------
    # FIRST ADMINISTRATOR BOOTSTRAP
    # --------------------------------------------------

    def bootstrap_admin(self, user_id, password):
        """
        Create the first administrator.

        This operation works only when the identity database
        contains no existing users.

        It cannot be used to create additional administrators.
        """

        with self.get_connection() as conn:

            # Serialize bootstrap attempts.
            conn.execute("BEGIN IMMEDIATE")

            user_count = conn.execute(
                "SELECT COUNT(*) FROM users"
            ).fetchone()[0]

            if user_count != 0:

                raise PermissionError(
                    "Administrator bootstrap is already closed"
                )

            return self._insert_user(
                conn,
                user_id,
                password,
                "ADMIN"
            )

    # --------------------------------------------------
    # ADMINISTRATOR-ONLY USER CREATION
    # --------------------------------------------------

    def create_user(
        self,
        session_token,
        user_id,
        password,
        role
    ):
        """
        Create a user through an authenticated administrator.

        Only an active ADMIN account with a valid session
        can create CUSTOMER, REGULATOR or ADMIN accounts.
        """

        identity = self.authenticate_session(session_token)

        if identity is None:

            raise PermissionError(
                "Valid administrator session required"
            )

        if identity["role"] != "ADMIN":

            raise PermissionError(
                "Only administrators can create accounts"
            )

        with self.get_connection() as conn:

            conn.execute("BEGIN IMMEDIATE")

            # Recheck administrator status in the database.
            admin = conn.execute("""
                SELECT role, is_active
                FROM users
                WHERE user_id = ?
            """, (
                identity["user_id"],
            )).fetchone()

            if (
                admin is None
                or admin["role"] != "ADMIN"
                or not admin["is_active"]
            ):

                raise PermissionError(
                    "Administrator account is not active"
                )

            return self._insert_user(
                conn,
                user_id,
                password,
                role
            )

    # --------------------------------------------------
    # USER LOGIN
    # --------------------------------------------------

    def login(self, user_id, password):

        if (
            not user_id
            or not isinstance(password, str)
            or not password
        ):

            return {
                "authenticated": False,
                "reason": "Invalid credentials"
            }

        with self.get_connection() as conn:

            user = conn.execute("""
                SELECT *
                FROM users
                WHERE user_id = ?
            """, (
                str(user_id).strip(),
            )).fetchone()

        if user is None or not user["is_active"]:

            return {
                "authenticated": False,
                "reason": "Invalid credentials"
            }

        salt = bytes.fromhex(user["salt"])

        calculated_hash = self._hash_password(
            password,
            salt
        )

        # Constant-time password hash comparison.
        if not hmac.compare_digest(
            calculated_hash,
            user["password_hash"]
        ):

            return {
                "authenticated": False,
                "reason": "Invalid credentials"
            }

        # Generate a secure random session token.
        token = secrets.token_urlsafe(32)

        now = datetime.now(timezone.utc)

        expires = now + timedelta(
            hours=self.session_hours
        )

        session_id = str(uuid.uuid4())

        token_hash = self._hash_token(token)

        with self.get_connection() as conn:

            conn.execute("""
                INSERT INTO sessions (
                    session_id,
                    user_id,
                    token_hash,
                    created_at,
                    expires_at
                )
                VALUES (?, ?, ?, ?, ?)
            """, (
                session_id,
                user["user_id"],
                token_hash,
                now.isoformat(),
                expires.isoformat()
            ))

        return {
            "authenticated": True,
            "user_id": user["user_id"],
            "role": user["role"],
            "session_token": token,
            "expires_at": expires.isoformat()
        }

    # --------------------------------------------------
    # AUTHENTICATE SESSION
    # --------------------------------------------------

    def authenticate_session(self, token):

        if not token or not isinstance(token, str):
            return None

        token_hash = self._hash_token(token)

        with self.get_connection() as conn:

            session = conn.execute("""
                SELECT
                    sessions.user_id,
                    sessions.expires_at,
                    sessions.revoked,
                    users.role,
                    users.is_active

                FROM sessions

                JOIN users
                ON users.user_id = sessions.user_id

                WHERE sessions.token_hash = ?
            """, (
                token_hash,
            )).fetchone()

        if session is None:
            return None

        # Reject revoked sessions.
        if session["revoked"]:
            return None

        # Reject disabled users.
        if not session["is_active"]:
            return None

        expires = datetime.fromisoformat(
            session["expires_at"]
        )

        # Reject expired sessions.
        if datetime.now(timezone.utc) >= expires:
            return None

        # Identity and role come from the database.
        return {
            "user_id": session["user_id"],
            "role": session["role"]
        }

    # --------------------------------------------------
    # USER LOGOUT
    # --------------------------------------------------

    def logout(self, token):

        if not token or not isinstance(token, str):
            return False

        token_hash = self._hash_token(token)

        with self.get_connection() as conn:

            cursor = conn.execute("""
                UPDATE sessions
                SET revoked = 1
                WHERE token_hash = ?
                AND revoked = 0
            """, (
                token_hash,
            ))

        return cursor.rowcount == 1

    # --------------------------------------------------
    # DEACTIVATE USER
    # --------------------------------------------------

    def deactivate_user(self, user_id):

        if not user_id:
            return False

        user_id = str(user_id).strip()

        with self.get_connection() as conn:

            cursor = conn.execute("""
                UPDATE users
                SET is_active = 0
                WHERE user_id = ?
                AND is_active = 1
            """, (
                user_id,
            ))

            deactivated = cursor.rowcount == 1

            if deactivated:

                conn.execute("""
                    UPDATE sessions
                    SET revoked = 1
                    WHERE user_id = ?
                """, (
                    user_id,
                ))

        return deactivated
