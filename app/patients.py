"""Пациенты врача, их анализы по датам и динамика показателей."""
import math
import sqlite3
from datetime import date, datetime, timedelta, timezone

import logic
import store

MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]
MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]


# ---------------- даты и возраст ----------------

def today():
    return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=3))).date()


def parse_date(s):
    try:
        return date.fromisoformat(str(s))
    except (TypeError, ValueError):
        return None


def total_months(birth, on):
    return (on.year - birth.year) * 12 + on.month - birth.month - (1 if on.day < birth.day else 0)


def child_at(sex, birth_iso, on_iso):
    """Возраст ребёнка на дату анализа в виде, который понимает logic.validate_and_evaluate."""
    b, o = parse_date(birth_iso), parse_date(on_iso)
    m = total_months(b, o)
    years, months = divmod(m, 12)
    weeks = min(3, (o - b).days // 7) if m == 0 else 0
    return {"years": years, "months": months, "weeks": weeks, "sex": sex}, m + (weeks * 0.25 if m == 0 else 0)


def plural(n, one, few, many):
    if n % 10 == 1 and n % 100 != 11:
        return one
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return few
    return many


def plural_years(n):
    if n % 10 == 1 and n % 100 != 11:
        return "год"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "года"
    return "лет"


def age_text(birth_iso, on=None):
    b = parse_date(birth_iso)
    o = on or today()
    m = max(0, total_months(b, o))
    y, mo = divmod(m, 12)
    if y == 0 and mo == 0:
        return f"{max(0, (o - b).days // 7)} нед."
    if y == 0:
        return f"{mo} мес."
    return f"{y} {plural_years(y)}" + (f" {mo} мес." if mo else "")


def interval_for(ind, sex, age_months):
    for b in ind["bins"]:
        if b["from"] - 1e-9 <= age_months < b["to"] - 1e-9:
            return b[sex]
    return None


def dmy(iso):
    d = parse_date(iso)
    return d.strftime("%d.%m.%Y") if d else (iso or "")


# ---------------- пациенты ----------------

def next_card(con, clinic_id):
    rows = con.execute("SELECT card FROM patients WHERE clinic_id=?", (clinic_id,)).fetchall()
    nums = [int(r["card"]) for r in rows if str(r["card"]).isdigit()]
    return f"{(max(nums) + 1) if nums else 1000:06d}"


def add_patient(user, full_name, sex, birth_iso, card=""):
    """Возвращает (id, ошибка)."""
    con = store.db()
    try:
        card = (card or "").strip() or next_card(con, user["clinic_id"])
        cur = con.execute(
            "INSERT INTO patients(clinic_id,doctor_id,card,full_name,sex,birth,created) VALUES(?,?,?,?,?,?,?)",
            (user["clinic_id"], user["id"], card, full_name, sex, birth_iso, store.now()))
        con.commit()
        return cur.lastrowid, None
    except sqlite3.IntegrityError:
        return None, "Пациент с таким номером карты уже есть"
    finally:
        con.close()


def get_patient(pid, doctor_id):
    con = store.db()
    row = con.execute("SELECT * FROM patients WHERE id=? AND doctor_id=?", (pid, doctor_id)).fetchone()
    con.close()
    return dict(row) if row else None


def set_note(pid, note):
    con = store.db()
    con.execute("UPDATE patients SET note=? WHERE id=?", (note, pid))
    con.commit()
    con.close()


def deviations(con, aid):
    r = con.execute("SELECT COUNT(*) AS n, COALESCE(SUM(value<lower OR value>upper),0) AS bad "
                    "FROM analysis_values WHERE analysis_id=?", (aid,)).fetchone()
    return r["n"], int(r["bad"])


def list_patients(doctor_id, q="", flt=""):
    con = store.db()
    rows = [dict(r) for r in con.execute(
        "SELECT p.*, (SELECT a.id FROM analyses a WHERE a.patient_id=p.id ORDER BY a.taken_on DESC, a.id DESC LIMIT 1) AS last_id, "
        "(SELECT a.taken_on FROM analyses a WHERE a.patient_id=p.id ORDER BY a.taken_on DESC, a.id DESC LIMIT 1) AS last_date "
        "FROM patients p WHERE p.doctor_id=? ORDER BY p.full_name", (doctor_id,))]
    week_ago = (today() - timedelta(days=7)).isoformat()
    q = (q or "").strip().casefold()
    out = []
    for r in rows:
        r["total"], r["bad"] = deviations(con, r["last_id"]) if r["last_id"] else (0, 0)
        r["age"] = age_text(r["birth"])
        if q and q not in r["full_name"].casefold() and q not in r["card"]:
            continue
        if flt == "attention" and not r["bad"]:
            continue
        if flt == "new" and not (r["last_date"] and r["last_date"] >= week_ago):
            continue
        out.append(r)
    con.close()
    return out


def doctor_stats(doctor_id):
    con = store.db()
    month = today().strftime("%Y-%m")
    total = con.execute("SELECT COUNT(*) FROM patients WHERE doctor_id=?", (doctor_id,)).fetchone()[0]
    an = con.execute("SELECT COUNT(*) FROM analyses a JOIN patients p ON p.id=a.patient_id "
                     "WHERE p.doctor_id=? AND a.taken_on LIKE ?", (doctor_id, month + "%")).fetchone()[0]
    att = 0
    for r in con.execute(
            "SELECT p.id, (SELECT a.id FROM analyses a WHERE a.patient_id=p.id ORDER BY a.taken_on DESC, a.id DESC LIMIT 1) AS last_id "
            "FROM patients p WHERE p.doctor_id=?", (doctor_id,)):
        if r["last_id"] and deviations(con, r["last_id"])[1]:
            att += 1
    con.close()
    return {"patients": total, "analyses": an, "attention": att, "month_gen": MONTHS[today().month - 1]}


# ---------------- анализы ----------------

def values_of(con, aid, sex=None):
    out = []
    for r in con.execute("SELECT * FROM analysis_values WHERE analysis_id=? ORDER BY ind_id", (aid,)):
        ind = logic.INDICATORS.get(r["ind_id"])
        if ind:
            out.append(logic.evaluate(ind, r["value"], r["lower"], r["upper"], sex))
    return out


def analyses_of(pid, start=None, end=None):
    """Анализы пациента, новые сверху, со сводкой по отклонениям."""
    con = store.db()
    sql, args = "SELECT a.*, u.login AS by_login FROM analyses a LEFT JOIN users u ON u.id=a.created_by WHERE a.patient_id=?", [pid]
    if start:
        sql += " AND a.taken_on>=?"
        args.append(start)
    if end:
        sql += " AND a.taken_on<=?"
        args.append(end)
    sql += " ORDER BY a.taken_on DESC, a.id DESC"
    rows = [dict(r) for r in con.execute(sql, args)]
    for r in rows:
        r["total"], r["bad"] = deviations(con, r["id"])
    con.close()
    return rows


def get_analysis(aid, pid, sex=None):
    con = store.db()
    row = con.execute("SELECT a.*, u.login AS by_login FROM analyses a LEFT JOIN users u ON u.id=a.created_by "
                      "WHERE a.id=? AND a.patient_id=?", (aid, pid)).fetchone()
    if not row:
        con.close()
        return None, []
    a = dict(row)
    results = values_of(con, aid, sex)
    con.close()
    return a, results


def previous_values(pid, before_date, exclude_aid=None):
    """Для каждого показателя: последнее значение до указанной даты. {ind_id: {value, date}}."""
    con = store.db()
    out = {}
    for r in con.execute(
            "SELECT v.ind_id, v.value, a.taken_on, a.id FROM analysis_values v JOIN analyses a ON a.id=v.analysis_id "
            "WHERE a.patient_id=? AND a.taken_on<=? ORDER BY a.taken_on ASC, a.id ASC", (pid, before_date)):
        if exclude_aid and r["id"] == exclude_aid:
            continue
        out[r["ind_id"]] = {"value": logic.fmt(r["value"]), "date": r["taken_on"], "raw": r["value"]}
    con.close()
    return out


def history_lines(pid, ind_ids, on_iso, exclude_aid=None, keep=4):
    """Строки с прошлыми значениями для запроса к ИИ. Без дат, только «дней назад»."""
    con = store.db()
    on = parse_date(on_iso)
    lines = []
    for iid in ind_ids:
        ind = logic.INDICATORS.get(iid)
        rows = con.execute(
            "SELECT v.value, a.taken_on FROM analysis_values v JOIN analyses a ON a.id=v.analysis_id "
            "WHERE a.patient_id=? AND v.ind_id=? AND a.taken_on<=? AND a.id IS NOT ? "
            "ORDER BY a.taken_on DESC, a.id DESC LIMIT ?", (pid, iid, on_iso, exclude_aid, keep)).fetchall()
        if not ind or not rows:
            continue
        parts = [f"{(on - parse_date(r['taken_on'])).days} дн. назад: {logic.fmt(r['value'])}" for r in reversed(rows)]
        lines.append(f"- {ind['name']}: " + "; ".join(parts))
    con.close()
    return lines


def save_analysis(user, pid, taken_on, lab, results, ai):
    con = store.db()
    cur = con.execute("INSERT INTO analyses(patient_id,taken_on,lab,created_by,created,ai_text,ai_kind) VALUES(?,?,?,?,?,?,?)",
                      (pid, taken_on, lab, user["id"], store.now(),
                       ai.get("text") if (ai.get("ai") or ai.get("ref")) else None,
                       "ai" if ai.get("ai") else ("ref" if ai.get("ref") else None)))
    aid = cur.lastrowid
    for r in results:
        con.execute("INSERT INTO analysis_values(analysis_id,ind_id,value,lower,upper) VALUES(?,?,?,?,?)",
                    (aid, r["ind"]["id"], logic.parse_num(r["value"]), logic.parse_num(r["lower"]), logic.parse_num(r["upper"])))
    con.commit()
    con.close()
    return aid


def analyses_on(pid, taken_on):
    """Сколько анализов у пациента уже есть на эту дату."""
    con = store.db()
    n = con.execute("SELECT COUNT(*) FROM analyses WHERE patient_id=? AND taken_on=?", (pid, taken_on)).fetchone()[0]
    con.close()
    return n


def delete_analysis(aid, pid):
    """Удаляет анализ пациента вместе со значениями. Возвращает True, если анализ был."""
    con = store.db()
    cur = con.execute("DELETE FROM analyses WHERE id=? AND patient_id=?", (aid, pid))
    con.commit()
    con.close()
    return cur.rowcount > 0


def set_ai(aid, ai):
    if not (ai.get("ai") or ai.get("ref")):
        return
    con = store.db()
    con.execute("UPDATE analyses SET ai_text=?, ai_kind=? WHERE id=?", (ai["text"], "ai" if ai.get("ai") else "ref", aid))
    con.commit()
    con.close()


# ---------------- динамика ----------------

def indicators_of(pid):
    """Показатели, по которым у пациента есть значения: по числу измерений."""
    con = store.db()
    rows = con.execute("SELECT v.ind_id, COUNT(*) AS n FROM analysis_values v JOIN analyses a ON a.id=v.analysis_id "
                       "WHERE a.patient_id=? GROUP BY v.ind_id ORDER BY n DESC, v.ind_id", (pid,)).fetchall()
    con.close()
    return [logic.INDICATORS[r["ind_id"]] for r in rows if r["ind_id"] in logic.INDICATORS]


def series(pid, ind_id, start=None, end=None):
    con = store.db()
    sql = ("SELECT a.taken_on, v.value, v.lower, v.upper, a.id AS aid FROM analysis_values v JOIN analyses a ON a.id=v.analysis_id "
           "WHERE a.patient_id=? AND v.ind_id=?")
    args = [pid, ind_id]
    if start:
        sql += " AND a.taken_on>=?"
        args.append(start)
    if end:
        sql += " AND a.taken_on<=?"
        args.append(end)
    sql += " ORDER BY a.taken_on, a.id"
    rows = [dict(r) for r in con.execute(sql, args)]
    con.close()
    return rows


PERIODS = [("m1", "Месяц", 30), ("m3", "3 месяца", 91), ("m6", "6 месяцев", 182), ("y1", "Год", 365), ("all", "Всё время", None)]


def period_range(period, d_from, d_to):
    """Возвращает (ключ периода, начало ISO или None, конец ISO или None)."""
    t = today()
    if period == "custom":
        a, b = parse_date(d_from), parse_date(d_to)
        if a and b and a <= b:
            return "custom", a.isoformat(), b.isoformat()
        period = "all"
    for key, _, days in PERIODS:
        if key == period:
            return key, ((t - timedelta(days=days)).isoformat() if days else None), None
    return "all", None, None


def nice_ticks(vmin, vmax, n=4):
    span = (vmax - vmin) or max(abs(vmax), 1.0)
    raw = span / n
    mag = 10 ** math.floor(math.log10(raw))
    step = mag * 10
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * mag + 1e-12:
            step = m * mag
            break
    start = math.floor(vmin / step + 1e-9) * step
    end = math.ceil(vmax / step - 1e-9) * step
    ticks, v = [], start
    while v <= end + step * 1e-6:
        ticks.append(round(v, 6))
        v += step
    return ticks


def build_chart(points):
    """Готовые координаты для SVG-графика. points: строки из series()."""
    if not points:
        return None
    x0, x1, yt, yb = 100.0, 700.0, 20.0, 240.0
    lo, hi = points[-1]["lower"], points[-1]["upper"]
    vals = [p["value"] for p in points]
    ticks = nice_ticks(min(vals + [lo]), max(vals + [hi]))
    tmin, tmax = ticks[0], ticks[-1]
    if tmax == tmin:
        tmax = tmin + 1

    def y(v):
        return round(yb - (v - tmin) / (tmax - tmin) * (yb - yt), 1)

    dates = [parse_date(p["taken_on"]) for p in points]
    d0, d1 = dates[0], dates[-1]
    span = (d1 - d0).days
    long_span = span > 300
    pts, last_x = [], -1000.0
    for p, d in zip(points, dates):
        x = round((x0 + x1) / 2 if span == 0 else x0 + (d - d0).days / span * (x1 - x0), 1)
        bad = p["value"] > p["upper"] or p["value"] < p["lower"]
        show_date = x - last_x >= 62
        if show_date:
            last_x = x
        pts.append({"x": x, "y": y(p["value"]), "bad": bad, "up": p["value"] > p["upper"],
                    "label": logic.fmt(p["value"]), "date": d.strftime("%d.%m.%y" if long_span else "%d.%m"),
                    "show_date": show_date, "aid": p["aid"]})
    if len(pts) > 1 and not pts[-1]["show_date"]:  # последнюю подпись всегда показываем
        for q in reversed(pts[:-1]):
            if q["show_date"]:
                q["show_date"] = pts[-1]["x"] - q["x"] >= 62
                break
        pts[-1]["show_date"] = True
    return {
        "ticks": [{"y": y(t), "label": logic.fmt(t)} for t in ticks],
        "band_y": y(hi), "band_h": round(y(lo) - y(hi), 1),
        "band_label": f"интервал {logic.fmt(lo)}–{logic.fmt(hi)}",
        "line": " ".join(f"{p['x']},{p['y']}" for p in pts),
        "points": pts, "show_values": len(pts) <= 12,
        "lo": logic.fmt(lo), "hi": logic.fmt(hi),
        "aria": "; ".join(f"{dmy(p['taken_on'])}: {logic.fmt(p['value'])}" for p in points),
        "bad_count": sum(1 for p in pts if p["bad"]),
    }


# ---------------- демо-данные ----------------

def shift_months(d, n):
    m = d.year * 12 + d.month - 1 - n
    y, mo = divmod(m, 12)
    mo += 1
    day = min(d.day, [31, 29 if y % 4 == 0 and (y % 100 or y % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][mo - 1])
    return date(y, mo, day)


# (ФИО, пол, возраст лет, мес., лишние дни, [дни назад], {показатель: значения})
DEMO = [
    ("Соколова Мария Андреевна", "f", 3, 6, 2, [120, 70, 33, 14, 0],
     {4: [9.8, 12.4, 14.9, 15.8, 16.5], 6: [7, 9, 11, 12, 14], 8: [2, 3, 3, 4, 18], 1: [134, 133, 132, 131, 128]}),
    ("Белов Артём Игоревич", "m", 7, 2, 5, [90, 45, 1], {6: [16, 13, 11], 4: [8.2, 7.9, 8.0]}),
    ("Кузнецова Алиса Павловна", "f", 1, 1, 3, [75, 30, 2], {1: [118, 121, 124], 4: [9.1, 8.7, 9.4]}),
    ("Морозов Тимофей Сергеевич", "m", 5, 0, 9, [100, 52, 5], {1: [124, 114, 106], 9: [40, 24, 12]}),
    ("Орлова Дарья Николаевна", "f", 10, 8, 4, [80, 3], {1: [132, 134], 4: [6.5, 7.0], 5: [265, 280]}),
    ("Волков Иван Денисович", "m", 2, 9, 6, [110, 60, 13], {8: [1, 3, 9], 4: [11.2, 12.9, 13.8]}),
    ("Новикова Полина Артёмовна", "f", 0, 8, 7, [100, 50, 15], {1: [96, 106, 113]}),
    ("Лебедев Михаил Андреевич", "m", 12, 0, 8, [90, 40, 20], {7: [5.4, 5.9, 6.3], 4: [7.1, 7.4, 7.0]}),
    ("Зайцева Ульяна Романовна", "f", 4, 3, 1, [70, 35, 4], {1: [128, 127, 129], 6: [6, 5, 6]}),
    ("Громов Егор Валерьевич", "m", 9, 0, 10, [60, 9], {5: [310, 355], 4: [7.2, 8.0]}),
]


def seed_demo(user_row):
    """Добавляет врачу 10 вымышленных пациентов с анализами. Возвращает (число, ошибка)."""
    con = store.db()
    if con.execute("SELECT 1 FROM patients WHERE doctor_id=? LIMIT 1", (user_row["id"],)).fetchone():
        con.close()
        return 0, "У этого врача уже есть пациенты, демо-данные не добавляются"
    t = today()
    n = 0
    for name, sex, years, months, extra, days_ago, series_ in DEMO:
        birth = shift_months(t, years * 12 + months) - timedelta(days=extra)
        card = next_card(con, user_row["clinic_id"])
        cur = con.execute("INSERT INTO patients(clinic_id,doctor_id,card,full_name,sex,birth,created) VALUES(?,?,?,?,?,?,?)",
                          (user_row["clinic_id"], user_row["id"], card, name, sex, birth.isoformat(), store.now()))
        pid = cur.lastrowid
        for k, ago in enumerate(days_ago):
            on = t - timedelta(days=ago)
            _, age_m = child_at(sex, birth.isoformat(), on.isoformat())
            vals = []
            for iid, arr in series_.items():
                iv = interval_for(logic.INDICATORS[iid], sex, age_m)
                if iv:
                    vals.append((iid, arr[k], iv[0], iv[1]))
            if not vals:
                continue
            a = con.execute("INSERT INTO analyses(patient_id,taken_on,lab,created_by,created) VALUES(?,?,?,?,?)",
                            (pid, on.isoformat(), "Лаборатория ДРКБ", user_row["id"], store.now()))
            for iid, v, lo, hi in vals:
                con.execute("INSERT INTO analysis_values(analysis_id,ind_id,value,lower,upper) VALUES(?,?,?,?,?)",
                            (a.lastrowid, iid, v, lo, hi))
        n += 1
    con.commit()
    con.close()
    return n, None
