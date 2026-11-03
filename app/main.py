import os, json, hmac, hashlib, time, datetime, asyncio, base64
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager
from psycopg_pool import ConnectionPool
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

DB = os.environ["DATABASE_URL"]
PASSWORD = os.environ.get("EDIT_PASSWORD", "")
SECRET = os.environ["SECRET_KEY"].encode()
TZ = ZoneInfo("Europe/Amsterdam")
pool = ConnectionPool(DB, min_size=1, max_size=5, open=False)

SCHEMA = """
CREATE TABLE IF NOT EXISTS habits (id serial PRIMARY KEY, name text NOT NULL, emoji text NOT NULL DEFAULT '✅', color text NOT NULL DEFAULT '#6c5ce7',
  period text NOT NULL DEFAULT 'anytime',          -- morning | afternoon | evening | anytime | chores
  days text NOT NULL DEFAULT '0123456',            -- scheduled weekdays, Monday = 0
  goal int NOT NULL DEFAULT 1, unit text NOT NULL DEFAULT '',   -- goal 8 + unit 'glasses' = a counting habit
  position int NOT NULL DEFAULT 0, created date NOT NULL);
ALTER TABLE habits ADD COLUMN IF NOT EXISTS description text NOT NULL DEFAULT '';
ALTER TABLE habits ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'units';   -- units | timer (a timer habit's goal and counts are MINUTES)
ALTER TABLE habits ADD COLUMN IF NOT EXISTS end_date date;                          -- last day the habit is scheduled (NULL = no end)
ALTER TABLE habits ADD COLUMN IF NOT EXISTS repeat text NOT NULL DEFAULT 'daily';  -- daily (weekdays) | weekly (wk days per week) | month (wk days per month, any day) | monthly (mdays) | once
ALTER TABLE habits ADD COLUMN IF NOT EXISTS wk int NOT NULL DEFAULT 1;
ALTER TABLE habits ADD COLUMN IF NOT EXISTS mdays text NOT NULL DEFAULT '';          -- monthly: comma separated dates, e.g. '1,15'
ALTER TABLE habits ADD COLUMN IF NOT EXISTS remind text NOT NULL DEFAULT '';           -- reminder time 'HH:MM' (push notification), empty = none
ALTER TABLE habits ADD COLUMN IF NOT EXISTS sync text NOT NULL DEFAULT '';             -- 'pomodoro' = count comes from the Pomodoro site
ALTER TABLE habits ADD COLUMN IF NOT EXISTS intervals text NOT NULL DEFAULT '';      -- timer: JSON [{"m":25,"t":"f"},{"m":5,"t":"s"}] f=focus s=short break l=long break
CREATE TABLE IF NOT EXISTS logs (habit_id int NOT NULL REFERENCES habits(id) ON DELETE CASCADE, day date NOT NULL,
  count int NOT NULL DEFAULT 0, skipped boolean NOT NULL DEFAULT false, PRIMARY KEY (habit_id, day));
CREATE TABLE IF NOT EXISTS settings (key text PRIMARY KEY, value text NOT NULL);
CREATE TABLE IF NOT EXISTS timer (id int PRIMARY KEY DEFAULT 1 CHECK (id = 1), habit_id int NOT NULL REFERENCES habits(id) ON DELETE CASCADE, day date NOT NULL,
  acc_ms bigint NOT NULL DEFAULT 0, started timestamptz, notif int NOT NULL DEFAULT 0, goal_sent boolean NOT NULL DEFAULT false);
CREATE TABLE IF NOT EXISTS push_subs (endpoint text PRIMARY KEY, p256dh text NOT NULL, auth text NOT NULL);
CREATE TABLE IF NOT EXISTS push_sent (key text PRIMARY KEY, day date NOT NULL);
"""
PERIODS = ("morning", "afternoon", "evening", "anytime", "chores")   # chores behaves like anytime, in its own section
LOG_DAYS = 90      # /api/state carries this many days of logs; older ones come from /api/logs

@asynccontextmanager
async def lifespan(app):
    pool.open(wait=True)
    with pool.connection() as c: c.execute(SCHEMA)
    trash_load()
    task = asyncio.create_task(push_loop())
    yield
    task.cancel()
    pool.close()

app = FastAPI(lifespan=lifespan)

# Password: starts as EDIT_PASSWORD from .env; once changed in the app a scrypt hash lives in settings.pw_hash and wins.
# settings.pw_ver is mixed into the cookie signature, so changing the password signs every other device out.
# Forgot it? DELETE FROM settings WHERE key IN ('pw_hash','pw_ver')  -> the .env password works again.
def pw_state():
    with pool.connection() as c: return dict(c.execute("SELECT key,value FROM settings WHERE key IN ('pw_hash','pw_ver')").fetchall())
def hash_pw(pw, salt=None):
    salt = salt or os.urandom(16).hex()
    return salt + "$" + hashlib.scrypt(pw.encode(), salt=salt.encode(), n=2**14, r=8, p=1).hex()
def pw_ok(pw, st=None):
    st = pw_state() if st is None else st
    if "pw_hash" in st:
        salt, _ = st["pw_hash"].split("$")
        return hmac.compare_digest(hash_pw(pw, salt), st["pw_hash"])
    return bool(PASSWORD) and hmac.compare_digest(pw, PASSWORD)
def sign(ts, ver=None):
    ver = pw_state().get("pw_ver", "") if ver is None else ver
    return hmac.new(SECRET + ver.encode(), str(ts).encode(), hashlib.sha256).hexdigest()
def authed(req: Request):
    try: ts, sig = req.cookies.get("hb_auth", "").split(".")
    except ValueError: return False
    return hmac.compare_digest(sig, sign(ts)) and time.time() - int(ts) < 90 * 86400
def need_auth(req):
    if not authed(req): raise HTTPException(401, "login required")
def today(): return datetime.datetime.now(TZ).date()
def parse_day(s):
    try: return datetime.date.fromisoformat(s)
    except (ValueError, TypeError): raise HTTPException(400, "bad date")

class Login(BaseModel): password: str
class NewPassword(BaseModel): current: str; new: str
class HabitIn(BaseModel):
    name: str; emoji: str = "✅"; color: str = "#6c5ce7"; period: str = "anytime"; days: str = "0123456"; goal: int = 1; unit: str = ""
    description: str = ""; kind: str = "units"; end_date: str | None = None
    repeat: str = "daily"; wk: int = 1; mdays: str = ""; intervals: list[dict] = []
    remind: str | None = None          # 'HH:MM', '' = off, None = leave unchanged (updates only)
    sync: str | None = None            # 'pomodoro' | '' | None = unchanged
class LogIn(BaseModel):
    habit_id: int; day: str; count: int | None = None; skipped: bool | None = None
class Order(BaseModel): ids: list[int]

