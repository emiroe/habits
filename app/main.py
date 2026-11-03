import os, json, hmac, hashlib, time, datetime
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
  period text NOT NULL DEFAULT 'anytime',          -- morning | afternoon | evening | anytime
  days text NOT NULL DEFAULT '0123456',            -- scheduled weekdays, Monday = 0
  goal int NOT NULL DEFAULT 1, unit text NOT NULL DEFAULT '',   -- goal 8 + unit 'glasses' = a counting habit
  position int NOT NULL DEFAULT 0, created date NOT NULL);
ALTER TABLE habits ADD COLUMN IF NOT EXISTS description text NOT NULL DEFAULT '';
ALTER TABLE habits ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'units';   -- units | timer (a timer habit's goal and counts are MINUTES)
ALTER TABLE habits ADD COLUMN IF NOT EXISTS end_date date;                          -- last day the habit is scheduled (NULL = no end)
ALTER TABLE habits ADD COLUMN IF NOT EXISTS repeat text NOT NULL DEFAULT 'daily';  -- daily (weekdays) | weekly (wk days per week) | monthly (mdays) | once
ALTER TABLE habits ADD COLUMN IF NOT EXISTS wk int NOT NULL DEFAULT 1;
ALTER TABLE habits ADD COLUMN IF NOT EXISTS mdays text NOT NULL DEFAULT '';          -- monthly: comma separated dates, e.g. '1,15'
ALTER TABLE habits ADD COLUMN IF NOT EXISTS intervals text NOT NULL DEFAULT '';      -- timer: JSON [{"m":25,"t":"f"},{"m":5,"t":"s"}] f=focus s=short break l=long break
CREATE TABLE IF NOT EXISTS logs (habit_id int NOT NULL REFERENCES habits(id) ON DELETE CASCADE, day date NOT NULL,
  count int NOT NULL DEFAULT 0, skipped boolean NOT NULL DEFAULT false, PRIMARY KEY (habit_id, day));
CREATE TABLE IF NOT EXISTS settings (key text PRIMARY KEY, value text NOT NULL);
"""
PERIODS = ("morning", "afternoon", "evening", "anytime")
LOG_DAYS = 90      # /api/state carries this many days of logs; older ones come from /api/logs

@asynccontextmanager
async def lifespan(app):
    pool.open(wait=True)
    with pool.connection() as c: c.execute(SCHEMA)
    yield
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
def sched(h, d):
    if h.get("end_date") and d > h["end_date"]: return False
    r = h.get("repeat", "daily")
    if r == "once": return d == h["created"]
    if r == "weekly": return True                      # any day counts, the goal is wk days per week (see habit_stats)
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
    while d <= t:
        l = hl.get(d); done = is_done(h, l)
        if done: total += 1
        v = in_vac(d, vac)       # vacation: nothing is due, the streak waits (doing the habit anyway still counts)
        if (sched(h, d) and not v or v and done) and not (l and l[1]):
            if done: cur += 1; best = max(best, cur); hit += 1; due += 1
            elif d != t: cur = 0; due += 1
        d += datetime.timedelta(days=1)
    return {"streak": cur, "best": best, "total": total, "rate": round(100 * hit / due) if due else None}

def weekly_stats(h, hl, t, vac=(), ws=0):
    """Weekly habit: a week succeeds with at least wk done days; the streak counts weeks. The week in progress never breaks it
    and the first (partial) week does not count against it."""
    first = start_of(h, hl); wk = max(1, h.get("wk", 1))
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
            if (sched(h, d) and h.get("repeat") != "weekly" and not in_vac(d, vac)) or is_done(h, l):
                tt += 1; dn += is_done(h, l)
        if tt: out.append([d.isoformat(), dn, tt])
    return out

def prefs(c):
    r = dict(c.execute("SELECT key,value FROM settings WHERE key IN ('vac','week_start')").fetchall())
    vac = [(parse_day(a), parse_day(b) if b else None) for a, b in json.loads(r.get("vac", "[]"))]
    return vac, int(r.get("week_start", 0))

def load(c, only_active_ids=None):
    habits = [{"id": r[0], "name": r[1], "emoji": r[2], "color": r[3], "period": r[4], "days": r[5], "goal": r[6], "unit": r[7], "position": r[8], "created": r[9],
               "description": r[10], "kind": r[11], "end_date": r[12], "repeat": r[13], "wk": r[14], "mdays": r[15], "intervals": json.loads(r[16]) if r[16] else []}
              for r in c.execute("SELECT id,name,emoji,color,period,days,goal,unit,position,created,description,kind,end_date,repeat,wk,mdays,intervals FROM habits ORDER BY position,id").fetchall()]
    logs = {}
    for hid, day, cnt, sk in c.execute("SELECT habit_id,day,count,skipped FROM logs").fetchall(): logs.setdefault(hid, {})[day] = (cnt, sk)
    return habits, logs

@app.get("/api/state")
def state(req: Request):
    if not authed(req): return {"edit": False}
    t = today()
    with pool.connection() as c: habits, logs = load(c); vac, ws = prefs(c)
    lo = t - datetime.timedelta(days=LOG_DAYS)
    out = []
    for h in habits:
        st = habit_stats(h, logs.get(h["id"], {}), t, vac, ws)
        out.append({**h, "created": h["created"].isoformat(), "end_date": h["end_date"].isoformat() if h["end_date"] else None, **st})
    recent = [[hid, d.isoformat(), v[0], v[1]] for hid, hl in logs.items() for d, v in hl.items() if d >= lo]
    return {"edit": True, "today": t.isoformat(), "habits": out, "logs": recent, "logs_from": lo.isoformat(), "days": day_summary(habits, logs, t, 400, vac), "vac": in_vac(t, vac),
            "vac_periods": [[a.isoformat(), b.isoformat() if b else None] for a, b in vac], "week_start": ws}

class PrefsIn(BaseModel):
    vacation: bool | None = None
    week_start: int | None = None

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
    return {"ok": True}

class CleanIn(BaseModel): confirm: str

@app.post("/api/clean-slate")
def clean_slate(b: CleanIn, req: Request):
    """Delete every habit and log (password and settings stay; vacation periods are cleared)."""
    need_auth(req)
    if b.confirm != "CLEAN": raise HTTPException(400, "type CLEAN to confirm")
    with pool.connection() as c:
        c.execute("DELETE FROM logs"); c.execute("DELETE FROM habits"); c.execute("DELETE FROM settings WHERE key='vac'")
    return {"ok": True}

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
    if b.repeat not in ("daily", "weekly", "monthly", "once"): raise HTTPException(400, "bad repeat")
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
        if len(b.intervals) > 12: raise HTTPException(400, "too many intervals")
        for x in b.intervals:
            m, t = x.get("m"), x.get("t")
            if not isinstance(m, int) or not 1 <= m <= 240 or t not in ("f", "s", "l"): raise HTTPException(400, "bad interval")
            iv.append({"m": m, "t": t})
        if iv:
            if not any(x["t"] == "f" for x in iv): raise HTTPException(400, "add at least one focus interval")
            b.goal = min(1440, sum(x["m"] for x in iv if x["t"] == "f"))
    b.intervals = iv
    if not 1 <= b.goal <= (1440 if b.kind == "timer" else 999): raise HTTPException(400, "goal must be 1-999 (1-1440 minutes for a timer)")
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
        i = c.execute("INSERT INTO habits (name,emoji,color,period,days,goal,unit,position,created,description,kind,end_date,repeat,wk,mdays,intervals) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                      (b.name, b.emoji.strip(), b.color, b.period, b.days, b.goal, b.unit.strip(), pos, today(), b.description, b.kind, end_of(b), b.repeat, b.wk, b.mdays, json.dumps(b.intervals) if b.intervals else "")).fetchone()[0]
    return {"id": i}

@app.post("/api/habit/{hid}")
def habit_update(hid: int, b: HabitIn, req: Request):
    need_auth(req); check(b)
    with pool.connection() as c:
        n = c.execute("UPDATE habits SET name=%s,emoji=%s,color=%s,period=%s,days=%s,goal=%s,unit=%s,description=%s,kind=%s,end_date=%s,repeat=%s,wk=%s,mdays=%s,intervals=%s WHERE id=%s",
                      (b.name, b.emoji.strip(), b.color, b.period, b.days, b.goal, b.unit.strip(), b.description, b.kind, end_of(b), b.repeat, b.wk, b.mdays, json.dumps(b.intervals) if b.intervals else "", hid)).rowcount
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
