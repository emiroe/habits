import os, sys, datetime, json
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

def test_vacation_and_week_start():
    t = D(2026, 11, 3)
    h = H(created=D(2026, 10, 25))
    hl = {D(2026, 11, 1): (1, False), D(2026, 11, 3): (1, False)}                    # 2 Nov missed during vacation
    assert main.habit_stats(h, hl, t)["streak"] == 1
    vac = [(D(2026, 11, 2), D(2026, 11, 2))]
    assert main.habit_stats(h, hl, t, vac)["streak"] == 2                             # the paused day keeps the streak alive
    assert main.in_vac(D(2026, 11, 2), vac) and not main.in_vac(D(2026, 11, 3), vac)
    w = H(repeat="weekly", wk=1, created=D(2026, 10, 1))
    wl = {D(2026, 11, 1): (1, False)}                                                  # Sunday 1 Nov: its own week with Sunday start
    assert main.habit_stats(w, wl, D(2026, 11, 3), (), 6)["streak"] == 1

def test_prefs_api(c):
    assert c.post("/api/prefs", json={"week_start": 3}).status_code == 400
    assert c.post("/api/prefs", json={"vacation": True, "week_start": 6}).status_code == 200
    s = c.get("/api/state").json(); assert s["vac"] is True and s["week_start"] == 6 and len(s["vac_periods"]) == 1
    assert c.post("/api/prefs", json={"vacation": False}).status_code == 200
    s = c.get("/api/state").json(); assert s["vac"] is False and s["vac_periods"] == []

def test_weekly_partial_vacation():
    w = H(repeat="weekly", wk=3, created=D(2026, 10, 1))
    wl = {D(2026, 10, 19): (1, False), D(2026, 10, 20): (1, False)}                    # week of 19 Oct: 2 of 3 done
    t = D(2026, 10, 28)
    assert main.habit_stats(w, wl, t)["streak"] == 0                                    # missed the goal
    vac = [(D(2026, 10, 21), D(2026, 10, 25))]                                          # only 2 days left that week: goal becomes 2
    assert main.habit_stats(w, wl, t, vac)["streak"] == 1

def test_clean_slate(c):
    c.post("/api/habit", json={"name": "gone"})
    assert c.post("/api/clean-slate", json={"confirm": "no"}).status_code == 400
    assert len(c.get("/api/state").json()["habits"]) >= 1
    assert c.post("/api/clean-slate", json={"confirm": "CLEAN"}).status_code == 200
    s = c.get("/api/state").json(); assert s["habits"] == [] and s["days"] == []

def test_push_basics(c):
    s = c.get("/api/state").json(); assert len(s["push_key"]) > 60
    assert c.post("/api/push/subscribe", json={"endpoint": "https://example.invalid/x", "p256dh": "a", "auth": "b"}).status_code == 200
    r = c.post("/api/habit", json={"name": "R", "remind": "07:30"}); assert r.status_code == 200
    hid = r.json()["id"]
    assert c.post("/api/habit", json={"name": "bad", "remind": "7:3"}).status_code == 400
    assert [h for h in c.get("/api/state").json()["habits"] if h["id"] == hid][0]["remind"] == "07:30"
    c.post("/api/habit/%d" % hid, json={"name": "R2"})                                   # remind omitted = unchanged
    assert [h for h in c.get("/api/state").json()["habits"] if h["id"] == hid][0]["remind"] == "07:30"
    c.post("/api/habit/%d" % hid, json={"name": "R2", "remind": ""})
    assert [h for h in c.get("/api/state").json()["habits"] if h["id"] == hid][0]["remind"] == ""
    assert c.post("/api/prefs", json={"nudge": True, "m_end": 13}).status_code == 200
    s = c.get("/api/state").json(); assert s["nudge"] is True and s["m_end"] == 13
    assert c.post("/api/prefs", json={"m_end": 20}).status_code == 400
    main.push_tick()           # no crash; the fake subscription fails silently

def _tm():
    return main.today()