def set_cookie(resp, ver=None):
    ts = int(time.time())
    resp.set_cookie("hb_auth", "%d.%s" % (ts, sign(ts, ver)), max_age=90 * 86400, httponly=True, samesite="lax", secure=True)

@app.post("/api/login")
def login(b: Login, resp: Response):
    if not pw_ok(b.password): raise HTTPException(403, "wrong password")
    set_cookie(resp); return {"ok": True}

@app.post("/api/password")
def change_password(b: NewPassword, req: Request, resp: Response):
    need_auth(req)
    st = pw_state()
    if not pw_ok(b.current, st): raise HTTPException(403, "current password is wrong")
    if len(b.new) < 8: raise HTTPException(400, "new password needs at least 8 characters")
    ver = str(int(st.get("pw_ver", "0")) + 1)
    with pool.connection() as c:
        for k, v in (("pw_hash", hash_pw(b.new)), ("pw_ver", ver)):
            c.execute("INSERT INTO settings VALUES (%s,%s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", (k, v))
    set_cookie(resp, ver)       # this device stays signed in, every other one has to log in again
    return {"ok": True}

@app.post("/api/logout")
def logout(resp: Response):
    resp.delete_cookie("hb_auth"); return {"ok": True}

# ---- the maths (pure functions, tested without a database) ----
def mset(h): return {int(x) for x in h.get("mdays", "").split(",") if x.strip().isdigit()}
TRASH = set()          # ISO days with a waste collection (pushed from Home Assistant by /api/trash); habits with sync='trash' are due only then
def sched(h, d):
    if h.get("sync") == "trash": return d.isoformat() in TRASH and not (h.get("end_date") and d > h["end_date"])
    if h.get("end_date") and d > h["end_date"]: return False
    r = h.get("repeat", "daily")
    if r == "once": return d == h["created"]
    if r in ("weekly", "month"): return True                      # any day counts, the goal is wk days per week (see habit_stats)
    if r == "monthly": return d.day in mset(h)
    return str(d.weekday()) in h["days"]
def in_vac(d, vac):
    return any(a <= d and (b is None or d <= b) for a, b in vac)
def is_done(h, l): return bool(l) and not l[1] and l[0] >= h["goal"]
def start_of(h, hl): return min([h["created"]] + list(hl))      # a habit also exists on days logged before it was created

def habit_stats(h, hl, t, vac=(), ws=0):
    """hl = {day: (count, skipped)}. Streak = consecutive scheduled days done; a skipped day keeps it alive, a day not scheduled is ignored,
    today not done yet does not break it. Rate = done / due scheduled days (skipped ones excluded, today only once done)."""
    cur = best = total = hit = due = 0
    d = start_of(h, hl)
    if h.get("repeat") == "weekly": return weekly_stats(h, hl, t, vac, ws)
    if h.get("repeat") == "month": return month_stats(h, hl, t)
    while d <= t:
        l = hl.get(d); done = is_done(h, l)
        if done: total += 1
        v = in_vac(d, vac)       # vacation: nothing is due, the streak waits (doing the habit anyway still counts)
        if (sched(h, d) and not v or v and done) and not (l and l[1]):
            if done: cur += 1; best = max(best, cur); hit += 1; due += 1
            elif d != t: cur = 0; due += 1
        d += datetime.timedelta(days=1)
    return {"streak": cur, "best": best, "total": total, "rate": round(100 * hit / due) if due else None}

def hws(h, ws):
    """First weekday of a weekly habit's week: a single digit in `days` (0=Mon) gives that habit its own week start, else the global one."""
    return int(h["days"]) if h.get("repeat") == "weekly" and len(h.get("days") or "") == 1 and h["days"].isdigit() else ws
def weekly_stats(h, hl, t, vac=(), ws=0):
    """Weekly habit: a week succeeds with at least wk done days; the streak counts weeks. The week in progress never breaks it
    and the first (partial) week does not count against it."""
    ws = hws(h, ws); first = start_of(h, hl); wk = max(1, h.get("wk", 1))
    mon = lambda d: d - datetime.timedelta(days=(d.weekday() - ws) % 7)
    per = {}; total = 0
    for d, l in hl.items():
        if d <= t and is_done(h, l): total += 1; per[mon(d)] = per.get(mon(d), 0) + 1
    cur = best = hit = due = 0; m = mon(first)
    while m <= mon(t):
        avail = 7 - sum(in_vac(m + datetime.timedelta(days=i), vac) for i in range(7))     # vacation days lower the goal of that week
        ok = per.get(m, 0) >= min(wk, avail) and (avail > 0 or per.get(m, 0) > 0)
        if ok and avail > 0: cur += 1; best = max(best, cur); hit += 1; due += 1
        elif m < mon(t) and m != mon(first) and avail > 0: cur = 0; due += 1
        m += datetime.timedelta(days=7)
    return {"streak": cur, "best": best, "total": total, "rate": round(100 * hit / due) if due else None}

def month_stats(h, hl, t):
    """'N times per month, any day': a calendar month succeeds with at least wk done days; the streak counts months. The month in progress never
    breaks it and the first (partial) month does not count against it."""
    first = start_of(h, hl); n = max(1, h.get("wk", 1)); per = {}; total = 0
    for d, l in hl.items():
        if d <= t and is_done(h, l): total += 1; per[(d.year, d.month)] = per.get((d.year, d.month), 0) + 1
    cur = best = hit = due = 0; y, m = first.year, first.month
    while (y, m) <= (t.year, t.month):
        if per.get((y, m), 0) >= n: cur += 1; best = max(best, cur); hit += 1; due += 1
        elif (y, m) < (t.year, t.month) and (y, m) != (first.year, first.month): cur = 0; due += 1
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return {"streak": cur, "best": best, "total": total, "rate": round(100 * hit / due) if due else None}

def day_summary(habits, logs, t, n=400, vac=()):
    """[[day, done, total]] for the last n days, only days that had something scheduled. Skipped habits do not count."""
    out = []
    starts = {h["id"]: start_of(h, logs.get(h["id"], {})) for h in habits}
    for i in range(n - 1, -1, -1):
        d = t - datetime.timedelta(days=i); dn = tt = 0
        for h in habits:
            if d < starts[h["id"]]: continue
            l = logs.get(h["id"], {}).get(d)
            if l and l[1]: continue
            if (sched(h, d) and h.get("repeat") not in ("weekly", "month") and not in_vac(d, vac)) or is_done(h, l):
                tt += 1; dn += is_done(h, l)
        if tt: out.append([d.isoformat(), dn, tt])
    return out

def prefs(c):
    r = dict(c.execute("SELECT key,value FROM settings WHERE key IN ('vac','week_start')").fetchall())
    vac = [(parse_day(a), parse_day(b) if b else None) for a, b in json.loads(r.get("vac", "[]"))]
    return vac, int(r.get("week_start", 0))
