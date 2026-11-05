"""Логика «Ориентира»: проверка ввода, расчёт отклонений, правила, работа с YandexGPT.

Ничего из введённого врачом не записывается ни в файлы, ни в базу, ни в журнал.
"""
import json
import os
import re
import time
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent
REF = json.loads((BASE.parent / "spravochnik.json").read_text(encoding="utf-8"))
INDICATORS = {i["id"]: i for i in REF["indicators"]}
RULES = {(r["indicator_id"], r["direction"]): r for r in REF["rules"]}

MAX_ROWS = 12

DEMOS = [
    {"title": "Снижен гемоглобин", "years": 7, "months": 3, "sex": "f", "rows": [
        {"indicator": 1, "value": "98", "lower": "110", "upper": "140"},
        {"indicator": 4, "value": "7,2", "lower": "5,0", "upper": "14,5"}]},
    {"title": "Всё в пределах", "years": 10, "months": 0, "sex": "m", "rows": [
        {"indicator": 1, "value": "128", "lower": "115", "upper": "145"},
        {"indicator": 5, "value": "260", "lower": "150", "upper": "400"},
        {"indicator": 7, "value": "4,8", "lower": "3,3", "upper": "5,5"}]},
    {"title": "Смешанный пример", "years": 3, "months": 6, "sex": "f", "rows": [
        {"indicator": 4, "value": "16,5", "lower": "5,5", "upper": "15,5"},
        {"indicator": 6, "value": "2", "lower": "4", "upper": "12"},
        {"indicator": 8, "value": "18", "lower": "0", "upper": "5"}]},
]

# вопросы-кнопки: ключ -> формулировка для ИИ
QUESTIONS = {
    "what": "Что отражает этот показатель?",
    "simple": "Объясни проще, что это за показатель.",
    "control": "Когда обычно рассматривают контроль этого показателя?",
    "spec": "К какому специалисту обычно обращаются при отклонении этого показателя?",
}


def fmt(x):
    s = f"{x:.2f}".rstrip("0").rstrip(".")
    return s.replace(".", ",")


def parse_num(s):
    s = str(s).strip().replace(",", ".")
    if not re.fullmatch(r"\d{1,6}(\.\d{1,3})?", s):
        raise ValueError
    return float(s)


def validate_and_evaluate(form):
    """form: dict с years, months, sex, rows[]. Возвращает (errors, child, results)."""
    errors = []
    try:
        years = int(form.get("years", ""))
        months = int(form.get("months", "0") or 0)
        if not (0 <= years <= 18 and 0 <= months <= 11) or (years == 18 and months > 0):
            raise ValueError
    except ValueError:
        errors.append("Возраст должен быть от 0 до 18 лет, месяцы от 0 до 11.")
        years = months = 0
    sex = form.get("sex")
    if sex not in ("f", "m"):
        errors.append("Выберите пол.")
    rows = form.get("rows", [])
    if not rows:
        errors.append("Добавьте хотя бы один показатель.")
    if len(rows) > MAX_ROWS:
        errors.append(f"Не больше {MAX_ROWS} показателей за раз.")
        rows = rows[:MAX_ROWS]
    results = []
    for n, r in enumerate(rows, 1):
        try:
            ind = INDICATORS[int(r.get("indicator"))]
        except (KeyError, TypeError, ValueError):
            errors.append(f"Показатель {n}: выберите показатель из списка.")
            continue
        try:
            v, lo, hi = (parse_num(r.get(k, "")) for k in ("value", "lower", "upper"))
        except ValueError:
            errors.append(f"{ind['name']}: значение и границы интервала должны быть числами.")
            continue
        if lo >= hi:
            errors.append(f"{ind['name']}: нижняя граница должна быть меньше верхней.")
            continue
        results.append(evaluate(ind, v, lo, hi))
    return errors, {"years": years, "months": months, "sex": sex}, results


def evaluate(ind, v, lo, hi):
    if v < lo:
        status, diff, base = "below", lo - v, lo
    elif v > hi:
        status, diff, base = "above", v - hi, hi
    else:
        status, diff, base = "within", 0.0, lo
    pct = (diff / base * 100) if base else 0
    span = hi - lo
    scale_lo = min(v, lo - 0.3 * span)
    scale_hi = max(v, hi + 0.3 * span)
    k = 100 / (scale_hi - scale_lo)
    direction = {"below": "ниже", "above": "выше"}.get(status)
    rule = RULES.get((ind["id"], direction)) if direction else None
    return {
        "ind": ind, "value": fmt(v), "lower": fmt(lo), "upper": fmt(hi),
        "status": status, "diff": fmt(diff), "pct": round(pct),
        "pos_v": round((v - scale_lo) * k, 1),
        "pos_lo": round((lo - scale_lo) * k, 1),
        "pos_hi": round((hi - scale_lo) * k, 1),
        "direction": direction, "rule": rule,
        "expl": ind["expl"],
        "rec": rule["text"] if rule else None,
        "ai_used": False,
    }


# ---------------- YandexGPT ----------------

SYSTEM = (
    "Ты помощник-справочник для врача-педиатра в учебном прототипе. Пиши по-русски, коротко и нейтрально. "
    "Используй только тот текст, который тебе дан. Запрещено: ставить диагноз, называть заболевания как вывод, "
    "называть лекарства и дозировки, описывать схемы лечения, добавлять новые рекомендации, давать выводы по сочетанию показателей. "
    "Если вопрос выходит за рамки данного текста, ответь: «Это выходит за рамки справочника, решение за врачом»."
)

FORBIDDEN = re.compile(
    r"диагноз|лечени|лечить|препарат|лекарств|таблетк|антибиот|витамин|добавк|дозиров|дозу|\bмг\b|\bмл\b|схем[аыу] |"
    r"назнач|госпитал|операци|анемия у|лейкоз|онколог|рак\b|диабет\b|сепсис|инфекци",
    re.I)
