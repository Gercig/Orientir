import json
import os

from flask import Flask, jsonify, render_template, request

import logic
import ocr
import patients as pt

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024  # маленькие запросы: только числа и списки


import re
from functools import wraps
from urllib.parse import urlparse

from flask import abort, flash, g, redirect, session, url_for

import store

app.config.update(
    SECRET_KEY=store.secret_key(),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=8 * 3600,
)
store.init()

PUBLIC = {"login", "health", "static"}


@app.before_request
def guard():
    """Вход по логину и паролю. Запросы с чужого сайта отклоняются."""
    if request.method == "POST":
        origin = request.headers.get("Origin")
        site = request.headers.get("Sec-Fetch-Site")
        if origin and origin != "null" and urlparse(origin).netloc != request.host:
            abort(403)
        if site and site not in ("same-origin", "none"):
            abort(403)
    g.user = None
    uid = session.get("uid")
    if uid:
        g.user = store.get_user(uid)
        if not g.user:
            session.clear()
    if request.endpoint in PUBLIC:
        return None
    if not g.user:
        if request.path in ("/ask",):
            return jsonify(ok=False, text="Сессия закончилась. Войдите заново."), 401
        return redirect(url_for("login", next=request.path if request.method == "GET" else ""))
    return None


def role_required(role):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            if g.user["role"] != role:
                if request.path == "/ask":
                    return jsonify(ok=False, text="Нет доступа."), 403
                return redirect(url_for("admin_page") if g.user["role"] == "admin" else url_for("index"))
            return fn(*a, **kw)
        return wrapper
    return deco


@app.context_processor
def inject_user():
    return {"user": g.get("user")}


@app.after_request
def headers(resp):
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    return resp


def blank_form():
    return {"years": "", "months": "0", "weeks": "0", "sex": "", "rows": [{"indicator": "", "value": "", "lower": "", "upper": ""}]}


def read_form():
    f = request.form
    rows = []
    for ind, v, lo, hi in zip(f.getlist("indicator"), f.getlist("value"), f.getlist("lower"), f.getlist("upper")):
        rows.append({"indicator": ind, "value": v, "lower": lo, "upper": hi})
    return {"years": f.get("years", ""), "months": f.get("months", "0"), "weeks": f.get("weeks", "0"), "sex": f.get("sex"), "rows": rows}


@app.get("/")
def index():
    return redirect(url_for("admin_page") if g.user["role"] == "admin" else url_for("patients_page"))


@app.get("/quick")
@role_required("doctor")
def quick():
    return render_template("index.html", nav="quick", form=blank_form(), errors=[], indicators=logic.REF["indicators"],
                           demos=json.dumps(logic.DEMOS, ensure_ascii=False),
                           intervals=json.dumps({i["id"]: i["bins"] for i in logic.REF["indicators"]}, ensure_ascii=False))


@app.post("/evaluate")
@role_required("doctor")
def evaluate():
    if logic.rate_limited(request.remote_addr):
        return "Слишком много запросов. Подождите минуту.", 429
    form = read_form()
    errors, child, results = logic.validate_and_evaluate(form)
    if errors:
        return render_template("index.html", nav="quick", form=form, errors=errors, indicators=logic.REF["indicators"],
                               demos=json.dumps(logic.DEMOS, ensure_ascii=False),
                           intervals=json.dumps({i["id"]: i["bins"] for i in logic.REF["indicators"]}, ensure_ascii=False)), 400
    answer = logic.ask_ai(child, results)
    sex = {"f": "Девочка", "m": "Мальчик"}[child["sex"]]
    ctx = {"years": child["years"], "months": child["months"], "weeks": child["weeks"], "sex": child["sex"],
           "rows": [{"indicator": r["ind"]["id"], "value": r["value"], "lower": r["lower"], "upper": r["upper"]}
                    for r in results]}
    return render_template("result.html", nav="quick", child=child, sex=sex, results=results,
                           dev=sum(1 for r in results if r["status"] != "within"),
                           answer=answer, ctx=ctx, age=logic.age_label(child))


