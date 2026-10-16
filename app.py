"""
SURVIVAL ROYALE — INFINITO 2026  |  Backend
FastAPI + SQLite. Answers never leave the server until a player submits.

Run:  ADMIN_PASSWORD=yourpass uvicorn app:app --host 0.0.0.0 --port 8000
"""
import csv, io, json, os, random, secrets, sqlite3, threading, time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

BASE = Path(__file__).parent
DB_PATH = os.environ.get("DB_PATH", str(BASE / "royale.db"))
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")  # REQUIRED for /admin
GRACE_SECONDS = 5          # network slack when accepting answers near the deadline
LOCK = threading.Lock()

PAPERS = json.loads((BASE / "questions.json").read_text(encoding="utf-8"))
QBANK = {q["id"]: q for p in PAPERS for q in p["questions"]}

DEFAULT_SETTINGS = {
    "duration_minutes": 60,        # same time for everyone (fair)
    "registration_open": True,     # admin can close/open the arena
    "access_code": "",             # optional join code, blank = none
    "reveal_review": True,         # show correct answers after submit
    "public_leaderboard": False,   # show leaderboard to players
    "one_attempt_per_name": True,  # same name+paper cannot start twice
}

app = FastAPI(title="Survival Royale API")
security = HTTPBasic()


# ───────────────────────── database ─────────────────────────
@contextmanager
def db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS attempts(
            id TEXT PRIMARY KEY,
            token TEXT NOT NULL,
            name TEXT NOT NULL,
            school TEXT DEFAULT '',
            paper INTEGER NOT NULL,
            order_json TEXT NOT NULL,       -- question ids in this player's order
            options_json TEXT NOT NULL,     -- option letter order per question (letters stay A-D)
            answers_json TEXT NOT NULL DEFAULT '{}',
            started_at REAL NOT NULL,
            deadline REAL NOT NULL,
            submitted_at REAL,
            auto_submitted INTEGER DEFAULT 0,
            correct INTEGER, wrong INTEGER, skipped INTEGER,
            tab_switches INTEGER DEFAULT 0,
            ip TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_attempts_paper ON attempts(paper, correct);
        """)


init_db()


def get_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    with db() as c:
        for r in c.execute("SELECT key,value FROM settings"):
            s[r["key"]] = json.loads(r["value"])
    return s


def set_settings(updates: dict):
    with db() as c:
        for k, v in updates.items():
            if k in DEFAULT_SETTINGS:
                c.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                          "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, json.dumps(v)))


# ───────────────────────── helpers ─────────────────────────
def load_attempt(attempt_id: str, token: str) -> sqlite3.Row:
    with db() as c:
        r = c.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
    if not r or not secrets.compare_digest(r["token"], token):
        raise HTTPException(403, "Invalid attempt or token.")
    return r


def grade(row) -> dict:
    answers = json.loads(row["answers_json"])
    order = json.loads(row["order_json"])
    correct = wrong = skipped = 0
    for qid in order:
        a = answers.get(qid)
        if not a:
            skipped += 1
        elif a == QBANK[qid]["a"]:
            correct += 1
        else:
            wrong += 1
    return {"correct": correct, "wrong": wrong, "skipped": skipped, "total": len(order)}


def finalize(attempt_id: str, auto: bool) -> sqlite3.Row:
    """Grade and lock an attempt (idempotent)."""
    with LOCK, db() as c:
        row = c.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
        if row["submitted_at"] is None:
            g = grade(row)
            now = min(time.time(), row["deadline"])  # never count time past the deadline
            c.execute("""UPDATE attempts SET submitted_at=?, auto_submitted=?,
                         correct=?, wrong=?, skipped=? WHERE id=?""",
                      (now, int(auto), g["correct"], g["wrong"], g["skipped"], attempt_id))
            row = c.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
        return row


def sweep_expired():
    """Auto-submit any attempt whose time ran out (e.g. player closed the tab)."""
    with db() as c:
        ids = [r["id"] for r in c.execute(
            "SELECT id FROM attempts WHERE submitted_at IS NULL AND deadline < ?",
            (time.time() - GRACE_SECONDS,))]
    for i in ids:
        finalize(i, auto=True)


def public_questions(row) -> list:
    order = json.loads(row["order_json"])
    return [{"id": qid, "q": QBANK[qid]["q"], "o": QBANK[qid]["o"]} for qid in order]


def build_review(row) -> list:
    answers = json.loads(row["answers_json"])
    out = []
    for qid in json.loads(row["order_json"]):
        q = QBANK[qid]
        val = {o["l"]: o["v"] for o in q["o"]}
        mine = answers.get(qid)
        out.append({"q": q["q"], "yours": mine, "yours_text": val.get(mine),
                    "correct": q["a"], "correct_text": val[q["a"]], "ok": mine == q["a"]})
    return out


def result_payload(row, settings) -> dict:
    p = {"name": row["name"], "paper": row["paper"], "correct": row["correct"], "wrong": row["wrong"],
         "skipped": row["skipped"], "total": row["correct"] + row["wrong"] + row["skipped"],
         "auto_submitted": bool(row["auto_submitted"]),
         "time_taken_sec": round(row["submitted_at"] - row["started_at"])}
    if settings["reveal_review"]:
        p["review"] = build_review(row)
    return p


# ───────────────────────── player API ─────────────────────────
class StartReq(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    school: str = Field("", max_length=80)
    paper: int
    shuffle: bool = True
    access_code: str = ""


@app.get("/api/config")
def config():
    s = get_settings()
    return {"duration_minutes": s["duration_minutes"], "registration_open": s["registration_open"],
            "needs_code": bool(s["access_code"]), "public_leaderboard": s["public_leaderboard"],
            "papers": [{"id": p["id"], "title": p["title"], "grade": p["grade"],
                        "count": len(p["questions"])} for p in PAPERS]}


@app.post("/api/start")
def start(req: StartReq, request: Request):
    s = get_settings()
    if not s["registration_open"]:
        raise HTTPException(403, "The arena is currently closed.")
    if s["access_code"] and not secrets.compare_digest(req.access_code.strip(), s["access_code"]):
        raise HTTPException(403, "Wrong access code.")
    if not 0 <= req.paper < len(PAPERS):
        raise HTTPException(400, "Unknown paper.")
    name = " ".join(req.name.split())
    sweep_expired()
    with LOCK, db() as c:
        if s["one_attempt_per_name"]:
            dup = c.execute("SELECT 1 FROM attempts WHERE lower(name)=lower(?) AND paper=?",
                            (name, req.paper)).fetchone()
            if dup:
                raise HTTPException(409, "This player already has an attempt for this arena. "
                                         "Ask the organiser to reset it if this is a mistake.")
        ids = [q["id"] for q in PAPERS[req.paper]["questions"]]
        if req.shuffle:
            random.SystemRandom().shuffle(ids)
        now = time.time()
        aid, token = secrets.token_hex(6), secrets.token_urlsafe(24)
        c.execute("""INSERT INTO attempts(id,token,name,school,paper,order_json,options_json,
                     started_at,deadline,ip) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                  (aid, token, name, req.school.strip(), req.paper, json.dumps(ids), "{}",
                   now, now + s["duration_minutes"] * 60,
                   request.client.host if request.client else ""))
        row = c.execute("SELECT * FROM attempts WHERE id=?", (aid,)).fetchone()
    return {"attempt_id": aid, "token": token, "name": name,
            "grade": PAPERS[req.paper]["grade"], "questions": public_questions(row),
            "answers": {}, "seconds_left": row["deadline"] - now}