def push_prefs(c):
    r = dict(c.execute("SELECT key,value FROM settings WHERE key IN ('nudge','m_end','e_start')").fetchall())
    return {"nudge": r.get("nudge", "0") == "1", "m_end": int(r.get("m_end", 12)), "e_start": int(r.get("e_start", 18))}

def load(c, only_active_ids=None):
    habits = [{"id": r[0], "name": r[1], "emoji": r[2], "color": r[3], "period": r[4], "days": r[5], "goal": r[6], "unit": r[7], "position": r[8], "created": r[9],
               "description": r[10], "kind": r[11], "end_date": r[12], "repeat": r[13], "wk": r[14], "mdays": r[15], "intervals": json.loads(r[16]) if r[16] else [], "remind": r[17], "sync": r[18]}
              for r in c.execute("SELECT id,name,emoji,color,period,days,goal,unit,position,created,description,kind,end_date,repeat,wk,mdays,intervals,remind,sync FROM habits ORDER BY position,id").fetchall()]
    logs = {}
    for hid, day, cnt, sk in c.execute("SELECT habit_id,day,count,skipped FROM logs").fetchall(): logs.setdefault(hid, {})[day] = (cnt, sk)
    return habits, logs

@app.get("/api/state")
def state(req: Request):
    if not authed(req): return {"edit": False}
    t = today()
    with pool.connection() as c: habits, logs = load(c); vac, ws = prefs(c); pp = push_prefs(c); tm = timer_public(timer_row(c))
    lo = t - datetime.timedelta(days=LOG_DAYS)
    out = []
    for h in habits:
        st = habit_stats(h, logs.get(h["id"], {}), t, vac, ws)
        out.append({**h, "created": h["created"].isoformat(), "end_date": h["end_date"].isoformat() if h["end_date"] else None, **st})
    recent = [[hid, d.isoformat(), v[0], v[1]] for hid, hl in logs.items() for d, v in hl.items() if d >= lo]
    return {"edit": True, "today": t.isoformat(), "habits": out, "logs": recent, "logs_from": lo.isoformat(), "days": day_summary(habits, logs, t, 1100, vac), "vac": in_vac(t, vac),
            "vac_periods": [[a.isoformat(), b.isoformat() if b else None] for a, b in vac], "week_start": ws, "timer": tm, "trash": sorted(TRASH), "ach": ach_list(*ach_values(habits, logs, t, vac, ws)), **pp, "push_key": vapid()[1]}

class PrefsIn(BaseModel):
    vacation: bool | None = None
    week_start: int | None = None
    nudge: bool | None = None
    m_end: int | None = None
    e_start: int | None = None

class TrashIn(BaseModel): dates: list[str]
def trash_load():
    global TRASH
    with pool.connection() as c: r = c.execute("SELECT value FROM settings WHERE key='trash'").fetchone()
    TRASH = set(json.loads(r[0])) if r else set()
@app.post("/api/trash")
def trash_set(b: TrashIn, req: Request):
    """Collection days from Home Assistant. Days before today are kept (history), future days are replaced by the given list."""
    need_auth(req); t = today().isoformat()
    new = {parse_day(x).isoformat() for x in b.dates}
    trash_load(); keep = {x for x in TRASH if x < t}
    with pool.connection() as c:
        c.execute("INSERT INTO settings VALUES ('trash',%s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", (json.dumps(sorted(keep | {x for x in new if x >= t})),))
    trash_load(); return {"days": len(TRASH)}

@app.post("/api/prefs")
def prefs_set(b: PrefsIn, req: Request):
    """Vacation mode (pauses all habits from today on, stats are kept) and the first day of the week (0 = Monday, 6 = Sunday)."""
    need_auth(req)
    if b.week_start is not None and b.week_start not in (0, 6): raise HTTPException(400, "bad week start")
    t = today()
    with pool.connection() as c:
        vac, ws = prefs(c)
        if b.vacation is not None:
            on = in_vac(t, vac)
            if b.vacation and not on: vac.append((t, None))
            elif not b.vacation and on:
                vac = [(a, bb) for a, bb in vac if not (bb is None and a >= t)]                          # started today: forget it
                vac = [(a, t - datetime.timedelta(days=1) if bb is None else bb) for a, bb in vac]       # otherwise it ends yesterday
            js = json.dumps([[a.isoformat(), bb.isoformat() if bb else None] for a, bb in vac])
            c.execute("INSERT INTO settings VALUES ('vac',%s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", (js,))
        if b.week_start is not None:
            c.execute("INSERT INTO settings VALUES ('week_start',%s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", (str(b.week_start),))
        for k, v in (("nudge", None if b.nudge is None else "1" if b.nudge else "0"), ("m_end", b.m_end), ("e_start", b.e_start)):
            if v is None: continue
            if k == "m_end" and not 9 <= int(v) <= 14 or k == "e_start" and not 15 <= int(v) <= 21: raise HTTPException(400, "bad time of day")
            c.execute("INSERT INTO settings VALUES (%s,%s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", (k, str(v)))
    return {"ok": True}

class CleanIn(BaseModel): confirm: str

@app.post("/api/clean-slate")
def clean_slate(b: CleanIn, req: Request):
    """Delete every habit and log (password and settings stay; vacation periods are cleared)."""
    need_auth(req)
    if b.confirm != "CLEAN": raise HTTPException(400, "type CLEAN to confirm")
    with pool.connection() as c:
        c.execute("DELETE FROM logs"); c.execute("DELETE FROM habits"); c.execute("DELETE FROM settings WHERE key IN ('vac','ach_seen')")
    return {"ok": True}

# ---- push notifications (Web Push, works for the app on the iPhone Home Screen) ----
def vapid():
    """(Vapid object, public key as urlsafe base64). The key pair is created once and kept in settings."""
    from py_vapid import Vapid
    from cryptography.hazmat.primitives import serialization as ser
    with pool.connection() as c:
        r = dict(c.execute("SELECT key,value FROM settings WHERE key IN ('vapid_priv','vapid_pub')").fetchall())
        if "vapid_priv" not in r:
            v = Vapid(); v.generate_keys()
            r["vapid_priv"] = base64.urlsafe_b64encode(v.private_key.private_numbers().private_value.to_bytes(32, "big")).decode().rstrip("=")
            r["vapid_pub"] = base64.urlsafe_b64encode(v.public_key.public_bytes(ser.Encoding.X962, ser.PublicFormat.UncompressedPoint)).decode().rstrip("=")
            for k in ("vapid_priv", "vapid_pub"): c.execute("INSERT INTO settings VALUES (%s,%s) ON CONFLICT (key) DO NOTHING", (k, r[k]))
    return Vapid.from_raw(r["vapid_priv"].encode()), r["vapid_pub"]

