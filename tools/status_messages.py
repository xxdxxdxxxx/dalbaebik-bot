"""Safe Discord status-message upserts without duplicate creation."""
from __future__ import annotations

import asyncio
import time


TITLES = {
    "online": "📡  ЯВКА НА КВ",
    "grenades": "💣  ГРАНАТЫ",
}
HISTORY_LIMIT = 500


def install(runtime):
    """Install one serialized, self-healing status-message updater."""
    if getattr(runtime, "_status_upsert_lock_installed", False):
        return
    runtime._status_upsert_lock_installed = True
    lock = asyncio.Lock()
    verified: set[tuple[int, str]] = set()

    async def find_oldest(channel, key):
        """Return the oldest matching message authored by this bot."""
        expected_title = TITLES[key]
        async for candidate in channel.history(limit=HISTORY_LIMIT, oldest_first=True):
            if runtime.bot.user is not None and candidate.author.id != runtime.bot.user.id:
                continue
            if any(embed.title == expected_title for embed in candidate.embeds):
                return candidate
        return None

    async def edit_existing_or_create(channel, key, embed, message_id):
        verification_key = (int(channel.id), key)

        # Once after every process start/channel change, repair a reference that
        # already points at a newer duplicate by selecting the oldest message.
        if verification_key not in verified:
            canonical = await find_oldest(channel, key)
            if canonical is not None:
                await canonical.edit(embed=embed)
                verified.add(verification_key)
                runtime.log(
                    f"status {key} · выбран первый message_id={canonical.id}", "ok"
                )
                return int(canonical.id)

        if message_id:
            try:
                message_id = int(message_id)
            except (TypeError, ValueError):
                runtime.log(
                    f"status {key} · некорректный message_id={message_id!r}", "warn"
                )
            else:
                try:
                    message = await channel.fetch_message(message_id)
                    await message.edit(embed=embed)
                    verified.add(verification_key)
                    return message_id
                except runtime.discord.NotFound:
                    runtime.log(
                        f"status {key} · message {message_id} не найден; ищу старое",
                        "warn",
                    )
                except runtime.discord.Forbidden:
                    raise
                except runtime.discord.HTTPException as exc:
                    # A timeout, 429 or Discord 5xx does not mean the message was
                    # deleted. Propagate it; never create a duplicate after it.
                    runtime.log(
                        f"status {key} · edit HTTP {exc.status} для {message_id}: {exc}",
                        "warn",
                    )
                    raise

        canonical = await find_oldest(channel, key)
        if canonical is not None:
            await canonical.edit(embed=embed)
            verified.add(verification_key)
            return int(canonical.id)

        message = await channel.send(embed=embed)
        verified.add(verification_key)
        runtime.log(f"status {key} · создан message_id={message.id}", "ok")
        return int(message.id)

    async def perform_upsert(data, want):
        channel = await runtime.get_log_channel(data)
        if channel is None:
            return data, "Канал логов не задан"

        me = channel.guild.me if channel.guild else None
        if me is None and channel.guild:
            me = channel.guild.get_member(runtime.bot.user.id) if runtime.bot.user else None
        if me is not None:
            bad = runtime.check_channel_post_perms(channel, me)
            if bad:
                return data, (
                    f"Нет прав писать в {channel.mention}.\n"
                    f"Выдай боту (или его роли) в этом канале:\n{bad}"
                )

        mids = data.setdefault("message_ids", {"online": None, "grenades": None})
        try:
            if "online" in want:
                mids["online"] = await edit_existing_or_create(
                    channel, "online", runtime.format_online_embed(data), mids.get("online")
                )
                await asyncio.sleep(0.35)
            if "grenades" in want:
                mids["grenades"] = await edit_existing_or_create(
                    channel,
                    "grenades",
                    runtime.format_grenades_embed(data),
                    mids.get("grenades"),
                )
        except runtime.discord.Forbidden:
            return data, (
                f"Нет доступа к каналу {channel.mention} (Missing Access).\n"
                "Проверь права бота в канале."
            )
        except runtime.discord.HTTPException as exc:
            if exc.status != 429:
                runtime.log(f"discord HTTP {exc.status}", "warn")
            return data, f"Не удалось изменить сообщение: {exc}"

        runtime._last_embed_edit_mono = time.monotonic()
        data["message_ids"] = mids
        runtime.save_db(data)
        return data, None

    async def upsert_status_messages(data=None, *, force=False, parts=None):
        want = set(parts) if parts else {"online", "grenades"}
        async with lock:
            # Always render and persist the newest DB snapshot. Callers frequently
            # carry stale snapshots while voice/stage refreshes overlap.
            fresh = runtime.load_db()

            if not force:
                runtime._embed_pending_parts |= want
                wait = runtime._EMBED_MIN_INTERVAL - (
                    time.monotonic() - runtime._last_embed_edit_mono
                )
                if wait > 0:
                    if (
                        runtime._embed_flush_task is None
                        or runtime._embed_flush_task.done()
                    ):
                        async def delayed_flush():
                            await asyncio.sleep(wait + 0.05)
                            pending = set(runtime._embed_pending_parts) or {"online"}
                            runtime._embed_pending_parts.clear()
                            await upsert_status_messages(
                                None, force=True, parts=tuple(pending)
                            )

                        runtime._embed_flush_task = asyncio.create_task(delayed_flush())
                    return fresh, None

                want |= runtime._embed_pending_parts
                runtime._embed_pending_parts.clear()

            return await perform_upsert(fresh, want)

    runtime.upsert_status_messages = upsert_status_messages
