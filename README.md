# КВ STALCRAFT · Discord-бот

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
| Этап III (финал) | **21:20** |

### Воскресенье — **4 этапа**

| Этап | Окно | Скан |
|------|------|------|
| Явка | **18:30 – 19:00** | |
| I | **19:00 – 19:20** | база **19:00** · I **19:20** |
| II | **19:20 – 19:40** | II **19:40** |
| III | **19:40 – 20:00** | III **20:00** |
| IV | **20:00 – 20:20** | IV **20:20** (финал) |

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

## Команды

`/help` — полный список.

| | |
|--|--|
| Состав | `/add` · `!add` · `/remove` · `/list` |
| Excel | `/sheet_sync` · `/squad_list` |
| КВ | `/map` · `/refresh` · `/scan_now`* · `/reset_session`* · `/deletegren`* |

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