def push_all(title, body, badge=0, tag=None):
    """Send to every subscribed device; dead subscriptions are removed. Returns how many were sent."""
    from pywebpush import webpush, WebPushException
    v = vapid()[0]
    with pool.connection() as c: subs = c.execute("SELECT endpoint,p256dh,auth FROM push_subs").fetchall()
    sent = 0
    for ep, p, a in subs:
        try:
            webpush({"endpoint": ep, "keys": {"p256dh": p, "auth": a}}, json.dumps({"title": title, "body": body, "badge": badge, "tag": tag}),
                    vapid_private_key=v, vapid_claims={"sub": "mailto:admin@romijn.app"}, ttl=3600)
            sent += 1
        except WebPushException as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code in (404, 410):
                with pool.connection() as c: c.execute("DELETE FROM push_subs WHERE endpoint=%s", (ep,))
        except Exception: pass
    return sent

def cooling(h, hl, t):
    """Rest period after doing it: a monthly habit stays off the list for 15 days, a once-a-week habit (not the gym) for 4 days. Days before today only."""
    n = 15 if h["repeat"] == "month" else 4 if h["repeat"] == "weekly" and h["wk"] == 1 and "gym" not in h["name"].lower() else 0
    return any(is_done(h, hl.get(t - datetime.timedelta(days=i))) for i in range(1, n + 1))

def open_habits(habits, logs, t, vac, ws, parts):
    """Habits still to do today in the given parts of the day."""
    if in_vac(t, vac): return []
    out = []
    for h in habits:
        if h["period"] not in parts or not sched(h, t) or t < h["created"]: continue
        l = logs.get(h["id"], {}).get(t)
        if (l and l[1]) or is_done(h, l) or cooling(h, logs.get(h["id"], {}), t): continue
        if h["repeat"] == "weekly":
            mon = t - datetime.timedelta(days=(t.weekday() - hws(h, ws)) % 7)
            if sum(is_done(h, logs.get(h["id"], {}).get(mon + datetime.timedelta(days=i))) for i in range(7)) >= h["wk"]: continue
        if h["repeat"] == "month" and sum(is_done(h, v) for dd, v in logs.get(h["id"], {}).items() if (dd.year, dd.month) == (t.year, t.month)) >= h["wk"]: continue
        out.append(h)
    return out

def push_tick(now=None):
    now = now or datetime.datetime.now(TZ); t = now.date(); hm = now.strftime("%H:%M")
    with pool.connection() as c:
        if not c.execute("SELECT 1 FROM push_subs LIMIT 1").fetchone(): return
        habits, logs = load(c); vac, ws = prefs(c); pp = push_prefs(c)
        sent = {r[0] for r in c.execute("SELECT key FROM push_sent WHERE day=%s", (t,)).fetchall()}
        c.execute("DELETE FROM push_sent WHERE day < %s", (t - datetime.timedelta(days=2),))
    def due(at):   # from the set time until 3 hours later (a restart or a late phone still delivers it)
        return at <= hm and now - datetime.datetime.combine(t, datetime.time(int(at[:2]), int(at[3:])), TZ) < datetime.timedelta(hours=3)
    def mark(k):
        with pool.connection() as c: c.execute("INSERT INTO push_sent VALUES (%s,%s) ON CONFLICT DO NOTHING", (k, t))
    cur = "morning" if now.hour < pp["m_end"] else "afternoon" if now.hour < pp["e_start"] else "evening"
    badge = len(open_habits(habits, logs, t, vac, ws, (cur, "anytime", "chores")))
    for h in open_habits(habits, logs, t, vac, ws, PERIODS):
        k = "r%d" % h["id"]
        if h["remind"] and k not in sent and due(h["remind"]):
            mark(k); push_all(h["name"], "Time for %s · %d %s" % (h["name"], h["goal"], h["unit"] or "times") if h["kind"] != "timer" else "Time for %s · %d min" % (h["name"], h["goal"]), badge, k)
    if True:     # nudges at 06:00, 12:00 and 18:00 for everyone who has notifications on
        for part, at in (("morning", "06:00"), ("afternoon", "12:00"), ("evening", "18:00")):
            k = "n" + part
            if k in sent or not due(at): continue
            mark(k); hs = open_habits(habits, logs, t, vac, ws, (part, "anytime", "chores"))
            if hs: push_all("%s · %d to do" % (part.capitalize(), len(hs)), ", ".join(h["name"] for h in hs[:6]) + (" …" if len(hs) > 6 else ""), len(hs), k)

# ---- achievements: general ones plus medals tied to specific habits (matched by name/sync). Same list for the Awards page and the pushes ----
def _n(*w): return lambda h: any(x in h["name"].lower() for x in w)
GEN = [("streak", "Streak Master", "flame", "days in a row", [3, 7, 14, 30, 60, 100, 200, 365]), ("checks", "Check-ins", "check", "check-ins", [10, 50, 100, 250, 500, 1000, 2500, 5000]),
       ("perfect", "Perfect Days", "target", "perfect days", [1, 7, 14, 30, 60, 100, 200]), ("pstreak", "Perfect Streak", "trophy", "perfect days in a row", [3, 7, 14, 30, 60, 100])]
# key, title, fallback icon, match, metric (best = best streak, days = days done, sum = summed count / div), div, tiers, unit
SPEC = [("mandarin", "Mandarin", "globe", lambda h: h["sync"] == "chinese", "sum", 60, [5, 10, 25, 50, 100, 250, 500, 1000], "hours of Mandarin"),
        ("homework", "Homework", "book", lambda h: h["sync"] == "homework", "days", 1, [1, 4, 10, 20, 40], "weeks of Chinese homework"),
        ("focus", "Deep Focus", "tomato", lambda h: h["sync"] == "pomodoro", "sum", 1, [10, 50, 100, 250, 500, 1000], "pomodoros"),
        ("reading", "Bookworm", "book", lambda h: h["sync"] == "reading", "sum", 60, [3, 10, 25, 50, 100, 200], "hours of reading"),
        ("gym", "Iron Will", "dumbbell", _n("gym"), "days", 1, [5, 10, 25, 50, 100, 200], "workouts"),
        ("dog", "Dog Walker", "run", _n("walk the dog"), "days", 1, [10, 50, 100, 250, 500, 1000], "walks with the dog"),
        ("teeth", "Sparkling Smile", "tooth", _n("brush teeth"), "days", 1, [30, 100, 250, 500, 1000], "times brushed"),
        ("candy", "Sweet Tooth Tamed", "heart", _n("no candy"), "best", 1, [3, 7, 14, 30, 60, 100], "days without candy"),
        ("soda", "Soda Free", "cup", _n("no soda"), "best", 1, [3, 7, 14, 30, 60, 100], "days without soda"),
        ("early", "Early Bird", "alarm", _n("wake up early"), "days", 1, [7, 30, 60, 100, 200, 365], "early mornings"),
        ("budget", "Money Wise", "coin", _n("review budget", "spend money"), "days", 1, [10, 30, 100, 250, 500], "money check-ins"),
        ("trash", "Trash Hero", "trash", lambda h: h["sync"] == "trash", "days", 1, [5, 10, 25, 50, 100], "times the trash went out"),
        ("chores", "Clean Machine", "broom", lambda h: h["period"] == "chores" and h["sync"] != "trash", "days", 1, [10, 50, 100, 250, 500, 1000], "chores done"),
        ("carly", "Carly's Caretaker", "bowl", _n("carly"), "days", 1, [10, 50, 100, 250, 500, 1000], "times caring for Carly"),
        ("tummy", "Milou's Tummy Time", "heart", _n("tummy time"), "sum", 1, [30, 60, 150, 300, 600, 1200], "minutes of tummy time")]
