# КВ STALCRAFT · Discord-бот

## Voice snapshot в SQLite

`voice_speak_seconds` остаётся JSON dual-write для совместимости и зеркалируется в `voice_stats` без блокировки event loop. Ключ `legacy-kv:<session_date>` отделяет КВ-сессии; повторная синхронизация заменяет snapshot и не суммирует его повторно. Если `session_date` отсутствует, применяется `legacy-kv:undated-current`: это стабильный ключ текущего недатированного snapshot, а не выдуманная дата. `player_id` назначается только по точному `discord_bindings`; непривязанные Discord-записи сохраняются с `NULL player_id`. Ручной `/voice_scan_start` намеренно не сохраняется.


Явка по войсам · отряды из Excel · сканы гранат · карты этапов.

## Расписание (МСК)

### Чт / пт / сб

| | Время |
|--|--------|
| Явка (✅) | **19:30 – 20:05** |
| ЛС неявившимся | 19:50 _(не Чемпионы/замены)_ |
| База грен | **20:05** |
| Этап I | **20:25** |
| Этап II | **20:50** |
| Этап III (финал) | **21:15** |

### Воскресенье — **4 этапа**

| Этап | Окно | Скан |
|------|------|------|
| Явка | **18:30 – 19:00** | |
| I | **19:00 – 19:20** | база **19:00** · I **19:20** |
| II | **19:20 – 19:40** | II **19:40** |
| III | **19:40 – 20:00** | III **20:00** |
| IV | **20:00 – 20:15** | IV **20:15** (финал) |

## ГРАНАТЫ

Скан eAPI `gre-thr` (1 запрос profile на ника):

- **отряды 1–6 + Чемпионы**
- **не** «Без отряда»
- **не** зависит от войса / ✅

В embed: дельты за I / II / III (+ IV вс) и **ИТОГ**.  
Данные **висят** после КВ до следующего старта явки (**19:30** / вс **18:30**) — тогда обнуление.

## Карты

`/map` · `!map` — три карты на I–III (вс: +IV):

```
!map хвойник берда низина
```

## Структура

```
ds bot stalzone/
├── bot.py
├── players.json
├── scan_stats.sqlite3  # создаётся автоматически, локальная статистика /scan
├── ДИТЯ22.xlsx
├── requirements.txt
├── .env / .env.example
├── phrases/
├── scans/etapy|itogi/
└── docs/EXCEL.md
```

## Запуск

1. Python 3.11+
2. Discord intents: Server Members + Message Content
3. `.env` из `.env.example`
4. ```bash
   pip install -r requirements.txt
   python bot.py
   ```
5. `/setup` → `/access_add` → состав / Excel

## SQLite и совместимость

`scan_stats.sqlite3` — единая база идентичности игроков, Discord-привязок, OCR-алиасов, гранат и `/scan`. Полный путь определяется как `<папка проекта>\scan_stats.sqlite3`. Таблицы сканов: `scans` (одна запись на таб) и `scan_players` (распознанные строки). При обычном `python bot.py` схема создаётся и legacy-данные из `players.json` импортируются идемпотентно. Текущий JSON сохраняется для совместимости интерфейса и сессионных полей; `/add`, гранатные сканы и `/scan` одновременно обновляют SQLite по единому `player_id`.

Посмотреть содержимое без запуска бота:

```bash
python -c "import sqlite3; c=sqlite3.connect('scan_stats.sqlite3'); print(list(c.execute(\"SELECT name FROM sqlite_master WHERE type='table' ORDER BY name\"))); print(list(c.execute(\"SELECT * FROM scans ORDER BY id DESC LIMIT 10\")))"
python -c "import sqlite3; c=sqlite3.connect('scan_stats.sqlite3'); print(list(c.execute(\"SELECT * FROM scan_players ORDER BY scan_id DESC, place LIMIT 100\")))"
```

Если установлен консольный клиент SQLite: `sqlite3 scan_stats.sqlite3`, затем `.tables`, `.schema scans` и `SELECT * FROM scans ORDER BY id DESC LIMIT 10;`.