@app.post("/ask")
@role_required("doctor")
def ask():
    if logic.rate_limited(request.remote_addr, limit=20):
        return jsonify(ok=False, text="Слишком много запросов. Подождите минуту."), 429
    d = request.get_json(silent=True) or {}
    form = {"years": str(d.get("years", "")), "months": str(d.get("months", "0")), "weeks": str(d.get("weeks", "0")), "sex": d.get("sex"),
            "rows": [{k: str(r.get(k, "")) for k in ("indicator", "value", "lower", "upper")}
                     for r in (d.get("rows") or []) if isinstance(r, dict)]}
    history = None
    try:
        pid, aid = int(d.get("pid")), int(d.get("aid"))
    except (TypeError, ValueError):
        pid = aid = None
    if pid:
        p = pt.get_patient(pid, g.user["id"])
        a, _ = pt.get_analysis(aid, pid) if p else (None, [])
        if not a:
            return jsonify(ok=False, text="Анализ не найден."), 404
        ids = [int(r["indicator"]) for r in form["rows"] if str(r["indicator"]).isdigit()]
        history = pt.history_lines(pid, ids, a["taken_on"], exclude_aid=aid) or None
        store.audit(g.user["login"], "Задал вопрос ИИ", f"карта № {p['card']}")
    return jsonify(logic.answer_free(form, d.get("text"), history))



@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("admin_page") if g.user["role"] == "admin" else url_for("index"))
    error = None
    if request.method == "POST":
        if logic.rate_limited("login:" + (request.remote_addr or ""), limit=10):
            return render_template("login.html", error="Слишком много попыток. Подождите минуту."), 429
        u = store.authenticate(request.form.get("login", "").strip(), request.form.get("password", ""))
        if u:
            session.clear()
            session.permanent = True
            session["uid"] = u["id"]
            store.audit(u["login"], "Вошёл в систему")
            nxt = request.args.get("next", "")
            if not nxt.startswith("/") or nxt.startswith("//") or u["role"] == "admin":
                nxt = url_for("admin_page") if u["role"] == "admin" else url_for("index")
            return redirect(nxt)
        error = "Неверный логин или пароль, либо доступ отключён"
    return render_template("login.html", error=error), (401 if error else 200)


@app.post("/logout")
def logout():
    if g.user:
        store.audit(g.user["login"], "Вышел из системы")
    session.clear()
    return redirect(url_for("login"))


@app.route("/password", methods=["GET", "POST"])
def password_page():
    msg = err = None
    if request.method == "POST":
        cur, new, rep = (request.form.get(k, "") for k in ("current", "new", "repeat"))
        if not store.check_current(g.user["id"], cur):
            err = "Текущий пароль неверный"
        elif len(new) < 8:
            err = "Новый пароль: не короче 8 символов"
        elif new != rep:
            err = "Пароли не совпадают"
        else:
            store.set_password(g.user["id"], new)
            msg = "Пароль изменён"
    return render_template("password.html", msg=msg, err=err)


def admin_view(created=None, err=None, status=200):
    stats = store.admin_stats()
    stats["month_gen"] = pt.MONTHS[pt.today().month - 1]
    return render_template("admin.html", nav="admin", clinics=store.clinics_with_doctors(), created=created, err=err,
                           stats=stats, journal=store.audit_recent(40)), status


@app.get("/admin")
@role_required("admin")
def admin_page():
    return admin_view()


@app.post("/admin/clinics")
@role_required("admin")
def admin_add_clinic():
    name = request.form.get("name", "").strip()
    if not (2 <= len(name) <= 120):
        return admin_view(err="Название клиники: от 2 до 120 символов", status=400)
    err = store.add_clinic(name)
    if not err:
        store.audit(g.user["login"], "Добавил клинику", name)
    return admin_view(err=err, status=400 if err else 200)


@app.post("/admin/clinics/<int:cid>/toggle")
@role_required("admin")
def admin_toggle_clinic(cid):
    store.toggle("clinics", cid)
    store.audit(g.user["login"], "Изменил доступ клиники", f"id {cid}")
    return redirect(url_for("admin_page"))


@app.post("/admin/doctors")
@role_required("admin")
def admin_add_doctor():
    login = request.form.get("login", "").strip()
    pw = request.form.get("password", "")
    if login and not re.fullmatch(r"[A-Za-z0-9._-]{3,32}", login):
        return admin_view(err="Логин: 3–32 символа, латиница, цифры, точка, дефис, подчёркивание", status=400)
    if pw and len(pw) < 8:
        return admin_view(err="Пароль: не короче 8 символов (или оставьте пустым, он создастся сам)", status=400)
    try:
        cid = int(request.form.get("clinic_id", ""))
    except ValueError:
        return admin_view(err="Выберите клинику", status=400)
    login, pw, err = store.add_doctor(cid, login, pw)
    if err:
        return admin_view(err=err, status=400)
    store.audit(g.user["login"], "Создал учётную запись", login)
    return admin_view(created={"login": login, "password": pw, "what": "Врач добавлен"})