def ach_values(habits, logs, t, vac, ws):
    days = day_summary(habits, logs, t, 1100, vac); st = {h["id"]: habit_stats(h, logs.get(h["id"], {}), t, vac, ws) for h in habits}
    m = {d[0]: d for d in days}; ps = 0
    for i in range(1100):
        x = m.get((t - datetime.timedelta(days=i)).isoformat())
        if not x: continue
        if x[2] and x[1] == x[2]: ps += 1
        elif i > 0: break
    v = {"streak": max([s["best"] for s in st.values()] or [0]), "checks": sum(d[1] for d in days), "perfect": sum(1 for d in days if d[2] and d[1] == d[2]), "pstreak": ps}
    icon = {}
    for k, _, ic, f, met, div, _, _ in SPEC:
        hs = [h for h in habits if f(h)]
        if not hs: continue
        icon[k] = hs[0]["emoji"][3:] if hs[0]["emoji"].startswith("ic:") else ic
        if met == "best": v[k] = max(st[h["id"]]["best"] for h in hs)
        elif met == "days": v[k] = sum(1 for h in hs for d, l in logs.get(h["id"], {}).items() if is_done(h, l))
        else: v[k] = sum(l[0] for h in hs for d, l in logs.get(h["id"], {}).items() if not l[1]) // div
    return v, icon
def ach_list(v, icon):
    out = [{"k": k, "title": t, "icon": ic, "unit": u, "tiers": L, "v": v[k]} for k, t, ic, u, L in GEN]
    out += [{"k": k, "title": t, "icon": icon[k], "unit": u, "tiers": L, "v": v[k]} for k, t, _, _, _, _, L, u in SPEC if k in v]
    return out
def ach_reached(v): return {"%s:%d" % (a["k"], x) for a in ach_list(v, {k: "" for k in v}) for x in a["tiers"] if a["v"] >= x}
def ach_label(k, x, icon={}):
    a = [a for a in ach_list({kk: 0 for kk in [g[0] for g in GEN] + [s[0] for s in SPEC]}, {s[0]: "" for s in SPEC}) if a["k"] == k][0]
    u = a["unit"]
    if x == 1: w = u.split(); w[0] = w[0].rstrip("s"); u = " ".join(w)
    return "%d %s" % (x, u)
def ach_check():
    with pool.connection() as c:
        habits, logs = load(c); vac, ws = prefs(c); r = c.execute("SELECT value FROM settings WHERE key='ach_seen'").fetchone()
        v2 = c.execute("SELECT 1 FROM settings WHERE key='ach_v2'").fetchone()
        now = ach_reached(ach_values(habits, logs, today(), vac, ws)[0])
        seen = set(json.loads(r[0])) if r else None
        if not v2:        # the new habit-specific medals: record what already exists without a push
            c.execute("INSERT INTO settings VALUES ('ach_v2','1') ON CONFLICT (key) DO NOTHING")
            c.execute("INSERT INTO settings VALUES ('ach_seen',%s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", (json.dumps(sorted(now | (seen or set()))),))
            return
        if seen is None or now - seen:       # first run only records what already exists
            c.execute("INSERT INTO settings VALUES ('ach_seen',%s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", (json.dumps(sorted(now | (seen or set()))),))
    if seen is None: return
    for key in sorted(now - seen, key=lambda k: (int(k.split(":")[1]))):
        k, x = key.split(":"); push_all("Achievement unlocked", ach_label(k, int(x)), 0, "ach-" + key)

# ---- the timer lives on the server: it keeps running (and logs the time) when the app is closed ----
def phase(iv, el):
    """el = elapsed seconds. -> (index, finished, focus seconds so far)  (same maths as tinfo() in index.html)"""
    acc = focus = 0
    for i, x in enumerate(iv):
        ln = x["m"] * 60
        if el < acc + ln: return i, False, focus + (el - acc if x["t"] == "f" else 0)
        if x["t"] == "f": focus += ln
        acc += ln
    return max(len(iv) - 1, 0), True, focus

def timer_row(c):
    r = c.execute("SELECT t.habit_id,t.day,t.acc_ms,t.started,t.notif,t.goal_sent,h.name,h.goal,h.intervals FROM timer t JOIN habits h ON h.id=t.habit_id WHERE t.id=1").fetchone()
    if not r: return None
    now = datetime.datetime.now(datetime.timezone.utc)
    run = r[3] is not None
    el = r[2] + (int((now - r[3]).total_seconds() * 1000) if run else 0)
    return {"habit_id": r[0], "day": r[1], "acc_ms": r[2], "running": run, "elapsed_ms": el, "notif": r[4], "goal_sent": r[5], "name": r[6], "goal": r[7], "iv": json.loads(r[8]) if r[8] else []}

def timer_public(t):
    return None if not t else {"habit_id": t["habit_id"], "day": t["day"].isoformat(), "acc_ms": t["acc_ms"], "running": t["running"], "elapsed_ms": t["elapsed_ms"], "iv": t["iv"]}

def timer_finish(c, t, manual):
    """Add the focus minutes to the habit's day and remove the timer. Returns the minutes logged."""
    el = t["elapsed_ms"] / 1000
    focus = phase(t["iv"], el)[2] if t["iv"] else el
    mins = round(focus / 60)
    if manual and focus >= 30: mins = max(1, mins)
    if mins > 0:
        cur = c.execute("SELECT count FROM logs WHERE habit_id=%s AND day=%s", (t["habit_id"], t["day"])).fetchone()
        n = min(9999, (cur[0] if cur else 0) + mins)
        c.execute("INSERT INTO logs VALUES (%s,%s,%s,false) ON CONFLICT (habit_id,day) DO UPDATE SET count=EXCLUDED.count, skipped=false", (t["habit_id"], t["day"], n))
    c.execute("DELETE FROM timer WHERE id=1")
    return mins