OCR-алиасы добавляются только явно командой `/alias_add` с теми же правами, что `/add`; конфликт с другим игроком отклоняется. Автоматического добавления алиасов при startup/migration нет.

Проверка миграции без изменения рабочей БД:

```bash
python -m py_compile bot.py player_store.py tools/test_player_store.py
python tools/test_player_store.py
```

## Команды

`/help` — полный список.

| | |
|--|--|
| Состав | `/add` · `!add` · `/remove` · `/list` |
| Excel | `/sheet_sync` · `/squad_list` |
| КВ | `/map` · `/stats` · `/refresh` · `/scan_now`* · `/reset_session`* · `/deletegren`* |

## Админ-панель базы (локальная)

Запускается автоматически вместе с ботом (`python bot.py`) → <http://127.0.0.1:8787> (только этот ПК, снаружи не видно). Отдельно: `python tools/admin_ui.py`. Выключить: `ADMIN_UI=0` в `.env`, порт: `ADMIN_PORT`.

- **Дни** — все даты КВ: сколько табов, гранат, войса.
- **День** — по каждому игроку: таб (У/С/П/СЧЁТ), гранаты по этапам, войс.
  Всё редактируется прямо в таблице: «Сохранить» пишет в SQLite, «Всего»
  пересчитывается как сумма этапов. Пустая ячейка этапа удаляет запись этапа,
  `0` — оставляет ноль. Кнопка ✕ у строки удаляет гранаты+войс игрока за день
  (включая runtime-снапшоты счётчика), ✕ у таба — весь таб.
- **Игроки → игрок** — история по дням, чтобы найти накрутку (подсветка ⚠),
  каждая дата ведёт в редактор дня.

Правки сразу попадают в `/stats` (исторические данные бот читает из SQLite
напрямую); перезапускать бота не нужно. Требуется `flask` (см. requirements).

## Табы КВ по датам

- `/scan date map screenshot` — дата обязательна; форматы `DD.MM.YY`, `DD.MM.YYYY`, `YYYY-MM-DD`.
- `/scan_view scan_id` — приватный сохранённый таб с датой КВ.
- `/scan_dates` — приватный список дат и количества табов сервера.
- `/stats date_from date_to` — фильтр У/С/П/СЧЁТ и ЭФФ. Одна граница означает один день. ГРЕНЫ/ВРЕМЯ не фильтруются датой табов, потому что их даты хранятся отдельными сессиями.

## `/stats`

Показывает всех известных игроков в порядке: **Отряд 1…6 → Чемпионы → Без отряда**.
Строка игрока: `ник — У x.x · С x.x · П x.x · Гранаты x.x · Время мм:сс`.

- У/С/П усредняются по сохранённым строкам `/scan` (одна строка таблицы = один матч/скан).
- Гранаты и время усредняются по завершённым КВ-сессиям из `scans/itogi`; незавершённая текущая сессия добавляется из `players.json`.
- Отсутствующие показатели не считаются нулём и выводятся как `—`.
- Длинный результат автоматически делится на несколько embed-страниц.

\* access-роль или админ.

## Удалить из состава

`/remove @user` или `!remove @user` — по Discord.

`/remove ник` или `!remove ник` — по **ник STALCRAFT** (sosew, Teipo, ЛомаюЛицаКвезикс и т.д.) — как в таблице Excel.

Пример: `!remove sosew` — удалит человека даже если ники немного отличаются по регистру/пробелам.

## Excel

См. [docs/EXCEL.md](docs/EXCEL.md). Лист **tech** обновляется после add/remove.  
Чемпионы (F) — без ЛС перед КВ.

## Отладка

Общая отладка по умолчанию выключена:

```env
APP_DEBUG=false
APP_LOG_LEVEL=
```

`APP_LOG_LEVEL` необязателен. При `APP_DEBUG=false` штатные INFO-сообщения подключения войса и voice gateway скрыты, но WARNING/ERROR остаются видны; при `APP_DEBUG=true` эти INFO выводятся. Калибровка громкости работает независимо от `APP_DEBUG` и не записывает аудио или текст речи. `VOICE_SCAN_GATE_DEBUG` устарел и поддерживается только как deprecated fallback.