class AttemptReq(BaseModel):
    attempt_id: str
    token: str


@app.post("/api/resume")
def resume(req: AttemptReq):
    """Lets a player refresh the page / reconnect without losing progress or time."""
    row = load_attempt(req.attempt_id, req.token)
    if row["submitted_at"] is None and time.time() > row["deadline"] + GRACE_SECONDS:
        row = finalize(row["id"], auto=True)
    if row["submitted_at"] is not None:
        return {"finished": True, "result": result_payload(row, get_settings())}
    return {"finished": False, "name": row["name"], "grade": PAPERS[row["paper"]]["grade"],
            "questions": public_questions(row), "answers": json.loads(row["answers_json"]),
            "seconds_left": row["deadline"] - time.time()}


class AnswerReq(AttemptReq):
    answers: Dict[str, Optional[str]]   # {question_id: "A"|"B"|"C"|"D"|null to clear}
    tab_switches: int = 0


@app.post("/api/answers")
def save_answers(req: AnswerReq):
    """Autosave. The client sends only changed answers; the server merges and validates."""
    row = load_attempt(req.attempt_id, req.token)
    if row["submitted_at"] is not None:
        raise HTTPException(409, "Already submitted.")
    now = time.time()
    if now > row["deadline"] + GRACE_SECONDS:
        finalize(row["id"], auto=True)
        raise HTTPException(410, "Time is up.")
    allowed = set(json.loads(row["order_json"]))
    with LOCK, db() as c:
        cur = json.loads(c.execute("SELECT answers_json FROM attempts WHERE id=?",
                                   (row["id"],)).fetchone()[0])
        for qid, letter in req.answers.items():
            if qid not in allowed:
                continue
            if letter is None:
                cur.pop(qid, None)
            elif letter in ("A", "B", "C", "D"):
                cur[qid] = letter
        c.execute("UPDATE attempts SET answers_json=?, tab_switches=MAX(tab_switches,?) WHERE id=?",
                  (json.dumps(cur), min(req.tab_switches, 10_000), row["id"]))
    return {"saved": len(cur), "seconds_left": row["deadline"] - now}


