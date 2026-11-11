# Ориентир

Веб-помощник врача-педиатра: сверка анализов ребёнка с возрастными интервалами, возможные причины отклонений, разбор YandexGPT, карточки пациентов с динамикой, загрузка бланков (Yandex Vision).

Стек: Python 3, Flask, SQLite, gunicorn. Облако: Yandex Cloud (ВМ, YandexGPT, Vision).

## Структура

| Путь | Назначение |
|---|---|
| `app/main.py` | маршруты, вход, роли, защита форм |
| `app/logic.py` | сверка с интервалами, промты YandexGPT, запасные тексты |
| `app/patients.py` | пациенты, анализы, динамика, графики, демо-данные |
| `app/ocr.py` | распознавание бланков (Yandex Vision) |
| `app/store.py` | база SQLite, журнал действий, статистика |
| `app/templates/`, `app/static/` | страницы и оформление |
| `spravochnik.json` | интервалы показателей и причины отклонений |

## Запуск у себя

```
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # впишите свои значения
set -a; source .env; set +a
cd app && python main.py    # http://127.0.0.1:8000
```

Без ключей Yandex приложение работает: вместо ИИ показываются тексты из справочника, распознавание бланков недоступно.

## Переменные окружения

`YANDEX_API_KEY`, `YANDEX_FOLDER_ID`, `YANDEX_MODEL`, `ORIENTIR_DATA`, `ORIENTIR_ADMIN_LOGIN`, `ORIENTIR_ADMIN_PASSWORD`.
Значения здесь не хранятся. На сервере они лежат в `/etc/orientir.env` (права 0600).

Для ключа сервисного аккаунта нужны роли `ai.languageModels.user` и `ai.vision.user`.

## База данных

Файл `orientir.db` (SQLite) в папке `ORIENTIR_DATA`. Таблицы: `patients`, `analyses`, `analysis_values`, `audit` (и пользователи). Открыть: `sqlite3 orientir.db` или DB Browser for SQLite. Реальные данные пациентов на личные компьютеры не копировать, для разработки использовать демо-данные (кнопка в панели администратора).

## Сервер

Сервис `orientir.service` (systemd): gunicorn на порту 8080, рабочая папка `/opt/orientir/app`, окружение из `/etc/orientir.env`, база на отдельном диске `/data`.
Перезапуск: `sudo systemctl restart orientir`. Логи: `journalctl -u orientir -f`.

## Правила работы с кодом

- Ключи, пароли и cloud-init-файлы в репозиторий не добавлять (`.gitignore` это учитывает).
- Один коммит — одно понятное изменение.
- Справочник (`spravochnik.json`) после правки проверить на нескольких возрастах и показателях.
