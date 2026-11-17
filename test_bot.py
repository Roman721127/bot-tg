import json
import sqlite3
import pytest
from bot import (
    init_db,
    save_user_group,
    get_user_data,
    save_user_electives,
    filter_schedule_for_user,
    get_group_subjects,
    load_schedule_file
)

@pytest.fixture(autouse=True)
def setup_db():
    import os
    if os.path.exists("bot_database.db"):
        os.remove("bot_database.db")
    init_db()
    yield
    if os.path.exists("bot_database.db"):
        os.remove("bot_database.db")

def test_user_db_operations():
    user_id = 123456
    group, electives = get_user_data(user_id)
    assert group is None
    assert electives == []

    save_user_group(user_id, "IPZ-42")
    group, electives = get_user_data(user_id)
    assert group == "IPZ-42"

    save_user_electives(user_id, ["Math"])
    group, electives = get_user_data(user_id)
    assert electives == ["Math"]

def test_filter_schedule_for_user():
    schedule = "🔹 Пара 1: Математика — Іван\n🔹 Пара 2: Фізика — Петро"
    excluded = ["Фізика"]
    filtered = filter_schedule_for_user(schedule, excluded)
    assert "Математика" in filtered
    assert "Фізика" not in filtered

def test_load_and_get_subjects(tmp_path, monkeypatch):
    test_data = {
        "IPZ-42": [
            {
                "date": "19.11.2026",
                "day": "Середа",
                "pairs": [
                    {"num": 1, "time": "08:30", "subject": "Алгоритми", "teacher": "Сидорук", "location": "101", "link": ""}
                ]
            }
        ]
    }
    d = tmp_path / "schedule.json"
    d.write_text(json.dumps(test_data), encoding="utf-8")
    
    monkeypatch.chdir(tmp_path)
    data = load_schedule_file()
    assert "IPZ-42" in data

    subjects = get_group_subjects("IPZ-42")
    assert "Алгоритми" in subjects