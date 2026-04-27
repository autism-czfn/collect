"""
Shared time-of-day helpers.

Kept in a dedicated module to avoid circular imports between
routes/logs.py, routes/transcribe_and_log.py, and routes/abstractions.py.
"""
from __future__ import annotations


def meal_type_from_hour(hour: int) -> str:
    """Map a local clock hour (0–23) to a meal-type label.

    Uses client_local_hour (new Date().getHours()) — never server UTC time.
    """
    if 5 <= hour <= 10:
        return "breakfast"
    if 11 <= hour <= 13:
        return "lunch"
    if 14 <= hour <= 17:
        return "snack"
    if 18 <= hour <= 20:
        return "dinner"
    return "late_night"


def time_label_from_hour(hour: int) -> str:
    """Map a local clock hour (0–23) to a time-of-day label.

    Matches the _time_of_day() boundaries already in routes/logs.py.
    """
    if 6 <= hour <= 11:
        return "morning"
    if 12 <= hour <= 16:
        return "afternoon"
    if 17 <= hour <= 20:
        return "evening"
    return "night"