@app.post("/admin/doctors/<int:uid>/toggle")
@role_required("admin")
def admin_toggle_doctor(uid):
    store.toggle("users", uid)
    store.audit(g.user["login"], "Изменил доступ врача", f"id {uid}")
    return redirect(url_for("admin_page"))


@app.post("/admin/doctors/<int:uid>/reset")
@role_required("admin")
def admin_reset_doctor(uid):
    login, pw = store.reset_doctor_password(uid)
    if not login:
        return admin_view(err="Врач не найден", status=404)
    store.audit(g.user["login"], "Сбросил пароль", login)
    return admin_view(created={"login": login, "password": pw, "what": "Пароль заменён"})


@app.post("/admin/doctors/<int:uid>/demo")
@role_required("admin")
def admin_demo(uid):
    con = store.db()
    row = con.execute("SELECT id, login, clinic_id FROM users WHERE id=? AND role='doctor'", (uid,)).fetchone()
    con.close()
    if not row:
        return admin_view(err="Врач не найден", status=404)
    n, err = pt.seed_demo(row)
    if err:
        return admin_view(err=err, status=400)
    store.audit(g.user["login"], "Добавил демо-пациентов", f"{row['login']}: {n}")
    return redirect(url_for("admin_page"))


# ---------------- пациенты ----------------

app.add_template_filter(pt.dmy, "dmy")
app.add_template_filter(pt.plural, "plural")


def own_patient(pid):
    p = pt.get_patient(pid, g.user["id"])
    if not p:
        abort(404)
    p["age"] = pt.age_text(p["birth"])
    return p


def patients_view(form=None, err=None, status=200):
    q = request.args.get("q", "").strip()[:60]
    flt = request.args.get("f", "")
    stats = pt.doctor_stats(g.user["id"])
    return render_template("patients.html", nav="patients", patients=pt.list_patients(g.user["id"], q, flt), q=q, flt=flt,
                           stats=stats, form=form or {"full_name": "", "birth": "", "sex": "", "card": ""},
                           err=err, today=pt.today().isoformat(),
                           min_birth=pt.today().replace(year=pt.today().year - 19).isoformat()), status


@app.get("/patients")
@role_required("doctor")
def patients_page():
    return patients_view()


@app.post("/patients")
@role_required("doctor")
def patients_add():
    f = request.form
    form = {"full_name": " ".join(f.get("full_name", "").split()), "birth": f.get("birth", ""), "sex": f.get("sex", ""),
            "card": f.get("card", "").strip()}
    b = pt.parse_date(form["birth"])
    if not (3 <= len(form["full_name"]) <= 120):
        return patients_view(form, "ФИО: от 3 до 120 символов", 400)
    if form["sex"] not in ("f", "m"):
        return patients_view(form, "Выберите пол", 400)
    if not b or b > pt.today() or pt.total_months(b, pt.today()) > 18 * 12:
        return patients_view(form, f"Дата рождения {pt.dmy(form['birth']) if b else 'указана неверно'}: проверьте год. Нужен ребёнок от 0 до 18 лет.", 400)
    if form["card"] and not re.fullmatch(r"[0-9A-Za-zА-Яа-я/-]{1,20}", form["card"]):
        return patients_view(form, "№ карты: до 20 символов, буквы, цифры, дефис", 400)
    pid, err = pt.add_patient(g.user, form["full_name"], form["sex"], b.isoformat(), form["card"])
    if err:
        return patients_view(form, err, 400)
    p = pt.get_patient(pid, g.user["id"])
    store.audit(g.user["login"], "Добавил пациента", f"карта № {p['card']}")
    return redirect(url_for("patient_page", pid=pid))


@app.get("/patients/<int:pid>")
@role_required("doctor")
def patient_page(pid):
    p = own_patient(pid)
    inds = pt.indicators_of(pid)
    try:
        want = int(request.args.get("ind", ""))
    except ValueError:
        want = None
    cur = next((i for i in inds if i["id"] == want), inds[0] if inds else None)
    period, start, end = pt.period_range(request.args.get("period", "all"), request.args.get("from"), request.args.get("to"))
    chart = pt.build_chart(pt.series(pid, cur["id"], start, end)) if cur else None
    allan = pt.analyses_of(pid)
    store.audit(g.user["login"], "Открыл карточку пациента", f"карта № {p['card']}")
    return render_template("patient.html", nav="patients", p=p, inds=inds, cur=cur, chart=chart, period=period,
                           periods=[(k, l) for k, l, _ in pt.PERIODS],
                           d_from=(start if period == "custom" else request.args.get("from", "")),
                           d_to=(end if period == "custom" else request.args.get("to", "")),
                           history=pt.analyses_of(pid, start, end), latest=allan[0] if allan else None,
                           saved=request.args.get("saved") == "1",
                           deleted=request.args.get("deleted") == "1")


