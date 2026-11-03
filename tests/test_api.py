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

def test_end_date():
    t = D(2026, 11, 3)
    hl = {D(2026, 10, 30): (1, False), D(2026, 10, 31): (1, False), D(2026, 11, 1): (1, False)}
    assert main.habit_stats(H(created=D(2026, 10, 30)), hl, t)["streak"] == 0             # without an end the missed 2 and 3 Nov break it
    s = main.habit_stats(H(created=D(2026, 10, 30), end_date=D(2026, 11, 1)), hl, t)      # days after the end date are not scheduled
    assert s["streak"] == 3 and s["rate"] == 100

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

def test_timer_and_description(c):
    c.post("/api/login", json={"password": os.environ["EDIT_PASSWORD"]})
    assert c.post("/api/habit", json={"name": "a", "kind": "bogus"}).status_code == 400
    assert c.post("/api/habit", json={"name": "a", "kind": "timer", "goal": 1441}).status_code == 400
    assert c.post("/api/habit", json={"name": "a", "end_date": "nope"}).status_code == 400
    assert c.post("/api/habit", json={"name": "a", "description": "x" * 501}).status_code == 400
    i = c.post("/api/habit", json={"name": "Run", "kind": "timer", "goal": 30, "unit": "ignored", "description": " go far ", "end_date": "2099-01-01"}).json()["id"]
    h = [x for x in c.get("/api/state").json()["habits"] if x["id"] == i][0]
    assert h["kind"] == "timer" and h["unit"] == "min" and h["goal"] == 30 and h["description"] == "go far" and h["end_date"] == "2099-01-01"
    t = main.today().isoformat()
    assert c.post("/api/log", json={"habit_id": i, "day": t, "count": 30}).json()["count"] == 30           # minutes
    assert c.post(f"/api/habit/{i}", json={"name": "Run", "kind": "units", "goal": 2, "end_date": None}).status_code == 200
    h = [x for x in c.get("/api/state").json()["habits"] if x["id"] == i][0]
    assert h["kind"] == "units" and h["end_date"] is None and h["description"] == ""
    c.delete(f"/api/habit/{i}")

def test_password(c):
    c.post("/api/login", json={"password": os.environ["EDIT_PASSWORD"]})
    assert c.post("/api/password", json={"current": "nope", "new": "longenough1"}).status_code == 403
    assert c.post("/api/password", json={"current": os.environ["EDIT_PASSWORD"], "new": "short"}).status_code == 400
    assert c.post("/api/password", json={"current": os.environ["EDIT_PASSWORD"], "new": "longenough1"}).status_code == 200
    assert c.get("/api/state").json()["edit"] is True
    assert c.post("/api/login", json={"password": "longenough1"}).status_code == 200

def test_repeat_modes():
    d = D(2026, 11, 3)
    assert main.sched(H(repeat="monthly", mdays="3,15"), d) and not main.sched(H(repeat="monthly", mdays="4"), d)
    assert main.sched(H(repeat="once", created=d), d) and not main.sched(H(repeat="once", created=d), D(2026, 11, 4))
    assert main.sched(H(repeat="weekly", wk=3), d)
    # weekly: 2 of 3 days in a week fails, 3 passes; the running week never breaks the streak
    hl = {}
    for w, n in ((2, 3), (3, 3), (4, 2)):                         # weeks starting Mon 12 Oct, 19 Oct, 26 Oct
        for i in range(n): hl[D(2026, 10, 12) + datetime.timedelta(days=7 * (w - 2) + i)] = (1, False)
    hl[D(2026, 11, 2)] = (1, False)
    h = H(repeat="weekly", wk=3, created=D(2026, 10, 5))
    s = main.habit_stats(h, hl, D(2026, 11, 3))
    assert s["best"] == 2 and s["streak"] == 0                    # 26 Oct week had 2 (<3) -> reset
    hl[D(2026, 10, 28)] = (1, False)
    assert main.habit_stats(h, hl, D(2026, 11, 3))["streak"] == 3

def test_new_fields_api(c):
    r = c.post("/api/habit", json={"name": "Pom", "emoji": "ic:tomato", "color": "#ffffff", "kind": "timer", "goal": 1, "days": "0123456",
              "intervals": [{"m": 25, "t": "f"}, {"m": 5, "t": "s"}, {"m": 25, "t": "f"}, {"m": 15, "t": "l"}]}); assert r.status_code == 200, r.text
    hb = [x for x in c.get("/api/state").json()["habits"] if x["name"] == "Pom"][0]
    assert hb["goal"] == 50 and hb["unit"] == "min" and len(hb["intervals"]) == 4
    bad = c.post("/api/habit", json={"name": "x", "kind": "timer", "intervals": [{"m": 5, "t": "s"}]}); assert bad.status_code == 400
    assert c.post("/api/habit", json={"name": "m", "repeat": "monthly", "mdays": ""}).status_code == 400
    ok = c.post("/api/habit", json={"name": "m", "repeat": "monthly", "mdays": "15,1"}); assert ok.status_code == 200
    assert [x for x in c.get("/api/state").json()["habits"] if x["name"] == "m"][0]["mdays"] == "1,15"
    assert c.post("/api/habit", json={"name": "w", "repeat": "weekly", "wk": 8}).status_code == 400
