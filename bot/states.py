"""Состояния FSM диалога с клиентом.

Блок 5 использует вход в диалог (выбор источника, ожидание первого
содержательного сообщения); Блок 6 добавит состояния вопросов квалификации.
"""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class Dialog(StatesGroup):
    """Путь клиента от /start до квалификации (Блоки 5–6)."""

    # /start без deep link: ждём выбор источника кнопками (ТЗ, Блок 5, шаг 4.2)
    choosing_source = State()
    # Приветствие отправлено: ждём первое содержательное сообщение,
    # по нему определяется сценарий A/B/C (ТЗ, Блок 6)
    waiting_first_message = State()

    # Сценарий A «Готовность записаться»: ровно два вопроса → передача
    a_question_1 = State()
    a_question_2 = State()

    # Сценарий B «Описывает проблему»: q1 = первое сообщение, дальше q2–q5
    b_question_2 = State()
    b_question_3 = State()
    b_question_4 = State()
    b_question_5 = State()

    # Сценарий C «Информационный интерес»: отвечаем, квалификацию не начинаем
    c_info = State()

    # Квалификация завершена (warm/cold): свободный диалог, AI следит
    # за появлением запроса и триггерами передачи
    open_dialog = State()


class AdminFlow(StatesGroup):
    """Состояния Юлии (Блок 8: правка ответа на комментарий)."""

    waiting_comment_reply = State()
