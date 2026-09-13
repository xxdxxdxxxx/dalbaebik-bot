# Excel — состав и отряды

SQLite — основной runtime-источник данных. Excel используется как удобный источник изменений состава. При `SHEET_AUTO_SYNC=true` бот следит за сохранением файла и запускает импорт после debounce; это не периодический таймер. Ручной запуск доступен через `/sheet_sync`.

## Явный формат `roster`

Лист `roster` должен содержать колонки:

```text
squad | slot | discord_id | game_nick
```

- `squad` и `slot` — положительные целые числа;
- `game_nick` обязателен;
- новый игрок должен иметь `discord_id`;
- дубли слотов, Discord ID и ников блокируют импорт;
- удалённый через `/remove` игрок не возвращается от очередного импорта автоматически.

## Транзакционный импорт

Сначала выполните dry-run:

```bash
python tools/roster_io.py import roster.xlsx --guild-id 123
```

Команда вернёт конфликты и `revision_before`. Если всё верно, примените тот же файл с optimistic revision:

```bash
python tools/roster_io.py import roster.xlsx --guild-id 123 --apply --expected-revision HASH
```

Импорт выполняется одной SQLite-транзакцией, сохраняет identity и историю игрока и не записывает `players.json`.

## Экспорт листа tech

```bash
python tools/roster_io.py export-tech tech.xlsx --guild-id 123
```

Файл создаётся атомарно из текущего активного состава SQLite.
