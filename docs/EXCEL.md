# Excel ┬╖ ╤П╨▓╨╜╤Л╨╣ import/export

SQLite тАФ ╨╡╨┤╨╕╨╜╤Б╤В╨▓╨╡╨╜╨╜╤Л╨╣ runtime source. ╨С╨╛╤В ╨╜╨╡ ╤З╨╕╤В╨░╨╡╤В Excel ╨┐╤А╨╕ ╨╖╨░╨┐╤Г╤Б╨║╨╡ ╨╕ ╨╜╨╡ ╤Б╨╕╨╜╤Е╤А╨╛╨╜╨╕╨╖╨╕╤А╤Г╨╡╤В ╨╡╨│╨╛ ╨┐╨╛ ╤В╨░╨╣╨╝╨╡╤А╤Г.

## Import roster

╨Ы╨╕╤Б╤В `roster`: `squad | slot | discord_id | game_nick`. ╨б╨╜╨░╤З╨░╨╗╨░ dry-run:

```bash
python tools/roster_io.py import roster.xlsx --guild-id 123
```

╨Я╤А╨╕╨╝╨╡╨╜╨╡╨╜╨╕╨╡ ╤Б optimistic revision:

```bash
python tools/roster_io.py import roster.xlsx --guild-id 123 --apply --expected-revision HASH
```

╨Ш╨╝╨┐╨╛╤А╤В ╨▓╨░╨╗╨╕╨┤╨╕╤А╤Г╨╡╤В ╨┤╤Г╨▒╨╗╨╕/╨║╨╛╨╜╤Д╨╗╨╕╨║╤В╤Л, ╨▓╤Л╨┐╨╛╨╗╨╜╤П╨╡╤В╤Б╤П ╨╛╨┤╨╜╨╛╨╣ SQLite-╤В╤А╨░╨╜╨╖╨░╨║╤Ж╨╕╨╡╨╣, ╤Б╨╛╤Е╤А╨░╨╜╤П╨╡╤В identity/history ╨╕ ╨╜╨╡ ╨┐╨╕╤И╨╡╤В JSON.

## Export tech

```bash
python tools/roster_io.py export-tech tech.xlsx --guild-id 123
```

╨д╨░╨╣╨╗ ╨░╤В╨╛╨╝╨░╤А╨╜╨╛ ╤Б╨╛╨╖╨┤╨░╤С╤В╤Б╤П ╤В╨╛╨╗╤М╨║╨╛ ╨╕╨╖ SQLite.