class SubmitReq(AnswerReq):
    auto: bool = False


@app.post("/api/submit")
def submit(req: SubmitReq):
    row = load_attempt(req.attempt_id, req.token)
    if row["submitted_at"] is None:
        # accept final answers only if inside the deadline (+grace)
        if time.time() <= row["deadline"] + GRACE_SECONDS and req.answers:
            save_answers(req)
        row = finalize(row["id"], auto=req.auto or time.time() > row["deadline"])
    return result_payload(row, get_settings())


@app.get("/api/leaderboard")
def leaderboard(paper: int, limit: int = 20):
    if not get_settings()["public_leaderboard"]:
        raise HTTPException(403, "Leaderboard is hidden by the organiser.")
    return leaderboard_rows(paper, limit)


def leaderboard_rows(paper: int, limit: int = 50):
    sweep_expired()
    with db() as c:
        rows = c.execute("""SELECT name, school, correct, (submitted_at-started_at) AS secs
                            FROM attempts WHERE paper=? AND submitted_at IS NOT NULL
                            ORDER BY correct DESC, secs ASC LIMIT ?""", (paper, limit)).fetchall()
    return [{"rank": i + 1, "name": r["name"], "school": r["school"], "score": r["correct"],
             "time_taken_sec": round(r["secs"])} for i, r in enumerate(rows)]


# ───────────────────────── admin API ─────────────────────────
def admin(creds: HTTPBasicCredentials = Depends(security)):
    if not ADMIN_PASSWORD:
        raise HTTPException(503, "Set the ADMIN_PASSWORD environment variable to enable admin.")
    ok = (secrets.compare_digest(creds.username, ADMIN_USER)
          and secrets.compare_digest(creds.password, ADMIN_PASSWORD))
    if not ok:
        raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Basic"})


@app.get("/admin", dependencies=[Depends(admin)])
def admin_page():
    return FileResponse(BASE / "static" / "admin.html")


@app.get("/api/admin/overview", dependencies=[Depends(admin)])
def admin_overview():
    sweep_expired()
    with db() as c:
        rows = c.execute("""SELECT id,name,school,paper,started_at,deadline,submitted_at,auto_submitted,
                            correct,wrong,skipped,tab_switches,answers_json FROM attempts
                            ORDER BY started_at DESC""").fetchall()
    out = []
    for r in rows:
        out.append({"id": r["id"], "name": r["name"], "school": r["school"], "paper": r["paper"],
                    "status": "submitted" if r["submitted_at"] else "in_progress",
                    "answered": len(json.loads(r["answers_json"])), "score": r["correct"],
                    "wrong": r["wrong"], "skipped": r["skipped"], "auto": bool(r["auto_submitted"]),
                    "tab_switches": r["tab_switches"], "started_at": r["started_at"],
                    "time_taken_sec": round(r["submitted_at"] - r["started_at"]) if r["submitted_at"] else None})
    return {"settings": get_settings(), "attempts": out,
            "leaderboards": {p["id"]: leaderboard_rows(p["id"]) for p in PAPERS}}


@app.post("/api/admin/settings", dependencies=[Depends(admin)])
def admin_settings(updates: dict):
    set_settings(updates)
    return get_settings()


@app.delete("/api/admin/attempts/{attempt_id}", dependencies=[Depends(admin)])
def admin_delete(attempt_id: str):
    with db() as c:
        c.execute("DELETE FROM attempts WHERE id=?", (attempt_id,))
    return {"deleted": attempt_id}


@app.get("/api/admin/export.csv", dependencies=[Depends(admin)])
def admin_export():
    sweep_expired()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["rank_in_paper", "name", "school", "arena", "score", "wrong", "skipped",
                "time_taken_sec", "auto_submitted", "tab_switches"])
    with db() as c:
        for p in PAPERS:
            rows = c.execute("""SELECT * FROM attempts WHERE paper=? AND submitted_at IS NOT NULL
                                ORDER BY correct DESC, (submitted_at-started_at) ASC""", (p["id"],))
            for i, r in enumerate(rows, 1):
                w.writerow([i, r["name"], r["school"], p["title"], r["correct"], r["wrong"],
                            r["skipped"], round(r["submitted_at"] - r["started_at"]),
                            r["auto_submitted"], r["tab_switches"]])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=survival_royale_results.csv"})


@app.get("/api/admin/attempts/{attempt_id}/review", dependencies=[Depends(admin)])
def admin_review(attempt_id: str):
    with db() as c:
        row = c.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Not found")
    return build_review(row)


# ───────────────────────── static frontend ─────────────────────────
@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html")


app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
