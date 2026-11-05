import json
import os

from flask import Flask, jsonify, render_template, request

import logic

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024  # маленькие запросы: только числа и списки


@app.after_request
def headers(resp):
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


def blank_form():
    return {"years": "", "months": "0", "sex": "", "rows": [{"indicator": "", "value": "", "lower": "", "upper": ""}]}


def read_form():
    f = request.form
    rows = []
    for ind, v, lo, hi in zip(f.getlist("indicator"), f.getlist("value"), f.getlist("lower"), f.getlist("upper")):
        rows.append({"indicator": ind, "value": v, "lower": lo, "upper": hi})
    return {"years": f.get("years", ""), "months": f.get("months", "0"), "sex": f.get("sex"), "rows": rows}


@app.get("/")
def index():
    return render_template("index.html", form=blank_form(), errors=[], indicators=logic.REF["indicators"],
                           demos=json.dumps(logic.DEMOS, ensure_ascii=False))


@app.post("/evaluate")
def evaluate():
    if logic.rate_limited(request.remote_addr):
        return "Слишком много запросов. Подождите минуту.", 429
    form = read_form()
    errors, child, results = logic.validate_and_evaluate(form)
    if errors:
        return render_template("index.html", form=form, errors=errors, indicators=logic.REF["indicators"],
                               demos=json.dumps(logic.DEMOS, ensure_ascii=False)), 400
    for r in results:
        logic.polish(r)
    sex = {"f": "Девочка", "m": "Мальчик"}[child["sex"]]
    return render_template("result.html", child=child, sex=sex, results=results,
                           dev=sum(1 for r in results if r["status"] != "within"),
                           questions=logic.QUESTIONS, ai=logic.ai_enabled())


@app.post("/ask")
def ask():
    if logic.rate_limited(request.remote_addr, limit=20):
        return jsonify(ok=False, text="Слишком много запросов. Подождите минуту."), 429
    d = request.get_json(silent=True) or {}
    try:
        ind_id = int(d.get("indicator"))
    except (TypeError, ValueError):
        return jsonify(ok=False, text="Некорректный запрос."), 400
    return jsonify(logic.answer_question(ind_id, d.get("direction"), d.get("qkey"), d.get("text")))


@app.get("/health")
def health():
    return "ok"


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 8000)))
