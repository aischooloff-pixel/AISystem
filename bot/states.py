"""Состояния FSM диалога с клиентом.

Блок 5 использует вход в диалог (выбор источника, ожидание первого
содержательного сообщения); Блок 6 добавит состояния вопросов квалификации.
"""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class Dialog(StatesGroup):
    """Путь клиента от /start до квалификации."""

    # /start без deep link: ждём выбор источника кнопками (ТЗ, Блок 5, шаг 4.2)
    choosing_source = State()
    # Приветствие отправлено: ждём первое содержательное сообщение,
    # по нему Блок 6 определит сценарий A/B/C
    waiting_first_message = State()