def test_timer_on_server(c, monkeypatch):
    sent = []; monkeypatch.setattr(main, "push_all", lambda *a, **k: sent.append(a) or 1)
    c.post("/api/clean-slate", json={"confirm": "CLEAN"})
    hid = c.post("/api/habit", json={"name": "Focus", "kind": "timer", "intervals": [{"m": 1, "t": "f"}, {"m": 1, "t": "s"}]}).json()["id"]
    other = c.post("/api/habit", json={"name": "Other", "kind": "timer", "goal": 10}).json()["id"]
    assert c.post("/api/timer/start", json={"habit_id": hid}).status_code == 200
    assert c.post("/api/timer/start", json={"habit_id": other}).status_code == 409             # one timer at a time
    t = c.get("/api/state").json()["timer"]; assert t["habit_id"] == hid and t["running"] and t["elapsed_ms"] < 5000
    c.post("/api/timer/pause"); t = c.get("/api/state").json()["timer"]; assert not t["running"]
    c.post("/api/timer/start", json={"habit_id": hid})
    with main.pool.connection() as cn: cn.execute("UPDATE timer SET acc_ms=70000, started=now() - interval '10 seconds'")      # 80 s: second phase starts
    main.timer_tick(); assert any("Short break" in a[1] for a in sent)
    with main.pool.connection() as cn: cn.execute("UPDATE timer SET acc_ms=130000")                                              # past the end: finished
    main.timer_tick()
    s = c.get("/api/state").json(); assert s["timer"] is None
    assert [l[2] for l in s["logs"] if l[0] == hid] == [1]                                  # 1 focus minute logged by the server
    assert any("finished" in a[1] for a in sent)
    c.post("/api/timer/start", json={"habit_id": other})
    with main.pool.connection() as cn: cn.execute("UPDATE timer SET acc_ms=0, started=now() - interval '10 minutes'")
    r = c.post("/api/timer/stop").json(); assert r["minutes"] == 10

def test_export_import(c):
    c.post("/api/clean-slate", json={"confirm": "CLEAN"})
    a = c.post("/api/habit", json={"name": "A", "goal": 3, "unit": "glasses", "remind": "08:15"}).json()["id"]
    t = main.today().isoformat(); c.post("/api/log", json={"habit_id": a, "day": t, "count": 2})
    ex = c.get("/api/export").json(); assert ex["app"] == "habits" and len(ex["habits"]) == 1 and ex["logs"] == [[a, t, 2, False]]
    c.post("/api/clean-slate", json={"confirm": "CLEAN"}); assert c.get("/api/state").json()["habits"] == []
    r = c.post("/api/import", json=ex); assert r.status_code == 200 and r.json() == {"habits": 1, "logs": 1}, r.text
    s = c.get("/api/state").json(); h = s["habits"][0]; assert h["name"] == "A" and h["goal"] == 3 and h["remind"] == "08:15" and s["logs"][0][2] == 2
    assert c.post("/api/habit", json={"name": "next"}).json()["id"] > a                      # id sequence continues
    assert c.post("/api/import", json={"app": "other", "version": 1, "habits": [], "logs": []}).status_code == 400
    bad = {**ex, "habits": [{**ex["habits"][0], "goal": 5000}]}; assert c.post("/api/import", json=bad).status_code == 400

def test_achievement_push(c, monkeypatch):
    sent = []; monkeypatch.setattr(main, "push_all", lambda *a, **k: sent.append(a) or 1)
    c.post("/api/clean-slate", json={"confirm": "CLEAN"})
    a = c.post("/api/habit", json={"name": "A"}).json()["id"]
    main.ach_check(); assert sent == []                                                       # first run only records
    c.post("/api/log", json={"habit_id": a, "day": main.today().isoformat(), "count": 1})     # first perfect day + first check-in
    assert any("1 perfect day" in x[1] for x in sent), sent
    n = len(sent); c.post("/api/log", json={"habit_id": a, "day": main.today().isoformat(), "count": 1}); assert len(sent) == n   # not twice

