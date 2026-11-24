"""One persistent, editable bot post per chat, shared by commands and outbox."""

from __future__ import annotations

import asyncio
import hashlib
import json

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest

from database import Database
from telegram_ui import navigation_keyboard


class MessagePanel:
    def __init__(self, db: Database, bot: Bot):
        self.db, self.bot = db, bot
        self._locks: dict[int, asyncio.Lock] = {}

    async def send_message(
        self, chat_id: int, text: str, *, force: bool = False, **kwargs: object,
    ) -> object:
        lock = self._locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            markup = kwargs.pop("reply_markup", None) or navigation_keyboard()
            kwargs.setdefault("parse_mode", "HTML")
            kwargs.setdefault("request_timeout", 20)
            payload = json.dumps(
                {"text": text, "markup": markup.model_dump(mode="json"),
                 "parse_mode": kwargs["parse_mode"]},
                ensure_ascii=False, sort_keys=True,
            )
            digest = hashlib.sha256(payload.encode()).hexdigest()
            key = f"telegram_panel:{self.bot.id}:{chat_id}"
            saved = self.db.get_meta(key)
            state = json.loads(saved) if saved else None
            if state and state["digest"] == digest and not force:
                return state["message_id"]  # No HTTP call for unchanged content.
            if state:
                try:
                    result = await self.bot.edit_message_text(
                        chat_id=chat_id, message_id=state["message_id"],
                        text=text, reply_markup=markup, **kwargs,
                    )
                except TelegramBadRequest as exc:
                    error = exc.message.lower()
                    if "message is not modified" in error:
                        result = state["message_id"]
                    elif "message to edit not found" in error or "message can't be edited" in error:
                        state = None  # A confirmed missing/uneditable post may be replaced.
                    else:
                        raise
                if state:
                    self.db.set_meta(key, json.dumps({**state, "digest": digest}))
                    return result
            # No fallback on timeouts/429/5xx: the edit may already have succeeded.
            result = await self.bot.send_message(
                chat_id=chat_id, text=text, reply_markup=markup, **kwargs,
            )
            self.db.set_meta(key, json.dumps({"message_id": result.message_id, "digest": digest}))
            return result
