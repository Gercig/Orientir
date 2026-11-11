"""Учётные записи (клиники, администратор, врачи), пациенты врачей, их анализы и журнал действий."""
import os
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from werkzeug.security import check_password_hash, generate_password_hash

DATA_DIR = os.environ.get("ORIENTIR_DATA", "/data")
DB_PATH = os.path.join(DATA_DIR, "orientir.db")
ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_DUMMY_HASH = generate_password_hash("dummy-password-for-timing")


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")


def db():
    os.makedirs(DATA_DIR, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init():
    con = db()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS clinics(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT NOT NULL UNIQUE,
      active INTEGER NOT NULL DEFAULT 1,
      created TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS users(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      login TEXT NOT NULL UNIQUE,
      password_hash TEXT NOT NULL,
      role TEXT NOT NULL CHECK(role IN ('admin','doctor')),
      clinic_id INTEGER REFERENCES clinics(id),
      active INTEGER NOT NULL DEFAULT 1,
      created TEXT NOT NULL,
      last_login TEXT);
    CREATE TABLE IF NOT EXISTS patients(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      clinic_id INTEGER NOT NULL REFERENCES clinics(id),
      doctor_id INTEGER NOT NULL REFERENCES users(id),
      card TEXT NOT NULL,
      full_name TEXT NOT NULL,
      sex TEXT NOT NULL CHECK(sex IN ('f','m')),
      birth TEXT NOT NULL,
      note TEXT NOT NULL DEFAULT '',
      created TEXT NOT NULL,
      UNIQUE(clinic_id, card));
    CREATE TABLE IF NOT EXISTS analyses(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      patient_id INTEGER NOT NULL REFERENCES patients(id) ON DELETE CASCADE,
      taken_on TEXT NOT NULL,
      lab TEXT NOT NULL DEFAULT '',
      created_by INTEGER REFERENCES users(id),
      created TEXT NOT NULL,
      ai_text TEXT,
      ai_kind TEXT);
    CREATE TABLE IF NOT EXISTS analysis_values(
      analysis_id INTEGER NOT NULL REFERENCES analyses(id) ON DELETE CASCADE,
      ind_id INTEGER NOT NULL,
      value REAL NOT NULL,
      lower REAL NOT NULL,
      upper REAL NOT NULL,
      PRIMARY KEY(analysis_id, ind_id));
    CREATE INDEX IF NOT EXISTS an_pat ON analyses(patient_id, taken_on);
    CREATE TABLE IF NOT EXISTS audit(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      ts TEXT NOT NULL,
      login TEXT NOT NULL,
      action TEXT NOT NULL,
      detail TEXT NOT NULL DEFAULT '');
    """)
    login = os.environ.get("ORIENTIR_ADMIN_LOGIN", "admin").strip() or "admin"
    pw = os.environ.get("ORIENTIR_ADMIN_PASSWORD", "")
    has_admin = con.execute("SELECT 1 FROM users WHERE role='admin' LIMIT 1").fetchone()
    if not has_admin and pw and not pw.startswith("PASTE"):
        con.execute("INSERT OR IGNORE INTO users(login,password_hash,role,created) VALUES(?,?, 'admin', ?)",
                    (login, generate_password_hash(pw), now()))
    con.commit()
    con.close()


def gen_password(n=10):
    return "".join(secrets.choice(ALPHABET) for _ in range(n))


def secret_key():
    """Секрет для подписи сессий хранится на диске данных, чтобы сессии переживали перезапуск."""
    env = os.environ.get("ORIENTIR_SECRET", "")
    if env:
        return env
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, "secret.key")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_hex(32))
    except FileExistsError:
        pass
    for _ in range(20):
        with open(path) as f:
            v = f.read().strip()
        if v:
            return v
        time.sleep(0.05)
    return secrets.token_hex(32)


def authenticate(login, password):
    con = db()
    row = con.execute("""SELECT u.*, c.name AS clinic_name, c.active AS clinic_active
                         FROM users u LEFT JOIN clinics c ON c.id=u.clinic_id
                         WHERE u.login=?""", (login,)).fetchone()
    ok = check_password_hash(row["password_hash"] if row else _DUMMY_HASH, password)
    if not (row and ok and row["active"] and (row["role"] == "admin" or row["clinic_active"])):
        con.close()
        return None
    con.execute("UPDATE users SET last_login=? WHERE id=?", (now(), row["id"]))
    con.commit()
    con.close()
    return dict(row)


def get_user(uid):
    con = db()
    row = con.execute("""SELECT u.id,u.login,u.role,u.clinic_id,u.active,c.name AS clinic_name,c.active AS clinic_active
                         FROM users u LEFT JOIN clinics c ON c.id=u.clinic_id WHERE u.id=?""", (uid,)).fetchone()
    con.close()
    if row and row["active"] and (row["role"] == "admin" or row["clinic_active"]):
        return dict(row)
    return None


def set_password(uid, new_password):
    con = db()
    con.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(new_password), uid))
    con.commit()
    con.close()


def check_current(uid, password):
    con = db()
    row = con.execute("SELECT password_hash FROM users WHERE id=?", (uid,)).fetchone()
    con.close()
    return bool(row and check_password_hash(row["password_hash"], password))


def clinics_with_doctors():
    con = db()
    clinics = [dict(r) for r in con.execute("SELECT * FROM clinics ORDER BY name")]
    for c in clinics:
        c["doctors"] = [dict(r) for r in con.execute(
            "SELECT u.id,u.login,u.active,u.created,u.last_login,"
            "(SELECT COUNT(*) FROM patients p WHERE p.doctor_id=u.id) AS patients "
            "FROM users u WHERE u.role='doctor' AND u.clinic_id=? ORDER BY u.login", (c["id"],))]
        c["patients"] = sum(d["patients"] for d in c["doctors"])
    con.close()
    return clinics


def add_clinic(name):
    con = db()
    try:
        con.execute("INSERT INTO clinics(name,created) VALUES(?,?)", (name, now()))
        con.commit()
        return None
    except sqlite3.IntegrityError:
        return "Клиника с таким названием уже есть"
    finally:
        con.close()


def next_login(con, clinic_id):
    n = con.execute("SELECT COUNT(*) FROM users WHERE role='doctor'").fetchone()[0] + 1
    while True:
        login = f"doctor-{n:03d}"
        if not con.execute("SELECT 1 FROM users WHERE login=?", (login,)).fetchone():
            return login
        n += 1


def add_doctor(clinic_id, login, password):
    con = db()
    try:
        if not con.execute("SELECT 1 FROM clinics WHERE id=?", (clinic_id,)).fetchone():
            return None, None, "Клиника не найдена"
        login = (login or "").strip() or next_login(con, clinic_id)
        password = password or gen_password()
        con.execute("INSERT INTO users(login,password_hash,role,clinic_id,created) VALUES(?,?, 'doctor', ?, ?)",
                    (login, generate_password_hash(password), clinic_id, now()))
        con.commit()
        return login, password, None
    except sqlite3.IntegrityError:
        return None, None, "Такой логин уже занят"
    finally:
        con.close()


def toggle(table, rid):
    assert table in ("clinics", "users")
    con = db()
    if table == "users":
        con.execute("UPDATE users SET active=1-active WHERE id=? AND role='doctor'", (rid,))
    else:
        con.execute("UPDATE clinics SET active=1-active WHERE id=?", (rid,))
    con.commit()
    con.close()


def reset_doctor_password(uid):
    con = db()
    row = con.execute("SELECT login FROM users WHERE id=? AND role='doctor'", (uid,)).fetchone()
    if not row:
        con.close()
        return None, None
    pw = gen_password()
    con.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(pw), uid))
    con.commit()
    con.close()
    return row["login"], pw


def local(ts):
    """'2026-11-11 18:30' (UTC) -> '11.11.2026, 21:30' (Москва, UTC+3)."""
    if not ts:
        return "—"
    try:
        d = datetime.strptime(ts, "%Y-%m-%d %H:%M") + timedelta(hours=3)
    except ValueError:
        return ts
    return d.strftime("%d.%m.%Y, %H:%M")


def audit(login, action, detail=""):
    con = db()
    con.execute("INSERT INTO audit(ts,login,action,detail) VALUES(?,?,?,?)", (now(), login, action, detail[:300]))
    con.commit()
    con.close()


def audit_recent(n=40):
    con = db()
    rows = [dict(r) for r in con.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (n,))]
    con.close()
    for r in rows:
        r["when"] = local(r["ts"])
    return rows


def admin_stats():
    con = db()
    month = datetime.now(timezone(timedelta(hours=3))).strftime("%Y-%m")
    out = {
        "clinics": con.execute("SELECT COUNT(*) FROM clinics").fetchone()[0],
        "doctors": con.execute("SELECT COUNT(*) FROM users WHERE role='doctor'").fetchone()[0],
        "patients": con.execute("SELECT COUNT(*) FROM patients").fetchone()[0],
        "analyses": con.execute("SELECT COUNT(*) FROM analyses WHERE taken_on LIKE ?", (month + "%",)).fetchone()[0],
    }
    con.close()
    return out