@app.post("/patients/<int:pid>/note")
@role_required("doctor")
def patient_note(pid):
    p = own_patient(pid)
    pt.set_note(pid, request.form.get("note", "").strip()[:1000])
    store.audit(g.user["login"], "Изменил заметку", f"карта № {p['card']}")
    return redirect(url_for("patient_page", pid=pid, saved=1))


def new_analysis_view(p, form, errors=None, status=200):
    last = pt.analyses_of(p["id"])
    prev = pt.previous_values(p["id"], pt.today().isoformat())
    return render_template("analysis_new.html", nav="patients", p=p, form=form, errors=errors or [], today=pt.today().isoformat(),
                           indicators=logic.REF["indicators"], prev={str(k): v for k, v in prev.items()},
                           intervals=json.dumps({i["id"]: i["bins"] for i in logic.REF["indicators"]}, ensure_ascii=False)), status


@app.route("/patients/<int:pid>/analyses/new", methods=["GET", "POST"])
@role_required("doctor")
def analysis_new(pid):
    p = own_patient(pid)
    if request.method == "GET":
        ids = [i["id"] for i in pt.indicators_of(pid)]
        ids = ids[:12] or [""]
        rows = [{"indicator": i, "value": "", "lower": "", "upper": ""} for i in ids]
        return new_analysis_view(p, {"taken_on": pt.today().isoformat(), "lab": "", "rows": rows})
    f = request.form
    rows = [{"indicator": a, "value": b, "lower": c, "upper": d}
            for a, b, c, d in zip(f.getlist("indicator"), f.getlist("value"), f.getlist("lower"), f.getlist("upper"))]
    form = {"taken_on": f.get("taken_on", ""), "lab": f.get("lab", "").strip()[:80], "rows": rows or [{"indicator": "", "value": "", "lower": "", "upper": ""}]}
    errors = []
    on = pt.parse_date(form["taken_on"])
    if not on or on > pt.today() or on < pt.parse_date(p["birth"]):
        errors.append("Дата анализа: не раньше рождения и не позже сегодняшнего дня.")
        return new_analysis_view(p, form, errors, 400)
    filled = [r for r in rows if r["value"].strip() or r["indicator"]]
    filled = [r for r in filled if r["value"].strip()]
    ids = [r["indicator"] for r in filled]
    if len(set(ids)) != len(ids):
        errors.append("Один показатель указан дважды.")
    child, _ = pt.child_at(p["sex"], p["birth"], on.isoformat())
    errs, child, results = logic.validate_and_evaluate({**child, "rows": filled})
    errors += errs
    if errors:
        return new_analysis_view(p, form, errors, 400)
    if pt.analyses_on(pid, on.isoformat()) and f.get("confirm_dup") != "1":
        form["dup"] = True
        return new_analysis_view(p, form, [f"На {pt.dmy(on.isoformat())} у этого пациента уже есть анализ. "
                                           "Если это не случайный повтор, нажмите «Всё равно сохранить»."], 200)
    if logic.rate_limited(request.remote_addr, limit=20):
        return "Слишком много запросов. Подождите минуту.", 429
    history = pt.history_lines(pid, [r["ind"]["id"] for r in results], on.isoformat()) or None
    answer = logic.ask_ai(child, results, None, history)
    aid = pt.save_analysis(g.user, pid, on.isoformat(), form["lab"], results, answer)
    store.audit(g.user["login"], "Внёс анализ", f"карта № {p['card']}, от {pt.dmy(on.isoformat())}")
    return redirect(url_for("analysis_page", pid=pid, aid=aid, saved=1))