IVN = {"f": "Focus", "s": "Short break", "l": "Long break"}
def timer_tick():
    with pool.connection() as c:
        t = timer_row(c)
        if not t or not t["running"]: return
        msgs = []
        if t["iv"]:
            i, fin, _ = phase(t["iv"], t["elapsed_ms"] / 1000)
            if fin:
                mins = timer_finish(c, t, False)
                msgs.append((t["name"], "Timer finished · %d min logged" % mins, "t%d" % t["habit_id"]))
            elif i > t["notif"]:
                c.execute("UPDATE timer SET notif=%s WHERE id=1", (i,))
                x = t["iv"][i]; msgs.append((t["name"], "%s · %d min" % (IVN[x["t"]], x["m"]), "t%d" % t["habit_id"]))
        else:
            left = t["goal"] - (c.execute("SELECT coalesce(max(count),0) FROM logs WHERE habit_id=%s AND day=%s", (t["habit_id"], t["day"])).fetchone()[0])
            if not t["goal_sent"] and t["elapsed_ms"] / 60000 >= left > 0:
                c.execute("UPDATE timer SET goal_sent=true WHERE id=1"); msgs.append((t["name"], "Goal reached · %d min" % t["goal"], "t%d" % t["habit_id"]))
    for title, body, tag in msgs: push_all(title, body, 0, tag)
    if any("finished" in m[1] for m in msgs): ach_check()

class TimerIn(BaseModel): habit_id: int | None = None

@app.post("/api/timer/start")
def timer_start(b: TimerIn, req: Request):
    need_auth(req)
    with pool.connection() as c:
        h = c.execute("SELECT kind FROM habits WHERE id=%s", (b.habit_id,)).fetchone()
        if not h or h[0] != "timer": raise HTTPException(404, "no such timer habit")
        t = timer_row(c)
        if t and t["habit_id"] != b.habit_id: raise HTTPException(409, "another timer is running")
        if t:
            if not t["running"]: c.execute("UPDATE timer SET started=now() WHERE id=1")
        else: c.execute("INSERT INTO timer (habit_id,day,started) VALUES (%s,%s,now())", (b.habit_id, today()))
    return {"ok": True}

@app.post("/api/timer/pause")
def timer_pause(req: Request):
    need_auth(req)
    with pool.connection() as c:
        t = timer_row(c)
        if t and t["running"]: c.execute("UPDATE timer SET acc_ms=%s, started=NULL WHERE id=1", (t["elapsed_ms"],))
    return {"ok": True}

@app.post("/api/timer/stop")
def timer_stop(req: Request):
    need_auth(req)
    with pool.connection() as c:
        t = timer_row(c)
        mins = timer_finish(c, t, True) if t else 0
    try: ach_check()
    except Exception as e: print("ach error", e)
    return {"minutes": mins}

# ---- sync with the other trackers: a habit with sync='pomodoro' counts the pomodoros finished on the Pomodoro site, sync='chinese' the minutes
# studied on the Chinese tracker (the tracker is the timer; the daily goal follows the tracker's goal) ----
POMODORO_URL = os.environ.get("POMODORO_URL", "https://pomodoro.romijn.app")
CHINESE_URL = os.environ.get("CHINESE_URL", "https://chinese.romijn.app")
READING_URL = os.environ.get("READING_URL", "https://reading.romijn.app")
SYNC_FROM = os.environ.get("SYNC_FROM", "2026-11-03")      # synced habits ignore tracker data before this day
def sync_counts(src):
    """-> ({day: count}, goal) from the public /api/state of the tracker."""
    import urllib.request
    if src == "pomodoro":
        d = json.load(urllib.request.urlopen(POMODORO_URL + "/api/state", timeout=10)); per = {}
        for x in d.get("sessions", []):
            if x.get("pomodoro"): per[x["day"]] = per.get(x["day"], 0) + 1
        return per, int(d.get("settings", {}).get("daily_goal", 0))
    if src == "reading":     # KoInsight page stats: start_time in ms, duration in seconds; days in Dutch local time
        from zoneinfo import ZoneInfo
        d = json.load(urllib.request.urlopen(READING_URL + "/api/stats", timeout=20)); sec = {}
        for x in d.get("stats", []):
            day = datetime.datetime.fromtimestamp(x["start_time"] / 1000, ZoneInfo("Europe/Amsterdam")).date().isoformat()
            sec[day] = sec.get(day, 0) + x["duration"]
        return {k: round(v / 60) for k, v in sec.items()}, 0
    d = json.load(urllib.request.urlopen(CHINESE_URL + "/api/state", timeout=10)); sec = {}
    if src == "homework": return {x["day"]: 1 for x in d.get("sessions", []) if x.get("activity") == "Homework"}, 1
    for x in d.get("sessions", []): sec[x["day"]] = sec.get(x["day"], 0) + x["seconds"]
    return {k: round(v / 60) for k, v in sec.items()}, int(d.get("daily_minutes", 0))

def tracker_sync():
    t = today(); changed = False
    for src in ("pomodoro", "chinese", "reading", "homework"):
        with pool.connection() as c: hs = [r[0] for r in c.execute("SELECT id FROM habits WHERE sync=%s", (src,)).fetchall()]
        if not hs: continue
        try: per, goal = sync_counts(src)
        except Exception as e: print("sync error", src, e); continue
        days = set(per) | {(t - datetime.timedelta(days=i)).isoformat() for i in range(35)}
        with pool.connection() as c:
            for hid in hs:
                if 1 <= goal <= 999: c.execute("UPDATE habits SET goal=%s WHERE id=%s AND goal<>%s", (goal, hid, goal))
                cur = {r[0].isoformat(): (r[1], r[2]) for r in c.execute("SELECT day,count,skipped FROM logs WHERE habit_id=%s", (hid,)).fetchall()}
                for day in days:
                    want = min(per.get(day, 0), 9999); have = cur.get(day, (0, False))
                    if have[1] or want == have[0] or day > t.isoformat() or day < SYNC_FROM: continue
                    changed = True
                    if want == 0: c.execute("DELETE FROM logs WHERE habit_id=%s AND day=%s", (hid, day))
                    else: c.execute("INSERT INTO logs VALUES (%s,%s,%s,false) ON CONFLICT (habit_id,day) DO UPDATE SET count=EXCLUDED.count", (hid, day, want))
    if changed: ach_check()

async def push_loop():
    n = 0
    while True:
        try:
            await asyncio.to_thread(timer_tick)
            if n % 15 == 0: await asyncio.to_thread(push_tick)
            if n % 30 == 0:
                try: await asyncio.to_thread(tracker_sync)
                except Exception as e: print("sync error", e)
        except Exception as e: print("push error", e)
        n += 1
        await asyncio.sleep(2)