def test_pomodoro_sync(c, monkeypatch):
    monkeypatch.setattr(main, "SYNC_FROM", "2000-01-01")
    import io, urllib.request
    c.post("/api/clean-slate", json={"confirm": "CLEAN"})
    hid = c.post("/api/habit", json={"name": "Focus on work", "unit": "pomodoros", "goal": 3, "sync": "pomodoro"}).json()["id"]
    t = main.today(); y = t - datetime.timedelta(days=1)
    sess = [{"day": t.isoformat(), "pomodoro": True}, {"day": t.isoformat(), "pomodoro": True}, {"day": t.isoformat(), "pomodoro": False}, {"day": y.isoformat(), "pomodoro": True}]
    def fake(url, timeout=0): return io.BytesIO(json.dumps({"sessions": sess, "settings": {"daily_goal": 8}}).encode())
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    main.tracker_sync()
    s = c.get("/api/state").json(); h = s["habits"][0]
    counts = {l[1]: l[2] for l in s["logs"] if l[0] == hid}; assert counts == {t.isoformat(): 2, y.isoformat(): 1} and h["goal"] == 8 and h["sync"] == "pomodoro"
    sess.pop(0); main.tracker_sync()                                                           # a deleted session lowers the count
    assert {l[1]: l[2] for l in c.get("/api/state").json()["logs"] if l[0] == hid}[t.isoformat()] == 1
    c.post("/api/log", json={"habit_id": hid, "day": y.isoformat(), "skipped": True}); sess.clear(); main.tracker_sync()
    assert any(l[1] == y.isoformat() and l[3] for l in c.get("/api/state").json()["logs"])      # skipped days are left alone

def test_chinese_sync(c, monkeypatch):
    import io, urllib.request
    c.post("/api/clean-slate", json={"confirm": "CLEAN"})
    hid = c.post("/api/habit", json={"name": "Learn Mandarin", "unit": "minutes", "goal": 120, "sync": "chinese"}).json()["id"]
    t = main.today().isoformat()
    sess = [{"day": t, "seconds": 2766}, {"day": t, "seconds": 1800}]
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=0: io.BytesIO(json.dumps({"sessions": sess, "daily_minutes": 90}).encode()))
    main.tracker_sync()
    s = c.get("/api/state").json(); assert [l[2] for l in s["logs"] if l[0] == hid] == [76] and s["habits"][0]["goal"] == 90        # 4566 s = 76 min
    assert c.post("/api/habit", json={"name": "x", "sync": "other"}).status_code == 400

def test_sync_ignores_days_before_start(c, monkeypatch):
    import io, urllib.request
    c.post("/api/clean-slate", json={"confirm": "CLEAN"})
    hid = c.post("/api/habit", json={"name": "M", "unit": "min", "goal": 60, "sync": "chinese"}).json()["id"]
    t = main.today().isoformat(); monkeypatch.setattr(main, "SYNC_FROM", t)
    old = (main.today() - datetime.timedelta(days=2)).isoformat()
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=0: io.BytesIO(json.dumps({"sessions": [{"day": old, "seconds": 600}, {"day": t, "seconds": 600}], "daily_minutes": 60}).encode()))
    main.tracker_sync()
    assert [l[1] for l in c.get("/api/state").json()["logs"] if l[0] == hid] == [t]

def test_reading_sync(c, monkeypatch):
    import io, urllib.request
    c.post("/api/clean-slate", json={"confirm": "CLEAN"})
    hid = c.post("/api/habit", json={"name": "Read", "unit": "min", "goal": 20, "sync": "reading"}).json()["id"]
    now = int(datetime.datetime.now().timestamp() * 1000)
    monkeypatch.setattr(main, "SYNC_FROM", "2000-01-01")
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=0: io.BytesIO(json.dumps({"stats": [{"start_time": now, "duration": 600}, {"start_time": now + 1000, "duration": 900}]}).encode()))
    main.tracker_sync()
    s = c.get("/api/state").json(); assert [l[2] for l in s["logs"] if l[0] == hid] == [25] and s["habits"][0]["goal"] == 20

def test_chores_period(c):
    hid = c.post("/api/habit", json={"name": "Chore", "period": "chores"}).json()["id"]
    assert [h["period"] for h in c.get("/api/state").json()["habits"] if h["id"] == hid] == ["chores"]

def test_trash_days(c):
    c.post("/api/clean-slate", json={"confirm": "CLEAN"})
    hid = c.post("/api/habit", json={"name": "Trash", "sync": "trash", "period": "morning"}).json()["id"]
    t = main.today(); tom = (t + datetime.timedelta(days=1)).isoformat()
    assert c.post("/api/trash", json={"dates": [tom]}).json()["days"] == 1
    s = c.get("/api/state").json(); assert s["trash"] == [tom]
    h = main.sched({"sync": "trash", "days": "", "created": t}, t + datetime.timedelta(days=1)); assert h
    assert not main.sched({"sync": "trash", "days": "", "created": t}, t)
    assert c.post("/api/trash", json={"dates": ["x"]}).status_code == 400