@app.post("/patients/<int:pid>/analyses/parse")
@role_required("doctor")
def analysis_parse(pid):
    p = own_patient(pid)
    request.max_content_length = ocr.MAX_BYTES + 100_000
    if not ocr.enabled():
        return jsonify(ok=False, text="Распознавание не подключено: не задан ключ Yandex."), 503
    if logic.rate_limited("ocr:" + (request.remote_addr or ""), limit=10):
        return jsonify(ok=False, text="Слишком много запросов. Подождите минуту."), 429
    f = request.files.get("file")
    data = f.read(ocr.MAX_BYTES + 1) if f else b""
    if not data:
        return jsonify(ok=False, text="Выберите файл."), 400
    if len(data) > ocr.MAX_BYTES:
        return jsonify(ok=False, text="Файл больше 5 МБ."), 400
    mime = ocr.sniff(data)
    if not mime:
        return jsonify(ok=False, text="Нужен снимок (JPG, PNG) или PDF."), 400
    try:
        res = ocr.extract(ocr.call_vision(data, mime))
    except Exception as e:
        code = getattr(getattr(e, "response", None), "status_code", None)
        msg = "У ключа нет прав на распознавание. Добавьте сервисному аккаунту роль для Vision." if code in (401, 403) \
            else "Не удалось распознать бланк. Попробуйте более чёткое фото или введите значения вручную."
        return jsonify(ok=False, text=msg), 502
    d = pt.parse_date(res["date"])
    if not d or d > pt.today() or d < pt.parse_date(p["birth"]):
        res["date"] = None
    store.audit(g.user["login"], "Загрузил бланк", f"карта № {p['card']}, показателей: {len(res['rows'])}")
    if not res["rows"]:
        return jsonify(ok=False, text="Показатели в бланке не найдены. Введите значения вручную."), 200
    return jsonify(ok=True, **res)


@app.get("/patients/<int:pid>/analyses/<int:aid>")
@role_required("doctor")
def analysis_page(pid, aid):
    p = own_patient(pid)
    a, results = pt.get_analysis(aid, pid, p["sex"])
    if not a:
        abort(404)
    prev = pt.previous_values(pid, a["taken_on"], exclude_aid=aid)
    for r in results:
        pv = prev.get(r["ind"]["id"])
        if pv:
            d = round(logic.parse_num(r["value"]) - pv["raw"], 3)
            r["prev"] = {"value": pv["value"], "date": pv["date"], "cls": "up" if d > 0 else "down" if d < 0 else "",
                         "delta": "без изменений" if d == 0 else ("+" if d > 0 else "−") + logic.fmt(abs(d))}
    child, _ = pt.child_at(p["sex"], p["birth"], a["taken_on"])
    ctx = {"years": child["years"], "months": child["months"], "weeks": child["weeks"], "sex": child["sex"], "pid": pid, "aid": aid,
           "rows": [{"indicator": r["ind"]["id"], "value": r["value"], "lower": r["lower"], "upper": r["upper"]} for r in results]}
    store.audit(g.user["login"], "Открыл анализ", f"карта № {p['card']}, от {pt.dmy(a['taken_on'])}")
    return render_template("analysis.html", nav="patients", p=p, a=a, results=results, ctx=ctx,
                           age=pt.age_text(p["birth"], pt.parse_date(a["taken_on"])),
                           dev=sum(1 for r in results if r["status"] != "within"), just_saved=request.args.get("saved") == "1")


@app.post("/patients/<int:pid>/analyses/<int:aid>/delete")
@role_required("doctor")
def analysis_delete(pid, aid):
    p = own_patient(pid)
    a, _ = pt.get_analysis(aid, pid)
    if not a:
        abort(404)
    pt.delete_analysis(aid, pid)
    store.audit(g.user["login"], "Удалил анализ", f"карта № {p['card']}, от {pt.dmy(a['taken_on'])}")
    return redirect(url_for("patient_page", pid=pid, deleted=1))


@app.post("/patients/<int:pid>/analyses/<int:aid>/ai")
@role_required("doctor")
def analysis_ai(pid, aid):
    p = own_patient(pid)
    a, results = pt.get_analysis(aid, pid, p["sex"])
    if not a:
        abort(404)
    if logic.rate_limited(request.remote_addr, limit=20):
        return "Слишком много запросов. Подождите минуту.", 429
    child, _ = pt.child_at(p["sex"], p["birth"], a["taken_on"])
    history = pt.history_lines(pid, [r["ind"]["id"] for r in results], a["taken_on"], exclude_aid=aid) or None
    pt.set_ai(aid, logic.ask_ai(child, results, None, history))
    store.audit(g.user["login"], "Запросил разбор ИИ", f"карта № {p['card']}")
    return redirect(url_for("analysis_page", pid=pid, aid=aid))


@app.get("/health")
def health():
    return "ok"


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 8000)))
