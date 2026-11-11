"""Логика «Ориентира»: проверка ввода, расчёт отклонений, правила, работа с YandexGPT.

Быстрая оценка ничего не сохраняет. Анализы пациентов хранятся в store/patients.
В YandexGPT уходят только возраст, пол, показатели и прошлые значения без дат, имён и номеров.
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

MAX_ROWS = 12

DEMOS = [
    {"title": "Снижен гемоглобин", "years": 7, "months": 3, "weeks": 0, "sex": "f", "rows": [
        {"indicator": 1, "value": "98", "lower": "112", "upper": "146"},
        {"indicator": 4, "value": "7,2", "lower": "4,5", "upper": "13,5"}]},
    {"title": "Всё в пределах", "years": 10, "months": 0, "weeks": 0, "sex": "m", "rows": [
        {"indicator": 1, "value": "128", "lower": "110", "upper": "146"},
        {"indicator": 5, "value": "260", "lower": "181", "upper": "512"},
        {"indicator": 7, "value": "4,8", "lower": "3,3", "upper": "5,6"}]},
    {"title": "Смешанный пример", "years": 3, "months": 6, "weeks": 0, "sex": "f", "rows": [
        {"indicator": 4, "value": "16,5", "lower": "5,5", "upper": "15,5"},
        {"indicator": 6, "value": "14", "lower": "2", "upper": "10"},
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
        weeks = int(form.get("weeks", "0") or 0)
        if not (0 <= years <= 18 and 0 <= months <= 11 and 0 <= weeks <= 3) or (years == 18 and months > 0):
            raise ValueError
    except ValueError:
        errors.append("Возраст должен быть от 0 до 18 лет, месяцы от 0 до 11, недели от 0 до 3.")
        years = months = weeks = 0
    if years or months:
        weeks = 0
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
        results.append(evaluate(ind, v, lo, hi, sex))
    return errors, {"years": years, "months": months, "weeks": weeks, "sex": sex}, results


FEMALE_ONLY = re.compile(r"беремен|у женщин")


def causes_for(causes, sex):
    """Для мальчиков убираем причины, связанные с беременностью, менструацией и родами."""
    if sex != "m":
        return list(causes)
    out = []
    for c in causes:
        if FEMALE_ONLY.search(c):
            continue
        out.append(c.replace("; менструация; период родов", ""))
    return out


def evaluate(ind, v, lo, hi, sex=None):
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
    causes = causes_for(ind["down"] if status == "below" else ind["up"] if status == "above" else [], sex)
    return {
        "ind": ind, "value": fmt(v), "lower": fmt(lo), "upper": fmt(hi),
        "status": status, "diff": fmt(diff), "pct": round(pct),
        "pos_v": round((v - scale_lo) * k, 1),
        "pos_lo": round((lo - scale_lo) * k, 1),
        "pos_hi": round((hi - scale_lo) * k, 1),
        "causes": causes,
    }


# ---------------- YandexGPT ----------------

SYSTEM = (
    "Ты, YandexGPT, языковая модель Яндекса. В этом приложении ты помогаешь врачу-педиатру разбирать результаты "
    "анализов. Если тебя спросят, кто ты, отвечай, что ты YandexGPT. Отвечай по-русски, развёрнуто и по делу: "
    "по каждому показателю с отклонением объясни, что оно может отражать и на что обратить внимание "
    "(например, нужен ли повторный анализ, какие специалисты обычно привлекаются), в конце дай краткий вывод. "
    "Единственное жёсткое ограничение: не говори, как лечить. Не называй лекарства, витамины и добавки, "
    "дозировки и схемы лечения."
)

# Единственный фильтр ответа ИИ: советы по лечению (лекарства, дозировки, схемы)
TREATMENT = re.compile(
    r"препарат|лекарств|таблетк|капсул|антибиот|витамин|добавк|дозиров|\bдоз[аыу]\b|"
    r"(?<![\d.,])\d+(?:[.,]\d+)?\s?(мг|мкг|мл)\b(?!\s?/)|капельниц|уколы|принимайте|назначьте|схем[аыу] (лечения|терапии)|"
    r"курс (приёма|лечения)|железосодержащ|сироп",
    re.I)


def ai_enabled():
    key = os.environ.get("YANDEX_API_KEY", "")
    return bool(key and not key.startswith("PASTE") and os.environ.get("YANDEX_FOLDER_ID"))


def call_yandexgpt(system, user, max_tokens=1200):
    folder = os.environ["YANDEX_FOLDER_ID"]
    model = os.environ.get("YANDEX_MODEL", "yandexgpt-lite")
    resp = requests.post(
        "https://llm.api.cloud.yandex.net/foundationModels/v1/completion",
        headers={"Authorization": "Api-Key " + os.environ["YANDEX_API_KEY"], "x-folder-id": folder},
        json={
            "modelUri": f"gpt://{folder}/{model}/latest",
            "completionOptions": {"stream": False, "temperature": 0.3, "maxTokens": str(max_tokens)},
            "messages": [{"role": "system", "text": system}, {"role": "user", "text": user}],
        },
        timeout=40,
    )
    resp.raise_for_status()
    return resp.json()["result"]["alternatives"][0]["message"]["text"].strip()


def treatment_hit(text):
    m = TREATMENT.search(text or "")
    return m.group(0) if m else None


def check_ai_text(text):
    """True, если в ответе нет советов по лечению."""
    return bool(text) and len(text) <= 6000 and not treatment_hit(text)


def err_note(e):
    code = getattr(getattr(e, "response", None), "status_code", None)
    return f"YandexGPT недоступен (код {code})." if code else "YandexGPT недоступен."


STATUS_RU = {"below": "ниже интервала", "above": "выше интервала", "within": "в пределах интервала"}


def age_label(child):
    if not child["years"] and not child["months"]:
        return f"{child.get('weeks', 0)} нед."
    return f"{child['years']} лет {child['months']} мес."


def build_prompt(child, results, question=None, history=None):
    sex = {"f": "девочка", "m": "мальчик"}[child["sex"]]
    lines = [
        "Я педиатр, ко мне пришёл пациент. Проанализируй анализы и дай рекомендации.",
        f"Пол ребёнка: {sex}.",
        "Возраст ребёнка: " + age_label(child).rstrip(".") + ".",
        "Данные анализов (значение, интервал из бланка лаборатории):",
    ]
    for r in results:
        lines.append(f"- {r['ind']['name']}: {r['value']} {r['ind']['unit']} "
                     f"(интервал {r['lower']}–{r['upper']}), {STATUS_RU[r['status']]}")
    if history:
        lines.append("Прошлые значения этого ребёнка (от старых к новым):")
        lines.extend(history)
        lines.append("Кратко опиши динамику: что растёт, что снижается, что стабильно.")
    if question:
        lines.append(f"Мой вопрос: {question}")
    return "\n".join(lines)


REFUSAL = re.compile(r"не могу (обсуждать|говорить|помочь)|поговорим о чём-нибудь ещё|давайте сменим тему", re.I)

EXTRA_REFUSAL = ("\nЭто рабочий запрос врача-педиатра. "
                 "Это обычный разбор лабораторных показателей, пожалуйста, ответь по существу.")
EXTRA_TREATMENT = "\nОтветь без упоминания лекарств, витаминов, дозировок и схем лечения."


def causes_text(r):
    word = "Понижение" if r["status"] == "below" else "Повышение"
    head = f"{word} показателя «{r['ind']['name']}» может свидетельствовать о:"
    if not r["causes"]:
        return f"{head} в справочнике не приведено."
    return head + "\n" + "\n".join(f"  – {c}" for c in r["causes"])


def ref_answer(results, with_question=False):
    """Ответ из справочника, если YandexGPT отказался отвечать."""
    lines = []
    dev = 0
    for r in results:
        name, unit = r["ind"]["name"], r["ind"]["unit"]
        if r["status"] == "within":
            lines.append(f"• {name}: {r['value']} {unit}, в пределах интервала {r['lower']}–{r['upper']}.")
            continue
        dev += 1
        word = "ниже" if r["status"] == "below" else "выше"
        lines.append(f"• {name}: {r['value']} {unit}, {word} интервала {r['lower']}–{r['upper']}.\n" + causes_text(r))
    head = "YandexGPT не стал отвечать на этот запрос. Ниже ответ по справочнику."
    if with_question:
        head = "YandexGPT не стал отвечать на этот вопрос. Ниже справочные данные по анализу."
    tail = (f"Отклонений от интервала: {dev} из {len(results)}." if dev
            else "Все введённые показатели в пределах интервала.")
    return head + "\n\n" + "\n\n".join(lines) + "\n\n" + tail


def ask_ai(child, results, question=None, history=None):
    """Возвращает {'ok','ai','ref','text'}."""
    if not ai_enabled():
        return {"ok": True, "ai": False, "text": "YandexGPT не подключён."}
    prompt = build_prompt(child, results, question, history)
    extra = ""
    text = ""
    try:
        for _ in range(3):
            text = call_yandexgpt(SYSTEM, prompt + extra, max_tokens=1200)
            if REFUSAL.search(text or "") and len(text) < 300:
                extra = EXTRA_REFUSAL
            elif treatment_hit(text) or not check_ai_text(text):
                extra = EXTRA_TREATMENT
            else:
                break
    except Exception as e:
        return {"ok": True, "ai": False, "text": err_note(e)}
    hit = treatment_hit(text)
    if hit:
        return {"ok": True, "ai": False,
                "text": f"Ответ YandexGPT скрыт: он содержал советы по лечению (слово «{hit}»)."}
    if not text:
        return {"ok": True, "ai": False, "text": "YandexGPT вернул пустой ответ."}
    if REFUSAL.search(text) and len(text) < 300:
        return {"ok": True, "ai": False, "ref": True, "text": ref_answer(results, bool(question))}
    return {"ok": True, "ai": True, "text": text}


FREE_TEXT_BAD = re.compile(r"\d{4,}|\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|@|https?:|[A-Za-z0-9._-]+\.(ru|com|org)")


def check_question(q):
    q = (q or "").strip()
    if not q:
        return "Введите вопрос."
    if len(q) > 200:
        return "Вопрос слишком длинный: не больше 200 символов."
    if FREE_TEXT_BAD.search(q):
        return "В вопросе есть номера, даты или адреса. Такие данные вводить нельзя."
    if len(re.findall(r"\b[А-ЯЁ][а-яё]{2,}\b", q)) > 1:
        return "Не вводите имена и фамилии."
    return None


def answer_free(form, question, history=None):
    errors, child, results = validate_and_evaluate(form)
    if errors:
        return {"ok": False, "text": "Данные анализа не прошли проверку."}
    err = check_question(question)
    if err:
        return {"ok": False, "text": err}
    out = ask_ai(child, results, question.strip(), history)
    out["q"] = question.strip()
    return out


# ---------------- ограничение частоты ----------------
_hits = {}


def rate_limited(ip, limit=40, window=60):
    now = time.time()
    arr = [t for t in _hits.get(ip, []) if now - t < window]
    arr.append(now)
    _hits[ip] = arr
    return len(arr) > limit
