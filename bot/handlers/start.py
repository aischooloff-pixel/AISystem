"""Точка входа: ``/start``, deep links, источник, маршрутизация (Блок 5).

Логика из ТЗ:

1. Распарсить start-параметр (источник из справочника).
2. Найти контакт по ``telegram_id``.
3. Существующий контакт: передан Юлии или на паузе → одно сообщение
   «Юлия уже знает...», касание, ВЫХОД без автоматики; иначе — обновить
   ``last_contact_date``/``touches_count``, касание, продолжить с текущего
   состояния FSM.
4. Новый контакт: источник из deep link, без deep link — кнопки выбора;
   создание записи, касание «Первое обращение в бот», приветствие
   с представлением AI-помощником, ожидание первого сообщения (FSM).

Сбой Airtable никогда не блокирует диалог: приветствие уходит в любом
случае, контакт досоздастся при следующем сообщении (upsert в Блоке 6).
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, User

from bot import texts
from bot.keyboards.user import SOURCE_CALLBACK_PREFIX, source_keyboard
from bot.services import airtable
from bot.states import Dialog
from bot.utils.logger import get_app_logger

logger = get_app_logger()

router = Router(name="start")

# Справочник source (модель данных, ТЗ Часть 3) — допустимые значения
# deep link. Шире списка из Блока 5: qr/speech/partner и др. нужны для
# QR-кодов и выступлений («Карта клиентского пути», п. 2).
VALID_SOURCES = frozenset(
    {
        "telegram_channel",
        "telegram_comment",
        "telegram_dm",
        "vk",
        "facebook",
        "site",
        "referral",
        "professional_group",
        "mastermind",
        "speech",
        "interview",
        "media",
        "qr",
        "partner",
        "other",
    }
)


def _profile_data(user: User) -> dict:
    """Имя и username из Telegram-профиля (обновляются при каждом касании)."""
    data: dict = {"name": user.full_name}
    if user.username:
        data["username"] = f"@{user.username}"
    return data


async def _reply_safe(message: Message, text: str, **kwargs) -> None:
    """Отправка ответа, не роняющая обработчик (ТЗ, Часть 7).

    ``message`` может оказаться InaccessibleMessage (кнопка старше 48 ч):
    у него нет ``from_user`` — обращаемся к атрибутам только через getattr.
    """
    try:
        await message.answer(text, **kwargs)
    except Exception:
        user = getattr(message, "from_user", None)
        logger.exception(
            "Не удалось отправить сообщение telegram_id=%s", getattr(user, "id", "unknown")
        )


async def _remove_keyboard_safe(callback: CallbackQuery) -> None:
    """Убирает inline-клавиатуру под сообщением callback'а, если возможно."""
    edit = getattr(callback.message, "edit_reply_markup", None)
    if edit is None:
        return  # InaccessibleMessage или сообщение недоступно — не критично
    try:
        await edit(reply_markup=None)
    except Exception:
        logger.exception(
            "Не удалось убрать клавиатуру источника (telegram_id=%s)", callback.from_user.id
        )


async def _register_and_greet(
    message: Message, state: FSMContext, user: User, source: str, source_detail: str | None
) -> None:
    """Шаги 4.3–4.6: создание контакта, касание, приветствие, FSM."""
    data = {
        **_profile_data(user),
        "source": source,
        "first_action": "/start",
    }
    if source_detail:
        data["source_detail"] = source_detail
    record = await airtable.upsert_contact(user.id, data)
    if record is None:
        # Airtable недоступен: клиента не теряем — диалог продолжается,
        # запись досоздаст upsert при следующем сообщении (Блок 6)
        logger.error("Контакт telegram_id=%s не сохранён (Airtable недоступен)", user.id)
    await airtable.add_touch(
        user.id, "dm_start", "Первое обращение в бот", source=source, raw_content=source_detail
    )
    await state.set_state(Dialog.waiting_first_message)
    await _reply_safe(message, texts.GREETING)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    """``/start`` с deep link и без: дедупликация, источник, маршрутизация."""
    user = message.from_user
    if user is None:
        return
    # Deep link: /start <параметр> (aiogram передаёт текст целиком)
    parts = (message.text or "").split(maxsplit=1)
    param = parts[1].strip() if len(parts) > 1 else ""
    logger.info("Входящее /start от telegram_id=%s, параметр=%r", user.id, param)

    ok, existing = await airtable.find_contact_checked(user.id)
    if not ok:
        # Airtable недоступен: нельзя отличить нового клиента от переданного
        # Юлии — не запускаем автоматику вслепую (шаг 3.1 важнее приветствия),
        # человек попробует через минуту
        await _reply_safe(message, texts.TECH_ERROR)
        return

    if existing is not None:
        fields = existing.get("fields", {})
        # Шаг 3.1: передан Юлии, пауза или уже клиент — автоматика не запускается
        from bot.utils.helpers import automation_stopped

        if automation_stopped(fields):
            await airtable.add_touch(user.id, "dm_start", "Повторный /start (клиент у Юлии)")
            await _reply_safe(message, texts.ALREADY_WITH_YULIA)
            return
        # Шаг 3.2: обновить last_contact_date и touches_count, продолжить FSM
        await airtable.upsert_contact(user.id, _profile_data(user))
        await airtable.add_touch(user.id, "dm_start", "Повторный /start")
        if await state.get_state() is None:
            await state.set_state(Dialog.waiting_first_message)
            await _reply_safe(message, texts.GREETING)
        else:
            # Живой диалог не сбрасываем — продолжаем с текущего состояния
            await _reply_safe(message, texts.CONTINUE_DIALOG)
        return

    # Новый контакт (или Airtable не ответил — upsert внутри перепроверит)
    if param in VALID_SOURCES:
        await _register_and_greet(message, state, user, param, f"deep link: {param}")
        return
    # Шаг 4.2: источник не определён — кнопки выбора
    detail = f"нераспознанный start-параметр: {param}" if param else None
    await state.set_state(Dialog.choosing_source)
    if detail:
        await state.update_data(source_detail=detail)
    await _reply_safe(message, texts.SOURCE_QUESTION, reply_markup=source_keyboard())


