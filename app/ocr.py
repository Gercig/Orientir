"""Распознавание бланка анализа: Yandex Vision OCR и разбор строк таблицы.

Файл обрабатывается в памяти и нигде не сохраняется.
Из бланка берутся только название показателя, значение, интервал и дата взятия.
Имя пациента и прочий текст бланка не используются и не передаются дальше.
"""
import base64
import os
import re

import requests

import logic

URL = "https://ocr.api.cloud.yandex.net/ocr/v1/recognizeText"
MAX_BYTES = 5 * 1024 * 1024

# Как показатель может называться в бланках разных лабораторий.
ALIASES = {
    1: ["гемоглобин", "hgb", "hb"],
    2: ["эритроциты", "rbc"],
    3: ["гематокрит", "hct"],
    4: ["лейкоциты", "wbc"],
    5: ["тромбоциты", "plt"],
    6: ["соэ", "esr"],
    7: ["глюкоза", "glu", "glucose"],
    8: ["с-реактивный", "с реактивный", "срб", "crp"],
    9: ["ферритин", "ferritin"],
}
NUM = r"\d{1,6}(?:[.,]\d{1,3})?"
RANGE = re.compile(rf"({NUM})\s*[-–—]\s*({NUM})")
LESS = re.compile(rf"[<≤]\s*({NUM})")
DATE = re.compile(r"дата\s*(?:взятия|забора)[^0-9]{0,20}(\d{2})[./](\d{2})[./](\d{4})", re.I)


def sniff(data):
    """Тип файла по первым байтам. Возвращает mime или None."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:5] == b"%PDF-":
        return "application/pdf"
    return None


def enabled():
    return logic.ai_enabled()


def call_vision(data, mime):
    resp = requests.post(
        URL,
        headers={"Authorization": "Api-Key " + os.environ["YANDEX_API_KEY"],
                 "x-folder-id": os.environ["YANDEX_FOLDER_ID"],
                 "x-data-logging-enabled": "false"},
        json={"mimeType": mime, "languageCodes": ["ru", "en"], "model": "table",
              "content": base64.b64encode(data).decode()},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def annotation(js):
    r = js.get("result", js)
    return r.get("textAnnotation", r)


def table_rows(ann):
    """Строки всех таблиц: список списков текстов ячеек."""
    out = []
    for t in ann.get("tables") or []:
        grid = {}
        for c in t.get("cells") or []:
            try:
                grid.setdefault(int(c.get("rowIndex", 0)), {})[int(c.get("columnIndex", 0))] = " ".join(str(c.get("text", "")).split())
            except (TypeError, ValueError):
                continue
        for ri in sorted(grid):
            out.append([grid[ri][ci] for ci in sorted(grid[ri])])
    return out


def text_lines(ann):
    lines = []
    for b in ann.get("blocks") or []:
        for ln in b.get("lines") or []:
            if ln.get("text"):
                lines.append(" ".join(str(ln["text"]).split()))
    if not lines and ann.get("fullText"):
        lines = [" ".join(l.split()) for l in str(ann["fullText"]).splitlines() if l.strip()]
    return lines


def find_indicator(text):
    t = text.lower()
    for iid, names in ALIASES.items():
        for n in names:
            if re.search(rf"(?<![а-яa-z]){re.escape(n)}(?![а-яa-z])", t):
                return iid
    return None


def num(s):
    return float(s.replace(",", "."))


def interval_in(text):
    m = RANGE.search(text)
    if m:
        lo, hi = num(m.group(1)), num(m.group(2))
        return (lo, hi) if lo < hi else None
    m = LESS.search(text)
    if m:
        return 0.0, num(m.group(1))
    return None


def parse_cells(cells):
    """Одна строка таблицы: [название, результат, ед., интервал]."""
    iid = find_indicator(cells[0]) if cells else None
    if not iid or len(cells) < 2:
        return None
    m = re.fullmatch(rf"\s*[<>≤≥]?\s*({NUM})\s*", cells[1])
    if not m:
        return None
    iv = None
    for c in cells[2:]:
        iv = interval_in(c)
        if iv:
            break
    return iid, num(m.group(1)), iv


def parse_line(line):
    """Строка без таблицы: «Лейкоциты 16,5 10^9/л 5,5 - 15,5»."""
    iid = find_indicator(line)
    if not iid:
        return None
    t = line.lower()
    end = max((m.end() for n in ALIASES[iid] for m in re.finditer(re.escape(n), t)), default=0)
    rest = line[end:]
    rest = re.sub(r"\([^)]*\)", " ", rest)                    # скобки вроде (HGB)
    rest = re.sub(r"(?:10|х10|x10)\s*[\^*]\s*\d+", " ", rest)  # единицы 10^9
    iv = interval_in(rest)
    first = re.search(NUM, rest)
    if not first:
        return None
    if iv:
        rm = RANGE.search(rest) or LESS.search(rest)
        if rm and rm.start() <= first.start():
            return None
    return iid, num(first.group(0)), iv


def extract(js):
    """Возвращает {'rows': [...], 'date': 'YYYY-MM-DD' или None, 'skipped': число}."""
    ann = annotation(js)
    found, seen = [], set()
    for cells in table_rows(ann):
        p = parse_cells(cells)
        if p and p[0] not in seen:
            seen.add(p[0])
            found.append(p)
    if not found:
        for line in text_lines(ann):
            p = parse_line(line)
            if p and p[0] not in seen:
                seen.add(p[0])
                found.append(p)
    rows = []
    for iid, v, iv in found:
        ind = logic.INDICATORS[iid]
        rows.append({"indicator": iid, "name": ind["name"], "unit": ind["unit"], "value": logic.fmt(v),
                     "lower": logic.fmt(iv[0]) if iv else "", "upper": logic.fmt(iv[1]) if iv else ""})
    full = ann.get("fullText") or "\n".join(text_lines(ann)) + "\n" + "\n".join(" ".join(r) for r in table_rows(ann))
    m = DATE.search(full)
    date = f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None
    return {"rows": rows, "date": date}
