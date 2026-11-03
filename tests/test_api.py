import os, sys, datetime
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
os.chdir(os.path.join(os.path.dirname(__file__), "..", "app"))
from fastapi.testclient import TestClient
import main, pytest

D = datetime.date
def H(**k): return {"id": 1, "days": "0123456", "goal": 1, "created": D(2026, 10, 1), **k}

def test_streak_basic():
    h = H(); t = D(2026, 11, 3)
    hl = {D(2026, 11, 1) + datetime.timedelta(days=i): (1, False) for i in range(3)}      # 1, 2, 3 Nov
    s = main.habit_stats(h, hl, t); assert s["streak"] == 3 and s["best"] == 3 and s["total"] == 3
    del hl[D(2026, 11, 3)]                                                                # today not done yet: streak survives
    assert main.habit_stats(h, hl, t)["streak"] == 2
    del hl[D(2026, 11, 2)]                                                                # yesterday missed: broken
    assert main.habit_stats(h, hl, t)["streak"] == 0

def test_skip_and_schedule():
    t = D(2026, 11, 3)                                                                    # a Tuesday
    h = H(days="0")                                                                       # Mondays only
    hl = {D(2026, 10, 19): (1, False), D(2026, 10, 26): (1, False), D(2026, 11, 2): (1, False)}
    assert main.habit_stats(h, hl, t)["streak"] == 3                                      # other weekdays are ignored
    hl[D(2026, 10, 26)] = (0, True)                                                       # skipped Monday keeps the streak alive, not counted
    s = main.habit_stats(h, hl, t); assert s["streak"] == 2 and s["total"] == 2

def test_goal_and_summary():
    h = H(goal=3); t = D(2026, 11, 3)
    hl = {D(2026, 11, 2): (2, False), D(2026, 11, 3): (3, False)}
    assert main.habit_stats(h, hl, t)["streak"] == 1                                      # 2 of 3 yesterday is a miss
    d = {r[0]: r for r in main.day_summary([h], {1: hl}, t, 3)}
    assert d["2026-11-02"][1:] == [0, 1] and d["2026-11-03"][1:] == [1, 1]

@pytest.fixture(scope="module")
def c():
    with TestClient(main.app, base_url="https://testserver") as cl: yield cl

def test_flow(c):
    assert c.get("/health").json()["ok"]
    assert c.get("/api/state").json() == {"edit": False}
    assert c.post("/api/habit", json={"name": "x"}).status_code == 401
    assert c.post("/api/login", json={"password": "wrong"}).status_code == 403
    assert c.post("/api/login", json={"password": os.environ["EDIT_PASSWORD"]}).status_code == 200
    for bad in ({"name": ""}, {"name": "a", "days": ""}, {"name": "a", "days": "9"}, {"name": "a", "period": "x"}, {"name": "a", "goal": 0}, {"name": "a", "color": "red"}):
        assert c.post("/api/habit", json=bad).status_code == 400, bad
    i = c.post("/api/habit", json={"name": " Water  ", "emoji": "💧", "goal": 8, "unit": "glasses"}).json()["id"]
    j = c.post("/api/habit", json={"name": "Read", "days": "420"}).json()["id"]
    t = main.today(); y = (t - datetime.timedelta(days=1)).isoformat()
    assert c.post("/api/log", json={"habit_id": i, "day": (t + datetime.timedelta(days=1)).isoformat(), "count": 1}).status_code == 400
    assert c.post("/api/log", json={"habit_id": 9999, "day": t.isoformat(), "count": 1}).status_code == 404
    assert c.post("/api/log", json={"habit_id": i, "day": t.isoformat(), "count": 3}).json() == {"count": 3, "skipped": False}
    st = c.get("/api/state").json()
    w = [h for h in st["habits"] if h["id"] == i][0]
    assert w["name"] == "Water" and w["days"] == "0123456" and st["today"] == t.isoformat() and [h["days"] for h in st["habits"]][1] == "024"
    assert [l for l in st["logs"] if l[0] == i] == [[i, t.isoformat(), 3, False]]
    c.post("/api/log", json={"habit_id": i, "day": y, "skipped": True})
    assert c.get(f"/api/logs?start={y}&end={t.isoformat()}&habit_id={i}").json()["logs"].__len__() == 2
    c.post("/api/log", json={"habit_id": i, "day": y, "count": 8})                          # counting again undoes the skip
    assert c.get(f"/api/logs?start={y}&end={y}").json()["logs"] == [[i, y, 8, False]]
    c.post("/api/log", json={"habit_id": i, "day": y, "count": 0})                          # zero removes the row
    assert c.get(f"/api/logs?start={y}&end={y}").json()["logs"] == []
    assert c.post(f"/api/habit/{j}", json={"name": "Read more", "days": "01"}).status_code == 200
    assert c.post("/api/habits/order", json={"ids": [j, i]}).status_code == 200
    assert [h["id"] for h in c.get("/api/state").json()["habits"]] == [j, i]
    assert c.delete(f"/api/habit/{j}").status_code == 200 and c.delete(f"/api/habit/{i}").status_code == 200
    assert c.get("/api/state").json()["habits"] == []

def test_password(c):
    c.post("/api/login", json={"password": os.environ["EDIT_PASSWORD"]})
    assert c.post("/api/password", json={"current": "nope", "new": "longenough1"}).status_code == 403
    assert c.post("/api/password", json={"current": os.environ["EDIT_PASSWORD"], "new": "short"}).status_code == 400
    assert c.post("/api/password", json={"current": os.environ["EDIT_PASSWORD"], "new": "longenough1"}).status_code == 200
    assert c.get("/api/state").json()["edit"] is True
    assert c.post("/api/login", json={"password": "longenough1"}).status_code == 200
