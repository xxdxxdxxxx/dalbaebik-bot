from __future__ import annotations
import json
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import player_store
import bot


def main() -> None:
    expected_scan_maps = ("Хвойник", "Бердовка", "Низина")
    mojibake_markers = ("╨", "╤", "Р", "С")
    assert bot.SCAN_MAP_CHOICES == expected_scan_maps
    assert all(name in bot.MAP_ALIASES for name in expected_scan_maps)
    assert all(not any(marker in name for marker in mojibake_markers) for name in expected_scan_maps)
    scan_command = bot.bot.tree.get_command("scan")
    map_parameter = next(
        parameter for parameter in scan_command.parameters if parameter.display_name == "map"
    )
    assert [(choice.name, choice.value) for choice in map_parameter.choices] == [
        (name, name) for name in expected_scan_maps
    ]

    working_db = Path('scan_stats.sqlite3')
    before = working_db.read_bytes() if working_db.exists() else None
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        db = root / 'test.sqlite3'
        legacy = root / 'players.json'
        legacy.write_text(json.dumps({
            'players': {'123': {'game_nick': 'iovecuit', 'discord_name': 'D', 'discord_username': 'u'}},
            'grenade_history': {'lovecult': {'20:00': 10}, 'iovecuit': {'20:25': 12}},
            'session_date': '2026-08-16',
            'kv_session_active': True,
            'kv_finished': True,
            'voice_speak_seconds': {'123': 12.5, '999': 3.25},
        }), encoding='utf-8')
        # Add legacy scan schema/row before unified migration.
        with closing(sqlite3.connect(db)) as con:
            con.executescript('''
            CREATE TABLE scans(id INTEGER PRIMARY KEY, stage INTEGER, map TEXT, guild_id INTEGER, channel_id INTEGER, user_id INTEGER, scanned_at TEXT, source_filename TEXT);
            CREATE TABLE scan_players(id INTEGER PRIMARY KEY, scan_id INTEGER, place INTEGER, nick TEXT, kills INTEGER, deaths INTEGER, assists INTEGER, score INTEGER);
            INSERT INTO scans VALUES(1,1,'map',1,1,1,'now','x.png');
            INSERT INTO scan_players VALUES(1,1,1,'lovecult',1,2,3,4);
            ''')
        player_store.ensure_schema(db)
        player_store.upsert_binding(db, '123', 'iovecuit', 'D', 'u')
        player_store.add_alias(db, 'iovecuit', 'lovecult')
        first = player_store.migrate(db, legacy, backup=False)
        second = player_store.migrate(db, legacy, backup=False)
        assert first['counts'] == second['counts'], (first, second)
        with closing(player_store.connect(db)) as con:
            binding = con.execute("SELECT player_id FROM discord_bindings WHERE discord_id='123'").fetchone()[0]
            alias = con.execute("SELECT player_id FROM player_aliases WHERE alias='lovecult'").fetchone()[0]
            scan = con.execute("SELECT player_id FROM scan_players WHERE nick='lovecult'").fetchone()[0]
            grenade_ids = {r[0] for r in con.execute("SELECT DISTINCT player_id FROM grenade_stats")}
            assert binding == alias == scan
            assert grenade_ids == {binding}
            flags = con.execute("SELECT active,finished FROM runtime_sessions").fetchone()
            assert tuple(flags) == (0, 1)
            voice = con.execute("SELECT player_id,seconds FROM voice_stats ORDER BY discord_id").fetchall()
            assert len(voice) == 2 and voice[0][0] == binding and voice[1][0] is None
            assert [row[1] for row in voice] == [12.5, 3.25]
        # Semantic scan idempotency: key order/transport metadata/row order do not matter.
        scan_rows = [
            {'place': 1, 'nickname': 'iovecuit', 'kills': 10, 'deaths': 2,
             'assists': 3, 'score': 100, 'technical_note': 'ignored'},
            {'place': 2, 'nickname': 'Other', 'kills': 5, 'deaths': 4,
             'assists': 1, 'score': 50},
        ]
        save_args = dict(match_date='2026-08-16', map_name='Хвойник', guild_id=42,
                         channel_id=10, user_id=123, scanned_at='2026-08-16T20:00:00Z',
                         source_filename='first.png')
        scan_db = root / 'scan-idempotency.sqlite3'
        saved = player_store.save_scan_result(scan_db, scan_rows, **save_args)
        assert saved.created
        reopened_duplicate = player_store.save_scan_result(
            scan_db, list(reversed(scan_rows)), **{
                **save_args, 'channel_id': 999, 'user_id': 456,
                'scanned_at': '2026-08-16T20:01:00Z', 'source_filename': 'retry.png',
            }
        )
        assert not reopened_duplicate.created and reopened_duplicate.scan_id == saved.scan_id
        changed_rows = [dict(row) for row in scan_rows]
        changed_rows[0]['score'] = 101
        changed = player_store.save_scan_result(scan_db, changed_rows, **save_args)
        assert changed.created and changed.scan_id != saved.scan_id
        with closing(player_store.connect(scan_db)) as con:
            assert con.execute('SELECT COUNT(*) FROM scans').fetchone()[0] == 2
            assert con.execute('SELECT COUNT(*) FROM scan_players').fetchone()[0] == 4
            assert con.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == str(player_store.SCHEMA_VERSION)
            indexes = {row[1] for row in con.execute("PRAGMA index_list('scans')")}
            assert 'uq_scans_content_fingerprint' in indexes

        # Nullable score stays unknown (not zero), changes the fingerprint, and guild scope is enforced.
        unknown_rows = [dict(scan_rows[0], score=None)]
        unknown = player_store.save_scan_result(scan_db, unknown_rows, **save_args)
        assert unknown.created and unknown.scan_id not in {saved.scan_id, changed.scan_id}
        own_scan = player_store.get_scan_for_guild(scan_db, unknown.scan_id, 42)
        assert own_scan is not None and own_scan[1][0]['score'] is None
        assert player_store.get_scan_for_guild(scan_db, unknown.scan_id, 999) is None
        assert '—' in player_store.format_scan_private_report(
            own_scan[1], match_date='2026-08-16', map_name='Хвойник', save_result=unknown,
        )[0]

        # Private report: normalized saved fields, explicit status, nick truncation and splitting.
        report = player_store.format_scan_private_report(
            [dict(scan_rows[0], nickname='VeryLongNicknameThatMustBeShortened')],
            match_date='2026-08-16', map_name='Хвойник', save_result=saved,
        )
        assert len(report) == 1
        assert 'создан новый скан' in report[0] and 'Дата КВ: 2026-08-16' in report[0]
        assert 'VeryLongNicknameT…' in report[0] and '10' in report[0] and '100' in report[0]
        duplicate_report = player_store.format_scan_private_report(
            scan_rows, match_date='2026-08-16', map_name='Хвойник', save_result=reopened_duplicate,
        )
        assert 'обнаружен дубль' in duplicate_report[0]
        many_rows = [dict(scan_rows[0], place=i, nickname=f'player_{i}') for i in range(1, 80)]
        chunks = player_store.format_scan_private_report(
            many_rows, match_date='2026-08-19', map_name='Низина', save_result=saved, limit=500,
        )
        assert len(chunks) > 1 and all(len(chunk) <= 500 for chunk in chunks)
        assert sum(chunk.count('player_') for chunk in chunks) == len(many_rows)

        # Unicode casefold identity, distinct scan aggregation, and exact dedupe.
        unicode_db = root / 'unicode.sqlite3'
        unicode_pid = player_store.upsert_binding(unicode_db, '777', 'Стальной_алекс')
        unicode_args = dict(match_date='2026-08-18', map_name='Бердовка', guild_id=7,
                            channel_id=1, user_id=7, scanned_at='2026-08-18T10:00:00Z',
                            source_filename='one.png')
        first_case = [{'place': 15, 'nickname': 'Стальной_Алекс', 'kills': 6,
                       'deaths': 13, 'assists': 8, 'score': 3520}]
        first_saved = player_store.save_scan_result(unicode_db, first_case, **unicode_args)
        assert first_saved.created
        exact_case_retry = [dict(first_case[0], nickname='СТАЛЬНОЙ_АЛЕКС')]
        duplicate = player_store.save_scan_result(
            unicode_db, exact_case_retry,
            **{**unicode_args, 'source_filename': 'retry.png',
               'scanned_at': '2026-08-18T10:01:00Z'},
        )
        assert not duplicate.created and duplicate.scan_id == first_saved.scan_id
        second_case = [dict(first_case[0], nickname='стальной_алекс', kills=10,
                            deaths=7, assists=4, score=3521)]
        second_saved = player_store.save_scan_result(
            unicode_db, second_case,
            **{**unicode_args, 'source_filename': 'two.png',
               'scanned_at': '2026-08-18T11:00:00Z'},
        )
        assert second_saved.created and second_saved.scan_id != first_saved.scan_id
        unicode_stats = player_store.collect_stats_samples(unicode_db)
        unicode_metrics = unicode_stats['samples'][unicode_pid]
        assert unicode_metrics['kills'] == [16.0, 2.0]
        assert unicode_metrics['deaths'] == [20.0, 2.0]
        assert unicode_metrics['assists'] == [12.0, 2.0]
        assert unicode_metrics['score'] == [7041.0, 2.0]
        # 3520.5 must use half-up rather than Python's bankers rounding.
        assert bot._fmt_stats_number(bot._stats_average(unicode_metrics['score'])) == '3521'
        unicode_row = bot._fmt_stats_row('Стальной_алекс_очень_длинный', unicode_metrics)
        assert 'Стальной_алек…' in unicode_row and '  3521 ' in unicode_row
        assert bot._STATS_TABLE_HEADER.split() == ['Ник', 'У', 'С', 'П', 'СЧЁТ', 'ГРЕНЫ', 'ВРЕМЯ', 'N', 'ЭФФ']
        assert len(bot._STATS_TABLE_RULE) == len(bot._STATS_TABLE_HEADER)
        # A dated /stats view is DB-only: current JSON snapshot must not leak into it.
        dated_collected = player_store.collect_stats_samples(
            unicode_db, date_from='2026-08-19', date_to='2026-08-19', guild_id=7,
        )
        dated_nick = '\u0421\u0442\u0430\u043b\u044c\u043d\u043e\u0439_test'
        dated_data = {'players': {'777': {'game_nick': dated_nick, 'squad': 1}},
                      'session_date': '2026-08-18',
                      'grenade_history': {dated_nick: {'20:00': 1, '20:25': 9}},
                      'voice_speak_seconds': {'777': 123.0}}
        dated_text = '\n'.join(embed.description or '' for embed in bot.build_stats_embeds(dated_data, dated_collected))
        assert dated_nick not in dated_text  # no current roster is fabricated for a historical day
        assert not dated_collected['historical_roster']

        # EFF: displayed rounded averages feed KD/min-max; missing values never become zero.
        def eff_metrics(k, d, a, score, gren, seconds):
            keys = ('kills', 'deaths', 'assists', 'score', 'grenades', 'voice_seconds')
            return {key: [float(value), 1.0] for key, value in zip(keys, (k, d, a, score, gren, seconds)) if value is not None}

        eff_rows = [
            (1, eff_metrics(10, 2, 5, 100, 5, 60)),
            (2, eff_metrics(20, 2, 10, 200, 10, 120)),
            (3, eff_metrics(15, 0, 7, 150, 7, 90)),  # D=0 => KD=K
            (4, eff_metrics(10, 2, None, 100, 5, 60)),
            (5, eff_metrics(20, 2, 10, 200, 10, 9999)),  # squad 5 time excluded
            (6, eff_metrics(10, 2, 5, None, 5, 60)),     # NULL score excluded
            (99, eff_metrics(999, 1, 999, 999, 999, 999)),
            (None, eff_metrics(999, 1, 999, 999, 999, 999)),
        ]
        efficiencies = bot.calc_stats_efficiencies(eff_rows)
        assert efficiencies[0] == 0.0 and efficiencies[1] == 78.1
        assert efficiencies[2] == 67.5 and efficiencies[3] is None
        assert efficiencies[4] == 78.1 and efficiencies[5] is None
        assert efficiencies[6:] == [None, None]
        # Identical min=max produces neutral 50 for every available metric.
        tied = bot.calc_stats_efficiencies([(1, eff_metrics(5, 0, 2, 100, 10, None))])
        assert tied == [50.0]
        # Incomplete players cannot get a rating by redistributing missing weights.
        partial = bot.calc_stats_efficiencies([
            (1, eff_metrics(1, 1, None, None, None, None)),
            (2, eff_metrics(3, 1, 8, None, None, None)),
        ])
        assert partial == [None, None]
        # Half-up EFF rounding is explicit at an exact x.x5 boundary.
        original_weights = bot._STATS_EFF_WEIGHTS
        try:
            bot._STATS_EFF_WEIGHTS = {'KD': 0.3335, 'assists': 0.6665}
            rounded = bot.calc_stats_efficiencies([
                (1, eff_metrics(1, 1, 0, None, None, None)),
                (2, eff_metrics(2, 1, 1, None, None, None)),
                (3, eff_metrics(2, 1, 0, None, None, None)),
            ])
            assert rounded[2] == 33.4
        finally:
            bot._STATS_EFF_WEIGHTS = original_weights
        # Per-squad ordering: raw EFF desc, stable slot tie-break, then missing EFF.
        sort_data = {'players': {
            'low': {'game_nick': 'Low', 'squad': 1, 'slot': 3},
            'tie_b': {'game_nick': 'TieB', 'squad': 1, 'slot': 2},
            'missing': {'game_nick': 'Missing', 'squad': 1, 'slot': 4},
            'tie_a': {'game_nick': 'TieA', 'squad': 1, 'slot': 1},
            'other': {'game_nick': 'OtherSquad', 'squad': 2, 'slot': 1},
            'champ': {'game_nick': 'Champion', 'squad': 99, 'slot': 1},
            'free': {'game_nick': 'FreeAgent', 'squad': None, 'slot': None},
        }}
        sort_collected = {
            'bindings': {did: i for i, did in enumerate(sort_data['players'], start=1)},
            'samples': {
                1: eff_metrics(1, 1, 2, 100, 10, None),
                2: eff_metrics(3, 1, 2, 100, 10, None),
                4: eff_metrics(3, 1, 2, 100, 10, None),
                5: eff_metrics(2, 1, 2, 100, 10, None),
            },
            'completed_dates': set(),
        }
        sorted_text = '\n'.join(embed.description or '' for embed in bot.build_stats_embeds(sort_data, sort_collected))
        ordered_names = ['TieA', 'TieB', 'Low', 'Missing', 'OtherSquad', 'Champion', 'FreeAgent']
        assert [sorted_text.index(name) for name in ordered_names] == sorted(
            sorted_text.index(name) for name in ordered_names
        )
        assert sorted_text.index('Отряд 1:') < sorted_text.index('Отряд 2:') < sorted_text.index('Чемпионы:') < sorted_text.index('Без отряда:')
        assert next(line for line in sorted_text.splitlines() if line.startswith('Missing')).split()[-1] == '—'

        # Discord-safe pagination and excluded squads render an em dash in the last column.
        excluded_row = bot._fmt_stats_row('Champion', eff_rows[6][1], None)
        assert excluded_row.split()[-1] == '—'
        many_data = {'players': {
            str(i): {'game_nick': f'long_player_name_{i}', 'squad': (i % 6) + 1}
            for i in range(180)
        }}
        many_collected = {'bindings': {}, 'samples': {}, 'completed_dates': set()}
        pages = bot.build_stats_embeds(many_data, many_collected)
        assert len(pages) > 1 and all(len(embed.description or '') <= 4096 for embed in pages)
        assert all('```' in (embed.description or '') for embed in pages)
        # A grenade/voice-only player has no scan score and renders an em dash.
        no_scan_row = bot._fmt_stats_row(
            'OnlyVoice', {'grenades': [6.0, 1.0], 'voice_seconds': [150.0, 1.0]},
        )
        assert no_scan_row.split()[4] == '—'
        null_score = [dict(first_case[0], nickname='Стальной_Алекс', score=None)]
        null_saved = player_store.save_scan_result(
            unicode_db, null_score,
            **{**unicode_args, 'source_filename': 'null.png',
               'scanned_at': '2026-08-18T12:00:00Z'},
        )
        assert null_saved.created
        after_null = player_store.collect_stats_samples(unicode_db)['samples'][unicode_pid]
        assert after_null['score'] == [7041.0, 2.0]
        with closing(player_store.connect(unicode_db)) as con:
            assert con.execute('SELECT COUNT(*) FROM scans').fetchone()[0] == 3
            assert con.execute('SELECT COUNT(*) FROM scan_players').fetchone()[0] == 3
            assert con.execute('SELECT COUNT(DISTINCT player_id) FROM scan_players').fetchone()[0] == 1
            assert con.execute('SELECT canonical_nick FROM players WHERE id=?',
                               (unicode_pid,)).fetchone()[0] == 'Стальной_алекс'

        # Legacy duplicate players differing only by Cyrillic case are merged safely.
        conflict_db = root / 'legacy-case-conflict.sqlite3'
        player_store.ensure_schema(conflict_db)
        with closing(player_store.connect(conflict_db)) as con:
            upper = con.execute("INSERT INTO players(canonical_nick) VALUES('Стальной_Алекс')").lastrowid
            lower = con.execute("INSERT INTO players(canonical_nick) VALUES('Стальной_алекс')").lastrowid
            con.execute("INSERT INTO discord_bindings(discord_id,player_id) VALUES('a',?)", (upper,))
            con.execute("INSERT INTO discord_bindings(discord_id,player_id) VALUES('b',?)", (lower,))
        player_store.ensure_schema(conflict_db)
        with closing(player_store.connect(conflict_db)) as con:
            assert con.execute('SELECT COUNT(*) FROM players').fetchone()[0] == 1
            assert con.execute('SELECT COUNT(DISTINCT player_id) FROM discord_bindings').fetchone()[0] == 1

        # Conservative fuzzy OCR matching, guild scope, exclusions and ambiguity.
        fuzzy_db = root / 'fuzzy.sqlite3'
        guild = 42
        invvi = player_store.upsert_binding(fuzzy_db, '1', 'INK_invvi', guild_id=guild)
        long_pid = player_store.upsert_binding(fuzzy_db, '2', 'LongNickname', guild_id=guild)
        close_a = player_store.upsert_binding(fuzzy_db, '3', 'realplayerA', guild_id=guild)
        close_b = player_store.upsert_binding(fuzzy_db, '4', 'realplayerB', guild_id=guild)
        player_store.upsert_binding(fuzzy_db, '5', 'abcd', guild_id=guild)
        with closing(player_store.connect(fuzzy_db)) as con:
            assert player_store.match_player_in_connection(con, 'INK_inwi', guild).player_id == invvi
            assert player_store.match_player_in_connection(con, 'ink_INVVI', guild).method == 'exact'
            assert player_store.damerau_levenshtein('LongNicknamf', 'LongNickname') == 1
            assert player_store.damerau_levenshtein('LongNicknaame', 'LongNickname') == 1
            assert player_store.damerau_levenshtein('LongXicknamY', 'LongNickname') == 2
            assert player_store.damerau_levenshtein('LongNicknmae', 'LongNickname') == 1
            assert player_store.match_player_in_connection(con, 'LongXicknamY', guild).player_id == long_pid
            assert player_store.match_player_in_connection(con, 'abce', guild).method == 'too-short'
            assert player_store.match_player_in_connection(con, 'realplayerX', guild).method == 'ambiguous'
            assert player_store.match_player_in_connection(con, 'realplayerA', guild).player_id == close_a
            assert close_a != close_b
            con.execute("INSERT INTO fuzzy_exclusions(normalized_nick,guild_id) VALUES(?,?)",
                        (player_store._norm('INK_inwi'), guild))
            # Explicit exact aliases still outrank an exclusion; non-exact fuzzy does not.
            assert player_store.match_player_in_connection(con, 'INK_inwi', guild).method == 'excluded'
        player_store.add_alias(fuzzy_db, 'INK_invvi', 'INK_inwi')
        with closing(player_store.connect(fuzzy_db)) as con:
            assert player_store.match_player_in_connection(con, 'INK_inwi', guild).method == 'exact'
            assert con.execute('SELECT COUNT(*) FROM players').fetchone()[0] == 5

        fuzzy_rows = [{'place': 1, 'nickname': 'LongXicknamY', 'kills': 1,
                       'deaths': 2, 'assists': 3, 'score': 4}]
        fuzzy_args = dict(match_date='2026-08-16', map_name='Хвойник', guild_id=guild,
                          channel_id=1, user_id=1, scanned_at='now',
                          source_filename='fuzzy.png')
        fuzzy_saved = player_store.save_scan_result(fuzzy_db, fuzzy_rows, **fuzzy_args)
        fuzzy_retry = player_store.save_scan_result(
            fuzzy_db, fuzzy_rows, **{**fuzzy_args, 'source_filename': 'retry.png'}
        )
        assert fuzzy_saved.created and not fuzzy_retry.created
        with closing(player_store.connect(fuzzy_db)) as con:
            row = con.execute("SELECT nick,player_id FROM scan_players WHERE scan_id=?",
                              (fuzzy_saved.scan_id,)).fetchone()
            assert row['nick'] == 'LongXicknamY' and row['player_id'] == long_pid
            assert con.execute('SELECT COUNT(*) FROM scan_players').fetchone()[0] == 1
            assert con.execute("SELECT COUNT(*) FROM player_aliases WHERE alias='LongXicknamY'").fetchone()[0] == 0

        # Same source replaces the snapshot value rather than adding it.
        player_store.sync_voice_snapshot(db, {'123': 20.75, '999': 1.0},
                                         'legacy-kv:2026-08-16', '2026-08-16')
        # A reset/new source remains a separate session.
        player_store.sync_voice_snapshot(db, {'123': 0.5},
                                         'legacy-kv:2026-08-17', '2026-08-17')
        with closing(player_store.connect(db)) as con:
            rows = con.execute("SELECT source_key,seconds,player_id FROM voice_stats ORDER BY source_key,discord_id").fetchall()
            assert len(rows) == 3
            assert sum(row[1] for row in rows if row[0] == 'legacy-kv:2026-08-16') == 21.75
            assert isinstance(rows[0][1], float)
            assert all(row[2] == binding for row in rows if row[0] == 'legacy-kv:2026-08-17')
        # Stats: one scoreboard row + one completed KV log; absent K/D/A stay absent.
        player_store.save_kv_daily_report(
            db,
            '2026-08-16',
            0,
            [{
                'player_id': binding,
                'raw_nick': 'iovecuit',
                'discord_username': 'user',
                'squad_label': 'Отряд 1',
                'stages': [1, 2, 3],
                'total_grenades': 6,
                'voice_seconds': 150,
            }],
            3,
        )
        with closing(player_store.connect(db)) as con:
            con.execute(
                "INSERT INTO scans(id,stage,map,user_id,scanned_at,source_filename) "
                "VALUES(2,1,'map',1,'now','stats.png')"
            )
            con.execute(
                "INSERT INTO scan_players(scan_id,place,nick,kills,deaths,assists,score,player_id) "
                "VALUES(2,1,'iovecuit',10,4,7,100,?)",
                (binding,),
            )
        stats = player_store.collect_stats_samples(db)
        metrics = stats['samples'][binding]
        # Includes both the pre-migration alias row (1/2/3) and the new row.
        assert metrics['kills'] == [11.0, 2.0]
        assert metrics['deaths'] == [6.0, 2.0]
        assert metrics['assists'] == [10.0, 2.0]
        assert metrics['score'] == [104.0, 2.0]
        assert metrics['grenades'] == [6.0, 1.0]
        assert metrics['voice_seconds'] == [150.0, 1.0]
        assert stats['completed_dates'] == {'2026-08-16'}

        try:
            player_store.upsert_binding(db, '456', 'other')
            player_store.add_alias(db, 'other', 'lovecult')
            raise AssertionError('conflicting alias accepted')
        except ValueError:
            pass

        # v10 full JSON cutover: normalized import, idempotency, soft-delete and round-trips.
        full_db = root / 'full-cutover.sqlite3'
        full_json = root / 'full-players.json'
        full_payload = {
            'players': {
                '100': {'game_nick': 'Alpha', 'discord_name': 'A', 'discord_username': 'alpha',
                        'squad': 1, 'slot': 1, 'came': True, 'in_voice': True},
                '200': {'game_nick': 'Beta', 'discord_name': 'B', 'discord_username': 'beta',
                        'squad': 2, 'slot': 1, 'came': False, 'in_voice': False},
            },
            'config': {'guild_id': 77, 'log_channel_id': 88,
                       'voice_channel_ids': [91, 92], 'access_role_ids': [501, 502]},
            'message_ids': {'online': 7001, 'grenades': 7002},
            'session_date': '2026-08-19', 'grenade_date': '2026-08-19',
            'kv_session_active': True, 'kv_finished': False,
            'last_grenade_step': '20:25', 'skipped_grenade_steps': ['20:00'],
            'grenade_history': {'Alpha': {'20:00': 10, '20:25': 12},
                                'Beta': {'20:00': 5}},
            'voice_speak_seconds': {'100': 15.5, '200': 0},
            'absent_dm_sent': ['200'], 'squad_names': {'1': 'One', '2': 'Two'},
            'kv_maps': {'date': '2026-08-19', 'maps': ['Хвойник', 'Бердовка']},
        }
        full_json.write_text(json.dumps(full_payload, ensure_ascii=False), encoding='utf-8')
        imported = player_store.migrate(full_db, full_json, backup=False)
        repeated = player_store.migrate(full_db, full_json, backup=False)
        assert not imported['skipped'] and repeated['skipped']
        assert imported['counts'] == repeated['counts']
        assert set(imported['counts']) == set(player_store.STATUS_COUNT_TABLES)
        assert imported['counts']['player_aliases'] == 0
        assert player_store.get_guild_config(full_db, 77) == full_payload['config']
        roster = player_store.list_roster(full_db, 77)
        assert len(roster) == 2
        alpha_id = next(r['player_id'] for r in roster if r['canonical_nick'] == 'Alpha')
        beta_id = next(r['player_id'] for r in roster if r['canonical_nick'] == 'Beta')
        assert player_store.deactivate_roster_member(full_db, 77, beta_id)
        assert len(player_store.list_roster(full_db, 77)) == 1
        assert len(player_store.list_roster(full_db, 77, include_inactive=True)) == 2
        state = player_store.load_runtime_session(full_db, 77, '2026-08-19')
        assert state and state['attendance'][str(alpha_id)] == {'came': True, 'in_voice': True}
        assert state['maps'] == ['Хвойник', 'Бердовка']
        assert state['grenades'][str(alpha_id)] == {'20:00': 10, '20:25': 12}
        assert state['voice']['100']['seconds'] == 15.5
        updated = dict(state, finished=True, voice={
            '100': {'player_id': alpha_id, 'seconds': 20.25, 'state': {'gate': 'open'}}
        })
        player_store.save_runtime_session(full_db, 77, updated)
        round_trip = player_store.load_runtime_session(full_db, 77, '2026-08-19')
        assert round_trip['finished'] and round_trip['voice']['100']['state'] == {'gate': 'open'}
        assert round_trip['grenades'] == state['grenades']
        status = player_store.reconciliation_status(full_db, full_json)
        assert status['schema_version'] == player_store.SCHEMA_VERSION and status['integrity'] == 'ok'
        assert not status['foreign_key_violations'] and status['legacy_imported']
        assert set(status['counts']) == set(player_store.STATUS_COUNT_TABLES)
        assert status['counts']['player_aliases'] == imported['counts']['player_aliases']
        snapshot = player_store.export_snapshot(full_db, 77, '2026-08-19')
        assert set(snapshot['players']) == {'100'}  # inactive Beta is retained in DB, omitted from active snapshot
        with closing(player_store.connect(full_db)) as con:
            assert con.execute('SELECT COUNT(*) FROM legacy_imports').fetchone()[0] == 1
            assert con.execute('SELECT COUNT(*) FROM absent_dm_deliveries').fetchone()[0] == 1
            try:
                con.execute("INSERT INTO attendance(session_id,player_id) VALUES(999999,?)", (alpha_id,))
                raise AssertionError('foreign key violation accepted')
            except sqlite3.IntegrityError:
                pass
    after = working_db.read_bytes() if working_db.exists() else None
    assert before == after, 'working database was modified by tests'

    # Stage 2 runtime snapshot is SQLite-only and removal preserves inactive history.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        stage2_db = root / 'stage2.sqlite3'
        stage2_json = root / 'players.json'
        stage2_json.write_text(json.dumps({
            'config': {'guild_id': 88},
            'players': {'800': {'game_nick': 'RuntimePlayer'}},
        }), encoding='utf-8')
        player_store.ensure_legacy_cutover(stage2_db, stage2_json)
        json_before = stage2_json.read_bytes()
        snapshot = player_store.load_bot_snapshot(stage2_db, 88)
        snapshot['config'].update({'guild_id': 88, 'log_channel_id': 10,
                                   'voice_channel_ids': [11, 12], 'access_role_ids': [13]})
        snapshot['players']['800'].update({'squad': 2, 'slot': 1, 'came': True, 'in_voice': True})
        snapshot.update({'session_date': '2026-08-20', 'grenade_date': '2026-08-20',
                         'kv_session_active': True, 'message_ids': {'online': 21, 'grenades': 22},
                         'absent_dm_sent': ['800'], 'squad_names': {'2': 'Alpha'},
                         'kv_maps': {'date': '2026-08-20', 'maps': ['Хвойник']}})
        player_store.save_bot_snapshot(stage2_db, snapshot, 88)
        restored = player_store.load_bot_snapshot(stage2_db, 88)
        assert restored['config']['log_channel_id'] == 10
        assert restored['players']['800']['came'] and restored['players']['800']['in_voice']
        assert restored['kv_maps']['maps'] == ['Хвойник'] and restored['absent_dm_sent'] == ['800']
        assert stage2_json.read_bytes() == json_before

        # Stage 3: grenade/voice checkpoints survive process-style reloads,
        # repeated saves replace rather than add, and finalization atomically
        # publishes historical stats while clearing resumable live rows.
        snapshot.update({
            'grenade_history': {'RuntimePlayer': {'20:00': 10, '20:25': 13}},
            'voice_speak_seconds': {'800': 15.5},
            'last_grenade_step': '20:25', 'skipped_grenade_steps': ['20:10'],
        })
        player_store.save_bot_snapshot(stage2_db, snapshot, 88)
        crashed_reload = player_store.load_bot_snapshot(stage2_db, 88)
        assert crashed_reload['grenade_history'] == snapshot['grenade_history']
        assert crashed_reload['voice_speak_seconds'] == {'800': 15.5}
        assert crashed_reload['last_grenade_step'] == '20:25'
        crashed_reload['voice_speak_seconds']['800'] = 21.25
        crashed_reload['grenade_history']['RuntimePlayer']['20:25'] = 14
        player_store.save_bot_snapshot(stage2_db, crashed_reload, 88)
        player_store.save_bot_snapshot(stage2_db, crashed_reload, 88)
        with closing(player_store.connect(stage2_db)) as con:
            sid = con.execute("SELECT id FROM runtime_sessions WHERE guild_id=88").fetchone()[0]
            assert con.execute("SELECT COUNT(*) FROM live_grenade_state WHERE session_id=?", (sid,)).fetchone()[0] == 2
            assert con.execute("SELECT seconds FROM voice_checkpoints WHERE session_id=?", (sid,)).fetchone()[0] == 21.25

        crashed_reload['kv_finished'] = True
        crashed_reload['kv_session_active'] = False
        player_store.save_bot_snapshot(stage2_db, crashed_reload, 88)
        finalized = player_store.load_bot_snapshot(stage2_db, 88)
        assert finalized['kv_finished'] and not finalized['kv_session_active']
        assert finalized['grenade_history']['RuntimePlayer']['20:25'] == 14
        assert finalized['voice_speak_seconds'] == {'800': 21.25}
        player_store.save_bot_snapshot(stage2_db, finalized, 88)
        player_store.finalize_runtime_session(stage2_db, 88, '2026-08-20')
        with closing(player_store.connect(stage2_db)) as con:
            assert con.execute('SELECT COUNT(*) FROM live_grenade_state').fetchone()[0] == 0
            assert con.execute('SELECT COUNT(*) FROM voice_checkpoints').fetchone()[0] == 0
            assert con.execute("SELECT COUNT(*) FROM grenade_session_stats WHERE source_key='runtime-kv:88:2026-08-20'").fetchone()[0] == 2
            assert con.execute("SELECT COUNT(*) FROM voice_stats WHERE source_key='runtime-kv:88:2026-08-20'").fetchone()[0] == 1
            assert con.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            assert not con.execute('PRAGMA foreign_key_check').fetchall()

        snapshot['players'].pop('800')
        player_store.save_bot_snapshot(stage2_db, snapshot, 88)
        assert player_store.list_roster(stage2_db, 88) == []
        assert player_store.list_roster(stage2_db, 88, include_inactive=True)[0]['active'] == 0
        stage2_json.write_text('{broken after marker', encoding='utf-8')
        cutover_status = player_store.ensure_legacy_cutover(stage2_db, stage2_json)
        assert cutover_status['skipped']
        assert set(cutover_status['counts']) == set(player_store.STATUS_COUNT_TABLES)
        assert player_store.load_bot_snapshot(stage2_db, 88)['players'] == {}

    # Regression: run the real startup initialization through main(), but stop at
    # a stubbed bot.run so no token is exposed and no Discord connection is made.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        startup_db = root / 'startup.sqlite3'
        startup_json = root / 'players.json'
        startup_json.write_text('{}', encoding='utf-8')
        original_values = (bot.PLAYER_DB_PATH, bot.LEGACY_JSON_PATH, bot.DISCORD_TOKEN,
                           bot.CLIENT_SECRET, bot.bot.run, bot.bot.close)
        run_calls = []
        try:
            bot.PLAYER_DB_PATH = startup_db
            bot.LEGACY_JSON_PATH = startup_json
            bot.DISCORD_TOKEN = 'test-token-not-a-real-secret'
            bot.CLIENT_SECRET = 'test-client-secret'
            bot.bot.run = lambda token, **kwargs: run_calls.append((token, kwargs))
            from unittest.mock import patch
            with patch.object(bot, 'start_admin_panel'):
                bot.main()
        finally:
            (bot.PLAYER_DB_PATH, bot.LEGACY_JSON_PATH, bot.DISCORD_TOKEN,
             bot.CLIENT_SECRET, bot.bot.run, bot.bot.close) = original_values
        assert run_calls == [('test-token-not-a-real-secret', {'log_handler': None})]
        startup_status = player_store.ensure_legacy_cutover(startup_db, startup_json)
        assert startup_status['counts']['player_aliases'] == 0
        assert set(startup_status['counts']) == set(player_store.STATUS_COUNT_TABLES)

    import ast
    bot_tree = ast.parse((ROOT / 'bot.py').read_text(encoding='utf-8'))
    forbidden = [node for node in ast.walk(bot_tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                 and node.func.value.id == 'json' and node.func.attr in {'load', 'dump'}]
    assert not forbidden
    print('OK: idempotency, binding, alias, shared scan/grenade player_id, conflict, working DB isolation')


if __name__ == '__main__':
    main()