SPECIALISTS = ["гематолог", "педиатр", "эндокринолог", "кардиолог", "невролог", "онколог", "нефролог",
               "гастроэнтеролог", "инфекционист", "иммунолог", "хирург", "ревматолог"]


def ai_enabled():
    return bool(os.environ.get("YANDEX_API_KEY") and os.environ.get("YANDEX_FOLDER_ID"))


def call_yandexgpt(system, user, max_tokens=350):
    folder = os.environ["YANDEX_FOLDER_ID"]
    model = os.environ.get("YANDEX_MODEL", "yandexgpt-lite")
    resp = requests.post(
        "https://llm.api.cloud.yandex.net/foundationModels/v1/completion",
        headers={"Authorization": "Api-Key " + os.environ["YANDEX_API_KEY"], "x-folder-id": folder},
        json={
            "modelUri": f"gpt://{folder}/{model}/latest",
            "completionOptions": {"stream": False, "temperature": 0.2, "maxTokens": str(max_tokens)},
            "messages": [{"role": "system", "text": system}, {"role": "user", "text": user}],
        },
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()["result"]["alternatives"][0]["message"]["text"].strip()


def check_ai_text(text, allowed_spec):
    """Проверка ответа ИИ. True, если можно показывать."""
    if not text or len(text) > 1200 or FORBIDDEN.search(text):
        return False
    for s in SPECIALISTS:
        if s in text.lower() and s != allowed_spec:
            return False
    return True


def polish(res):
    """Оформляет справку и рекомендацию через ИИ. При любой проблеме остаётся текст справочника."""
    if res["status"] == "within" or not ai_enabled():
        return
    ind = res["ind"]
    spec = ind["spec"] if res["rule"] and "консультация" in res["rule"]["type"] else None
    user = (
        f"Показатель: {ind['name']}. Отклонение: {res['direction']} интервала из бланка.\n"
        f"Справочное пояснение: {ind['expl']}\n"
        f"Правило рекомендации: {res['rule']['text'] if res['rule'] else 'нет правила'}\n\n"
        "Перепиши простым языком в формате двух строк:\n"
        "СПРАВКА: <1-2 предложения по пояснению>\n"
        "РЕКОМЕНДАЦИЯ: <1-2 предложения по правилу, только если правило есть; иначе напиши «нет»>\n"
        "Не добавляй ничего нового."
    )
    try:
        text = call_yandexgpt(SYSTEM, user)
    except Exception:
        return
    m = re.search(r"СПРАВКА:\s*(.+?)\s*(?:РЕКОМЕНДАЦИЯ:\s*(.+))?$", text, re.S)
    if not m:
        return
    expl, rec = m.group(1).strip(), (m.group(2) or "").strip()
    if not check_ai_text(expl, spec) or (rec and not check_ai_text(rec, spec)):
        return
    res["expl"] = expl
    if res["rule"] and rec and rec.lower() != "нет":
        res["rec"] = rec
    res["ai_used"] = True


FREE_TEXT_BAD = re.compile(r"\d{4,}|\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|@|https?:|[A-Za-z0-9._-]+\.(ru|com|org)")


def check_question(q):
    q = (q or "").strip()
    if not q:
        return "Введите вопрос или выберите готовый."
    if len(q) > 120:
        return "Вопрос слишком длинный: не больше 120 символов."
    if FREE_TEXT_BAD.search(q):
        return "В вопросе есть номера, даты или адреса. Такие данные вводить нельзя."
    if len(re.findall(r"\b[А-ЯЁ][а-яё]{2,}\b", q)) > 1:
        return "Не вводите имена и фамилии. Задайте вопрос только о показателе."
    return None


def answer_question(ind_id, direction, qkey, free_text):
    ind = INDICATORS.get(ind_id)
    if not ind or direction not in ("ниже", "выше"):
        return {"ok": False, "text": "Некорректный запрос."}
    if qkey:
        q = QUESTIONS.get(qkey)
        if not q:
            return {"ok": False, "text": "Неизвестный вопрос."}
    else:
        err = check_question(free_text)
        if err:
            return {"ok": False, "text": err}
        q = free_text.strip()
    rule = RULES.get((ind_id, direction))
    fallback = ind["expl"] + (" " + rule["text"] if rule else "")
    if not ai_enabled():
        return {"ok": True, "ai": False, "q": q,
                "text": fallback + " (ИИ не подключён, показан текст справочника.)"}
    spec = ind["spec"] if rule and "консультация" in rule["type"] else None
    user = (f"Показатель: {ind['name']}. Отклонение: {direction} интервала из бланка.\n"
            f"Справочное пояснение: {ind['expl']}\n"
            f"Правило рекомендации: {rule['text'] if rule else 'нет правила'}\n\n"
            f"Вопрос врача: {q}\nОтветь не более чем в 4 предложениях, используя только данный текст.")
    try:
        text = call_yandexgpt(SYSTEM, user)
    except Exception:
        return {"ok": True, "ai": False, "q": q, "text": fallback + " (ИИ недоступен, показан текст справочника.)"}
    if not check_ai_text(text, spec):
        return {"ok": True, "ai": False, "q": q,
                "text": fallback + " (Ответ ИИ не прошёл проверку, показан текст справочника.)"}
    return {"ok": True, "ai": True, "q": q, "text": text}


# ---------------- ограничение частоты ----------------
_hits = {}


def rate_limited(ip, limit=40, window=60):
    now = time.time()
    arr = [t for t in _hits.get(ip, []) if now - t < window]
    arr.append(now)
    _hits[ip] = arr
    return len(arr) > limit