## Калибровка громкости войса

Калибровка включается и настраивается независимо от общей отладки:

```env
VOICE_VOLUME_CALIBRATION_DEBUG=false
VOICE_VOLUME_CALIBRATION_INTERVAL_SECONDS=1.0
VOICE_VOLUME_CALIBRATION_USER_IDS=
```

В `VOICE_VOLUME_CALIBRATION_USER_IDS` укажите цифровые Discord ID пользователей через запятую. Пустое значение отключает фильтр и собирает диагностические строки для всех eligible users; любая невалидная запись приводит к ошибке конфигурации при запуске.

### Процедура сбора

Для каждого пользователя:

1. Соберите **10–15 секунд тишины**.
2. Затем соберите **10–15 секунд обычной речи**.
3. Пришлите все полученные строки `VOICE_CAL` без выборочного удаления.

Рекомендуемые параметры голосового порога:

```env
VOICE_SCAN_DBFS_THRESHOLD=-55
VOICE_SCAN_ADAPTIVE_THRESHOLD=false
VOICE_SCAN_MARGIN_DB=12
VOICE_SCAN_ATTACK_MS=60
VOICE_SCAN_RELEASE_MS=1000
```


## Фактический синтаксис датированных команд

- `/scan date:<ДД.ММ.ГГ|ДД.ММ.ГГГГ|ГГГГ-ММ-ДД> map:<карта> screenshot:<файл>`
- `/scan_now date:<дата>` — обязательная дата КВ, полный ручной grenade scan.
- `/voice_scan_start date:<дата>` и `/voice_scan_stop`.
- `/stats [date:<дата>] [date_from:<дата> date_to:<дата>]`.
- `/stats_dates` — приватный список `Дата | Табы | Гранаты | Войс`.
- `/scan_dates` сохранён для списка дат сканов табов.


## ╨п╨▓╨╜╤Л╨╡ import/export ╨╛╨┐╨╡╤А╨░╤Ж╨╕╨╕

Excel ╨╜╨╡ ╤Б╨╕╨╜╤Е╤А╨╛╨╜╨╕╨╖╨╕╤А╤Г╨╡╤В╤Б╤П ╨╜╨░ startup ╨╕╨╗╨╕ ╨┐╨╛ ╤В╨░╨╣╨╝╨╡╤А╤Г. ╨Я╨╛ ╤Г╨╝╨╛╨╗╤З╨░╨╜╨╕╤О import ╨▓╤Л╨┐╨╛╨╗╨╜╤П╨╡╤В dry-run ╨╕ ╨▓╨╛╨╖╨▓╤А╨░╤Й╨░╨╡╤В conflicts + revision:

```bash
python tools/roster_io.py import roster.xlsx --guild-id 123
python tools/roster_io.py import roster.xlsx --guild-id 123 --apply --expected-revision HASH
python tools/roster_io.py export-tech tech.xlsx --guild-id 123
```

╨Ш╨╝╨┐╨╛╤А╤В ╤В╤А╨░╨╜╨╖╨░╨║╤Ж╨╕╨╛╨╜╨╜╤Л╨╣, ╨╜╨╡ ╤Г╨┤╨░╨╗╤П╨╡╤В identity/history ╨╕ ╨╜╨╕╨║╨╛╨│╨┤╨░ ╨╜╨╡ ╨╝╨╡╨╜╤П╨╡╤В `players.json`. `players.json` ╨┐╤А╨╡╨┤╨╜╨░╨╖╨╜╨░╤З╨╡╨╜ ╤В╨╛╨╗╤М╨║╨╛ ╨┤╨╗╤П ╨╛╨┤╨╜╨╛╤А╨░╨╖╨╛╨▓╨╛╨│╨╛ legacy import ╨┤╨╛ marker ╨╕ ╤П╨▓╨╜╤Л╤Е snapshot/backup ╨╛╨┐╨╡╤А╨░╤Ж╨╕╨╣.