@router.callback_query(F.data.startswith(SOURCE_CALLBACK_PREFIX))
async def source_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    """Шаг 4.2 → 4.3: клиент выбрал источник кнопкой.

    Хендлер намеренно без фильтра состояния: клавиатура источника переживает
    рестарт бота (MemoryStorage теряет FSM), и нажатие «устаревшей» кнопки
    не должно оставлять клиента с вечным спиннером — маршрутизируем по
    фактическому состоянию контакта в CRM.
    """
    user = callback.from_user
    try:
        await callback.answer()
    except Exception:
        logger.exception("callback.answer() не прошёл (telegram_id=%s)", user.id)

    source = (callback.data or "")[len(SOURCE_CALLBACK_PREFIX) :]
    if source not in VALID_SOURCES:
        logger.warning("Неизвестный источник в callback: %r (telegram_id=%s)", source, user.id)
        source = "other"

    ok, existing = await airtable.find_contact_checked(user.id)
    if not ok:
        await _reply_message_of(callback, texts.TECH_ERROR)
        return

    await _remove_keyboard_safe(callback)

    if existing is not None:
        fields = existing.get("fields", {})
        from bot.utils.helpers import automation_stopped

        if automation_stopped(fields):
            await _reply_message_of(callback, texts.ALREADY_WITH_YULIA)
            return
        # Контакт уже зарегистрирован (повторное нажатие / кнопка после
        # рестарта): источник первого касания не переопределяем
        await airtable.upsert_contact(user.id, _profile_data(user))
        if await state.get_state() is None:
            await state.set_state(Dialog.waiting_first_message)
            await _reply_message_of(callback, texts.GREETING)
        else:
            await _reply_message_of(callback, texts.CONTINUE_DIALOG)
        return

    state_data = await state.get_data()
    extra = state_data.get("source_detail")
    detail = "кнопка: Facebook / VK" if source == "facebook" else None
    if extra:
        detail = f"{detail}; {extra}" if detail else extra
    if callback.message is not None:
        await _register_and_greet(callback.message, state, user, source, detail)
    else:
        logger.error("callback без message: приветствие не отправлено (id=%s)", user.id)


async def _reply_message_of(callback: CallbackQuery, text: str) -> None:
    """Ответ в чат callback'а, если сообщение доступно."""
    if callback.message is not None:
        await _reply_safe(callback.message, text)


@router.message(Dialog.choosing_source)
async def message_instead_of_source(message: Message, state: FSMContext) -> None:
    """Клиент проигнорировал кнопки и прислал сообщение: не блокируем диалог.

    Ловит ЛЮБОЙ тип сообщения (текст, голос, фото, стикер — живой клиент
    не должен получать тишину). Источник остаётся неопределённым
    (``telegram_dm`` — человек пришёл в ЛС), содержимое сохраняется
    в касании, дальше — обычный путь приветствия.
    """
    user = message.from_user
    if user is None:
        return
    state_data = await state.get_data()
    detail = state_data.get("source_detail") or "прислал сообщение вместо выбора источника"
    data = {
        **_profile_data(user),
        "source": "telegram_dm",
        "source_detail": detail,
        "first_action": "/start",
    }
    if await airtable.upsert_contact(user.id, data) is None:
        logger.error("Контакт telegram_id=%s не сохранён (Airtable недоступен)", user.id)
    raw = message.text or message.caption or f"<{message.content_type}>"
    await airtable.add_touch(
        user.id,
        "dm_start",
        "Первое обращение в бот (источник не выбран)",
        source="telegram_dm",
        raw_content=raw,
    )
    await state.set_state(Dialog.waiting_first_message)
    await _reply_safe(message, texts.GREETING)