class SubIn(BaseModel): endpoint: str; p256dh: str; auth: str
class EpIn(BaseModel): endpoint: str

@app.post("/api/push/subscribe")
def push_subscribe(b: SubIn, req: Request):
    need_auth(req)
    with pool.connection() as c: c.execute("INSERT INTO push_subs VALUES (%s,%s,%s) ON CONFLICT (endpoint) DO UPDATE SET p256dh=EXCLUDED.p256dh, auth=EXCLUDED.auth", (b.endpoint, b.p256dh, b.auth))
    return {"ok": True}

@app.post("/api/push/unsubscribe")
def push_unsubscribe(b: EpIn, req: Request):
    need_auth(req)
    with pool.connection() as c: c.execute("DELETE FROM push_subs WHERE endpoint=%s", (b.endpoint,))
    return {"ok": True}

@app.post("/api/push/test")
def push_test(req: Request):
    need_auth(req)
    return {"sent": push_all("Habits", "Notifications are working.", 0, "test")}

@app.get("/sw.js")
def sw(): return FileResponse("static/sw.js", media_type="application/javascript", headers={"Cache-Control": "no-cache"})

# ---- export / import (a complete backup: every habit and every log) ----
HCOLS = "id,name,emoji,color,period,days,goal,unit,position,created,description,kind,end_date,repeat,wk,mdays,intervals,remind,sync"
@app.get("/api/export")
def export(req: Request):
    need_auth(req)
    with pool.connection() as c:
        hs = [dict(zip(HCOLS.split(","), r)) for r in c.execute("SELECT %s FROM habits ORDER BY position,id" % HCOLS).fetchall()]
        lg = [[r[0], r[1].isoformat(), r[2], r[3]] for r in c.execute("SELECT habit_id,day,count,skipped FROM logs ORDER BY habit_id,day").fetchall()]
        st = dict(c.execute("SELECT key,value FROM settings WHERE key IN ('vac','week_start','m_end','e_start')").fetchall())
    for h in hs:
        for k in ("created", "end_date"): h[k] = h[k].isoformat() if h[k] else None
    return {"app": "habits", "version": 1, "exported": datetime.datetime.now(TZ).isoformat(timespec="seconds"), "habits": hs, "logs": lg, "settings": st}

class ImportIn(BaseModel):
    app: str; version: int; habits: list[dict]; logs: list[list]; settings: dict = {}