def test_month_habit(c):
    c.post("/api/clean-slate", json={"confirm": "CLEAN"})
    hid = c.post("/api/habit", json={"name": "Windows", "repeat": "month", "wk": 1}).json()["id"]
    s = c.get("/api/state").json(); h = s["habits"][0]; assert h["repeat"] == "month" and h["streak"] == 0
    t = main.today().isoformat()
    c.post("/api/log", json={"habit_id": hid, "day": t, "count": 1})
    h = c.get("/api/state").json()["habits"][0]; assert h["streak"] == 1 and h["total"] == 1
    with main.pool.connection() as cn: hs, lg = main.load(cn)
    assert main.open_habits(hs, lg, main.today(), [], 0, main.PERIODS) == []       # done this month: no longer open

def test_homework_sync_and_own_week(c, monkeypatch):
    import io, urllib.request
    c.post("/api/clean-slate", json={"confirm": "CLEAN"})
    hid = c.post("/api/habit", json={"name": "HW", "repeat": "weekly", "wk": 1, "days": "2", "sync": "homework"}).json()["id"]
    t = main.today().isoformat(); monkeypatch.setattr(main, "SYNC_FROM", "2000-01-01")
    sess = [{"day": t, "seconds": 600, "activity": "Homework"}, {"day": t, "seconds": 60, "activity": "Class"}]
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=0: io.BytesIO(json.dumps({"sessions": sess, "daily_minutes": 120}).encode()))
    main.tracker_sync()
    s = c.get("/api/state").json(); assert [l[2] for l in s["logs"] if l[0] == hid] == [1]
    assert main.hws({"repeat": "weekly", "days": "2"}, 0) == 2 and main.hws({"repeat": "weekly", "days": "0123456"}, 0) == 0


def test_achievements_specific(c):
    c.post("/api/clean-slate", json={"confirm": "CLEAN"})
    hid = c.post("/api/habit", json={"name": "Learn Mandarin", "unit": "min", "goal": 120, "sync": "chinese"}).json()["id"]
    c.post("/api/habit", json={"name": "Intimacy with Shuang", "goal": 1})
    a = {x["k"]: x for x in c.get("/api/state").json()["ach"]}
    assert {"streak", "checks", "perfect", "pstreak", "mandarin"} <= set(a) and "focus" not in a
    assert a["mandarin"]["tiers"][0] == 5 and a["mandarin"]["icon"] and a["mandarin"]["v"] == 0
    main.ach_check()


def test_cooling_after_done(c):
    import datetime
    c.post("/api/clean-slate", json={"confirm": "CLEAN"})
    m = c.post("/api/habit", json={"name": "Wash the windows", "repeat": "month", "wk": 1, "period": "chores"}).json()["id"]
    t = main.today(); hs = [{"id": 1, "name": "Wash", "repeat": "month", "wk": 1, "period": "chores", "days": "", "goal": 1, "created": t - datetime.timedelta(days=60), "end_date": None, "sync": ""}]
    day = lambda n: t - datetime.timedelta(days=n)
    assert main.open_habits(hs, {1: {day(15): (1, False)}}, t, [], 0, ("chores",)) == []
    assert main.open_habits(hs, {1: {day(16): (1, False)}}, t, [], 0, ("chores",)) == hs
    L = {day(1): (1, False)}
    assert not main.cooling({**hs[0], "name": "Go to the gym", "repeat": "weekly", "wk": 1}, L, t)
    assert main.cooling({**hs[0], "name": "Call parents", "repeat": "weekly", "wk": 1}, L, t)
    assert not main.cooling({**hs[0], "name": "Finish Chinese homework", "repeat": "weekly", "wk": 1, "sync": "homework"}, L, t)
    assert not main.cooling({**hs[0], "name": "Read", "repeat": "weekly", "wk": 3}, L, t)
    assert not main.cooling({**hs[0], "name": "Call parents", "repeat": "weekly", "wk": 1}, {day(5): (1, False)}, t)