@app.post("/api/import")
def import_data(b: ImportIn, req: Request):
    """Replace ALL habits and logs with the contents of an export file."""
    need_auth(req)
    if b.app != "habits" or b.version != 1: raise HTTPException(400, "this is not a Habits export file")
    if len(b.habits) > 500 or len(b.logs) > 300000: raise HTTPException(400, "file is too large")
    rows = []; ids = set()
    for h in b.habits:
        try:
            hid = int(h["id"]); iv = h.get("intervals") or ""
            m = HabitIn(name=h["name"], emoji=h.get("emoji") or "ic:check", color=h.get("color") or "#1e90ff", period=h.get("period") or "anytime", days=h.get("days") or "0123456",
                        goal=int(h.get("goal") or 1), unit=h.get("unit") or "", description=h.get("description") or "", kind=h.get("kind") or "units", end_date=h.get("end_date"),
                        repeat=h.get("repeat") or "daily", wk=int(h.get("wk") or 1), mdays=h.get("mdays") or "", intervals=json.loads(iv) if isinstance(iv, str) and iv else (iv if isinstance(iv, list) else []),
                        remind=h.get("remind") or "", sync=h.get("sync") or "")
            if m.kind == "timer" and not m.intervals: m.goal = int(h.get("goal") or 1)
            goal0 = m.goal; check(m)
            if m.kind == "timer" and not m.intervals: m.goal = goal0
            created = parse_day(h["created"]) if h.get("created") else today()
            rows.append((hid, m, int(h.get("position") or hid), created))
        except HTTPException as e: raise HTTPException(400, "habit %s: %s" % (h.get("name"), e.detail))
        except Exception: raise HTTPException(400, "the file has a habit that cannot be read")
        if hid in ids: raise HTTPException(400, "duplicate habit id")
        ids.add(hid)
    lrows = []
    for l in b.logs:
        try:
            hid, d, cnt, sk = int(l[0]), parse_day(l[1]), int(l[2]), bool(l[3])
        except Exception: raise HTTPException(400, "the file has a log that cannot be read")
        if hid in ids and 0 <= cnt <= 9999: lrows.append((hid, d, cnt, sk))
    with pool.connection() as c:
        c.execute("DELETE FROM timer"); c.execute("DELETE FROM logs"); c.execute("DELETE FROM habits")
        for hid, m, pos, created in rows:
            c.execute("INSERT INTO habits (id,name,emoji,color,period,days,goal,unit,position,created,description,kind,end_date,repeat,wk,mdays,intervals,remind,sync) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                      (hid, m.name, m.emoji.strip(), m.color, m.period, m.days, m.goal, m.unit.strip(), pos, created, m.description, m.kind, end_of(m), m.repeat, m.wk, m.mdays, json.dumps(m.intervals) if m.intervals else "", m.remind or "", m.sync or ""))
        c.execute("SELECT setval(pg_get_serial_sequence('habits','id'), greatest(1, (SELECT coalesce(max(id),1) FROM habits)))")
        with c.cursor() as cur: cur.executemany("INSERT INTO logs VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING", lrows)
        c.execute("DELETE FROM settings WHERE key IN ('vac','week_start','m_end','e_start','ach_seen')")
        for k in ("vac", "week_start", "m_end", "e_start"):
            if k in b.settings and isinstance(b.settings[k], str): c.execute("INSERT INTO settings VALUES (%s,%s)", (k, b.settings[k]))
    try: ach_check()
    except Exception as e: print("ach error", e)
    return {"habits": len(rows), "logs": len(lrows)}

@app.get("/api/logs")
def logs_range(req: Request, start: str, end: str, habit_id: int | None = None):
    need_auth(req)
    a, b = parse_day(start), parse_day(end)
    if (b - a).days > 400: raise HTTPException(400, "range too long")
    q, args = "SELECT habit_id,day,count,skipped FROM logs WHERE day BETWEEN %s AND %s", [a, b]
    if habit_id is not None: q += " AND habit_id=%s"; args.append(habit_id)
    with pool.connection() as c: rows = c.execute(q, args).fetchall()
    return {"logs": [[r[0], r[1].isoformat(), r[2], r[3]] for r in rows]}

def check(b: HabitIn):
    b.name = " ".join(b.name.split())
    if not b.name or len(b.name) > 60: raise HTTPException(400, "name needs 1-60 characters")
    if b.period not in PERIODS: raise HTTPException(400, "bad period")
    if not b.days or not set(b.days) <= set("0123456") or len(set(b.days)) != len(b.days): raise HTTPException(400, "pick at least one weekday")
    b.days = "".join(sorted(b.days))
    if b.repeat not in ("daily", "weekly", "monthly", "month", "once"): raise HTTPException(400, "bad repeat")
    if not 1 <= b.wk <= 7: raise HTTPException(400, "times per week must be 1-7")
    try: md = sorted({int(x) for x in b.mdays.split(",") if x.strip()})
    except ValueError: raise HTTPException(400, "bad month days")
    if any(not 1 <= x <= 31 for x in md): raise HTTPException(400, "bad month days")
    if b.repeat == "monthly" and not md: raise HTTPException(400, "pick at least one date")
    b.mdays = ",".join(map(str, md))
    if b.kind not in ("units", "timer"): raise HTTPException(400, "bad kind")
    if b.kind == "timer": b.unit = "min"                          # a timer habit counts minutes
    iv = []
    if b.kind == "timer":
        if len(b.intervals) > 20: raise HTTPException(400, "too many intervals")
        for x in b.intervals:
            m, t = x.get("m"), x.get("t")
            if not isinstance(m, int) or not 1 <= m <= 240 or t not in ("f", "s", "l"): raise HTTPException(400, "bad interval")
            iv.append({"m": m, "t": t})
        if iv:
            if not any(x["t"] == "f" for x in iv): raise HTTPException(400, "add at least one focus interval")
            b.goal = min(1440, sum(x["m"] for x in iv if x["t"] == "f"))
    b.intervals = iv
    if not 1 <= b.goal <= (1440 if b.kind == "timer" else 999): raise HTTPException(400, "goal must be 1-999 (1-1440 minutes for a timer)")
    if b.sync not in (None, "", "pomodoro", "chinese", "reading", "trash", "homework"): raise HTTPException(400, "bad sync")
    if b.remind:
        try: h_, m_ = b.remind.split(":"); assert len(b.remind) == 5 and 0 <= int(h_) < 24 and 0 <= int(m_) < 60
        except Exception: raise HTTPException(400, "bad reminder time")
    b.description = b.description.strip()
    if len(b.description) > 500: raise HTTPException(400, "description is too long")
    if len(b.unit) > 20 or not (b.emoji.strip() and len(b.emoji) <= 16): raise HTTPException(400, "bad emoji or unit")
    if len(b.color) != 7 or b.color[0] != "#" or any(ch not in "0123456789abcdefABCDEF" for ch in b.color[1:]): raise HTTPException(400, "bad colour")

def end_of(b): return parse_day(b.end_date) if b.end_date else None

@app.post("/api/habit")
def habit_add(b: HabitIn, req: Request):
    need_auth(req); check(b)
    with pool.connection() as c:
        pos = c.execute("SELECT coalesce(max(position),0)+1 FROM habits").fetchone()[0]
        i = c.execute("INSERT INTO habits (name,emoji,color,period,days,goal,unit,position,created,description,kind,end_date,repeat,wk,mdays,intervals,remind,sync) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                      (b.name, b.emoji.strip(), b.color, b.period, b.days, b.goal, b.unit.strip(), pos, today(), b.description, b.kind, end_of(b), b.repeat, b.wk, b.mdays, json.dumps(b.intervals) if b.intervals else "", b.remind or "", b.sync or "")).fetchone()[0]
    return {"id": i}

@app.post("/api/habit/{hid}")
def habit_update(hid: int, b: HabitIn, req: Request):
    need_auth(req); check(b)
    with pool.connection() as c:
        n = c.execute("UPDATE habits SET name=%s,emoji=%s,color=%s,period=%s,days=%s,goal=%s,unit=%s,description=%s,kind=%s,end_date=%s,repeat=%s,wk=%s,mdays=%s,intervals=%s,remind=coalesce(%s,remind),sync=coalesce(%s,sync) WHERE id=%s",
                      (b.name, b.emoji.strip(), b.color, b.period, b.days, b.goal, b.unit.strip(), b.description, b.kind, end_of(b), b.repeat, b.wk, b.mdays, json.dumps(b.intervals) if b.intervals else "", b.remind, b.sync, hid)).rowcount
    if not n: raise HTTPException(404, "no such habit")
    return {"ok": True}

@app.delete("/api/habit/{hid}")
def habit_delete(hid: int, req: Request):
    need_auth(req)
    with pool.connection() as c: c.execute("DELETE FROM habits WHERE id=%s", (hid,))
    return {"ok": True}

@app.post("/api/habits/order")
def habit_order(b: Order, req: Request):
    need_auth(req)
    with pool.connection() as c:
        for p, i in enumerate(b.ids, 1): c.execute("UPDATE habits SET position=%s WHERE id=%s", (p, i))
    return {"ok": True}

@app.post("/api/log")
def log_set(b: LogIn, req: Request):
    """Set the count and/or skipped flag of a habit on a day. count=0 and not skipped removes the row."""
    need_auth(req)
    d = parse_day(b.day)
    if d > today(): raise HTTPException(400, "cannot log the future")
    with pool.connection() as c:
        h = c.execute("SELECT goal FROM habits WHERE id=%s", (b.habit_id,)).fetchone()
        if not h: raise HTTPException(404, "no such habit")
        cur = c.execute("SELECT count,skipped FROM logs WHERE habit_id=%s AND day=%s", (b.habit_id, d)).fetchone() or (0, False)
        cnt = cur[0] if b.count is None else max(0, min(b.count, 9999))
        sk = cur[1] if b.skipped is None else b.skipped
        if b.count is not None and b.skipped is None: sk = False        # counting again undoes a skip
        if cnt == 0 and not sk: c.execute("DELETE FROM logs WHERE habit_id=%s AND day=%s", (b.habit_id, d))
        else: c.execute("INSERT INTO logs VALUES (%s,%s,%s,%s) ON CONFLICT (habit_id,day) DO UPDATE SET count=EXCLUDED.count, skipped=EXCLUDED.skipped",
                        (b.habit_id, d, cnt, sk))
    try: ach_check()
    except Exception as e: print("ach error", e)
    return {"count": cnt, "skipped": sk}

@app.get("/health")
def health():
    with pool.connection() as c: c.execute("SELECT 1")
    return {"ok": True}

@app.get("/favicon.ico")
def favicon(): return FileResponse("static/icons/favicon.ico", media_type="image/x-icon")

@app.get("/manifest.webmanifest")
def manifest(): return FileResponse("static/manifest.webmanifest", media_type="application/manifest+json", headers={"Cache-Control": "no-cache"})

@app.get("/")
def index(): return FileResponse("static/index.html", headers={"Cache-Control": "no-cache"})
app.mount("/static", StaticFiles(directory="static"), name="static")
