#!/usr/bin/env python3
"""
Personal Learning Assistant – v3 (Obsidian Hybrid)
==================================================

Architektur:
  - Obsidian-Vault = Source of Truth fuer Inhalt UND Scheduling (im YAML-Frontmatter).
  - SQLite        = schnell abfragbarer Index/Cache + reiner Algorithmus-State
                    (Streak, kognitive Tageslast), der keinen Notiz-Bezug hat.

Bei jedem schreibenden Vorgang (log_lecture, review_topic) wird BEIDES aktualisiert:
  1. Das Frontmatter der Markdown-Notiz im Vault  (menschenlesbare Wahrheit)
  2. Die SQLite-Zeile                             (schnelle Queries)

Konfiguration ueber Umgebungsvariablen (in claude_desktop_config.json -> env):
  OBSIDIAN_VAULT_PATH   absoluter Pfad zum Vault-Root (Pflicht)
  OBSIDIAN_LERNEN_DIR   Unterordner fuer Lernthemen   (Default: "📚 Lernen")
  LEARNING_DB_PATH      Pfad zur SQLite-Index-DB        (Default: <vault>/.learning_index.db)
  SBB_API_BASE          (Default: https://transport.opendata.ch/v1)
  SBB_TRAVEL_FALLBACK_MIN (Default: 30)
"""

import os
import re
import sqlite3
import math
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import List, Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv not required; env vars may come from MCP config instead

import frontmatter  # python-frontmatter
from mcp.server.fastmcp import FastMCP

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

mcp = FastMCP("personal-learning-assistant")

# ------------------------------------------------------------------
# KONFIGURATION
# ------------------------------------------------------------------
VAULT_PATH = os.environ.get("OBSIDIAN_VAULT_PATH", "").strip()
LERNEN_DIR = os.environ.get("OBSIDIAN_LERNEN_DIR", "📚 Lernen")
SBB_API_BASE = os.environ.get("SBB_API_BASE", "https://transport.opendata.ch/v1")
SBB_FALLBACK_MIN = int(os.environ.get("SBB_TRAVEL_FALLBACK_MIN", "30"))

if not VAULT_PATH:
    # Wir crashen nicht hart – aber jede Vault-Operation meldet den fehlenden Pfad.
    VAULT_PATH = ""

DB_PATH = os.environ.get(
    "LEARNING_DB_PATH",
    str(Path(VAULT_PATH) / ".learning_index.db") if VAULT_PATH else "learning_index.db",
)

# Mehrstufige Ebbinghaus-/SM-2-Intervalle (Tage) als Startpunkt je Verstaendnis-Score.
# Score 0-5; niedriger Score => kuerzeres erstes Intervall.
INITIAL_INTERVALS = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 6}


# ------------------------------------------------------------------
# DB-SETUP
# ------------------------------------------------------------------
def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = _db()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS topics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic_name TEXT NOT NULL,
            module TEXT NOT NULL,
            vault_path TEXT NOT NULL,
            understanding_score INTEGER,
            ease_factor REAL DEFAULT 2.5,
            interval INTEGER DEFAULT 1,
            repetitions INTEGER DEFAULT 0,
            next_review TEXT,
            last_reviewed TEXT,
            UNIQUE(module, topic_name)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS cognitive_load (
            date TEXT PRIMARY KEY,
            load_points INTEGER
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS streak (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            current_streak INTEGER DEFAULT 0,
            longest_streak INTEGER DEFAULT 0,
            last_active TEXT
        )
    """)
    c.execute("INSERT OR IGNORE INTO streak (id, current_streak, longest_streak) VALUES (1, 0, 0)")
    conn.commit()
    conn.close()


# ------------------------------------------------------------------
# OBSIDIAN-HELFER
# ------------------------------------------------------------------
def _vault_ok() -> Optional[str]:
    """Gibt eine Fehlermeldung zurueck, wenn der Vault nicht erreichbar ist – sonst None."""
    if not VAULT_PATH:
        return "⚠️ OBSIDIAN_VAULT_PATH ist nicht gesetzt. Bitte in der MCP-Config eintragen."
    if not Path(VAULT_PATH).is_dir():
        return f"⚠️ Vault-Pfad existiert nicht: {VAULT_PATH}"
    return None


def _slugify(name: str) -> str:
    """Dateiname-sicherer Slug, behaelt Buchstaben/Zahlen/Bindestriche."""
    s = name.strip().replace(" ", "-")
    s = re.sub(r"[^\w\-äöüÄÖÜéèàâ]", "", s, flags=re.UNICODE)
    return s or "Thema"


def _note_path(module: str, topic_name: str) -> Path:
    """Pfad zur Themen-Notiz: <vault>/<LERNEN_DIR>/<module>/<topic>.md"""
    return Path(VAULT_PATH) / LERNEN_DIR / _slugify(module) / f"{_slugify(topic_name)}.md"


def _read_or_new_note(path: Path, module: str, topic_name: str) -> "frontmatter.Post":
    if path.exists():
        return frontmatter.load(str(path))
    body = (
        f"# {topic_name}\n\n"
        f"## 📊 Quiz Ergebnis\n\n"
        f"## ✅ Gut verstanden\n\n"
        f"## ❌ Noch üben\n\n"
        f"## 📝 Notizen\n"
    )
    post = frontmatter.Post(body)
    post["type"] = "lernthema"
    post["module"] = module
    return post


def _write_note(path: Path, post: "frontmatter.Post"):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = frontmatter.dumps(post)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


# ------------------------------------------------------------------
# SM-2 / EBBINGHAUS-KERN
# ------------------------------------------------------------------
def _sm2(score: int, ease: float, interval: int, reps: int):
    """
    Vereinfachtes SM-2. score 0-5.
    Gibt (next_interval_days, new_ease, new_reps) zurueck.
    """
    score = max(0, min(5, int(score)))
    if score < 3:
        # Fehlversuch -> zurueck auf kurzes Intervall, reps reset
        return INITIAL_INTERVALS[score], max(1.3, ease - 0.2), 0
    new_ease = ease + (0.1 - (5 - score) * (0.08 + (5 - score) * 0.02))
    new_ease = max(1.3, new_ease)
    new_reps = reps + 1
    if new_reps == 1:
        new_interval = INITIAL_INTERVALS[score]
    elif new_reps == 2:
        new_interval = 6
    else:
        new_interval = math.ceil(interval * new_ease)
    return new_interval, round(new_ease, 2), new_reps


def _sync_frontmatter(post, module, topic_name, score, ease, interval, reps, next_review, last_reviewed):
    post["type"] = "lernthema"
    post["module"] = module
    post["topic"] = topic_name
    post["understanding_score"] = score
    post["ease_factor"] = ease
    post["interval"] = interval
    post["repetitions"] = reps
    post["next_review"] = next_review
    post["last_reviewed"] = last_reviewed


def _upsert_sqlite(topic_name, module, vault_path, score, ease, interval, reps, next_review, last_reviewed):
    conn = _db()
    c = conn.cursor()
    c.execute("""
        INSERT INTO topics
          (topic_name, module, vault_path, understanding_score, ease_factor,
           interval, repetitions, next_review, last_reviewed)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(module, topic_name) DO UPDATE SET
          vault_path=excluded.vault_path,
          understanding_score=excluded.understanding_score,
          ease_factor=excluded.ease_factor,
          interval=excluded.interval,
          repetitions=excluded.repetitions,
          next_review=excluded.next_review,
          last_reviewed=excluded.last_reviewed
    """, (topic_name, module, vault_path, score, ease, interval, reps, next_review, last_reviewed))
    conn.commit()
    conn.close()


def _touch_streak():
    """Aktualisiert den Streak bei Lern-Aktivitaet heute."""
    today = date.today().isoformat()
    conn = _db()
    c = conn.cursor()
    row = c.execute("SELECT current_streak, longest_streak, last_active FROM streak WHERE id=1").fetchone()
    cur, longest, last = row["current_streak"], row["longest_streak"], row["last_active"]
    if last == today:
        pass  # heute schon aktiv
    elif last == (date.today() - timedelta(days=1)).isoformat():
        cur += 1
    else:
        cur = 1
    longest = max(longest, cur)
    c.execute("UPDATE streak SET current_streak=?, longest_streak=?, last_active=? WHERE id=1",
              (cur, longest, today))
    conn.commit()
    conn.close()
    return cur, longest


# ==================================================================
# TOOL 1: VORLESUNG LOGGEN
# ==================================================================
@mcp.tool()
def log_lecture(module: str, topic_name: str, understanding_score: int,
                details: Optional[str] = None) -> str:
    """
    Loggt eine neue Vorlesung/ein Thema und plant die erste Wiederholung
    nach der Ebbinghaus-Vergessenskurve. Schreibt in Obsidian (Frontmatter
    + optional Notiz) UND in den SQLite-Index.

    module: z.B. "Algebra"
    topic_name: z.B. "Lineare Funktionen"
    understanding_score: 0-5 (Selbsteinschaetzung)
    details: optionale Notiz, wird unter "## 📝 Notizen" angehaengt
    """
    err = _vault_ok()
    if err:
        return err

    score = max(0, min(5, int(understanding_score)))
    today = date.today()
    interval = INITIAL_INTERVALS[score]
    next_review = (today + timedelta(days=interval)).isoformat()

    path = _note_path(module, topic_name)
    post = _read_or_new_note(path, module, topic_name)
    _sync_frontmatter(post, module, topic_name, score, 2.5, interval, 1 if score >= 3 else 0,
                      next_review, today.isoformat())

    if details:
        body = post.content
        marker = "## 📝 Notizen"
        stamp = f"\n- [{today.isoformat()}] {details}"
        if marker in body:
            body = body.replace(marker, marker + stamp, 1)
        else:
            body += f"\n\n{marker}{stamp}\n"
        post.content = body

    _write_note(path, post)
    rel = str(path.relative_to(VAULT_PATH))
    _upsert_sqlite(topic_name, module, rel, score, 2.5,
                   interval, 1 if score >= 3 else 0, next_review, today.isoformat())
    _touch_streak()

    note_line = f"\n- **Notiz:** {details} ⚠️" if details else ""
    return (
        f"Eingetragen ✅\n"
        f"- **Thema:** {topic_name}\n"
        f"- **Modul:** {module}\n"
        f"- **Verständnis-Score:** {score}/5{note_line}\n"
        f"- **Nächste Wiederholung:** {next_review} (in {interval} Tagen)\n"
        f"- **Obsidian:** {rel}"
    )


# ==================================================================
# TOOL 2: LEARNING-QUEUE
# ==================================================================
@mcp.tool()
def get_learning_queue() -> str:
    """
    Zeigt alle faelligen Wiederholungen (next_review <= heute), sortiert nach
    Dringlichkeit. Liest aus dem schnellen SQLite-Index.
    """
    today = date.today().isoformat()
    conn = _db()
    rows = conn.execute(
        "SELECT * FROM topics WHERE next_review <= ? ORDER BY next_review ASC, understanding_score ASC",
        (today,)
    ).fetchall()
    conn.close()

    if not rows:
        return "🎉 Deine Lern-Queue ist leer – aktuell nichts fällig."

    out = [f"📋 **Fällige Wiederholungen** ({len(rows)}):\n"]
    for r in rows:
        overdue = (date.fromisoformat(today) - date.fromisoformat(r["next_review"])).days
        flag = f" ⚠️ {overdue} Tage überfällig" if overdue > 0 else " (heute)"
        out.append(f"- **{r['topic_name']}** ({r['module']}) – Score {r['understanding_score']}/5{flag}")
    return "\n".join(out)


# ==================================================================
# TOOL 3: THEMA WIEDERHOLEN (Review)
# ==================================================================
@mcp.tool()
def review_topic(module: str, topic_name: str, quality_score: int,
                 details: Optional[str] = None) -> str:
    """
    Bewertet eine durchgefuehrte Wiederholung (quality_score 0-5) und berechnet
    via SM-2 das naechste Intervall. Aktualisiert Frontmatter + SQLite + Streak.
    """
    err = _vault_ok()
    if err:
        return err

    conn = _db()
    row = conn.execute(
        "SELECT * FROM topics WHERE module=? AND topic_name=?", (module, topic_name)
    ).fetchone()
    conn.close()

    if not row:
        return (f"❓ Kein Thema '{topic_name}' im Modul '{module}' gefunden. "
                f"Logge es zuerst mit log_lecture.")

    interval, ease, reps = _sm2(quality_score, row["ease_factor"],
                                row["interval"], row["repetitions"])
    today = date.today()
    next_review = (today + timedelta(days=interval)).isoformat()

    path = _note_path(module, topic_name)
    post = _read_or_new_note(path, module, topic_name)
    _sync_frontmatter(post, module, topic_name, quality_score, ease, interval, reps,
                      next_review, today.isoformat())
    if details:
        body = post.content
        marker = "## 📝 Notizen"
        stamp = f"\n- [{today.isoformat()}] (Review) {details}"
        body = body.replace(marker, marker + stamp, 1) if marker in body else body + f"\n\n{marker}{stamp}\n"
        post.content = body
    _write_note(path, post)

    rel = str(path.relative_to(VAULT_PATH))
    _upsert_sqlite(topic_name, module, rel, quality_score, ease, interval, reps,
                   next_review, today.isoformat())
    cur, longest = _touch_streak()

    return (
        f"Wiederholung bewertet ✅\n"
        f"- **Thema:** {topic_name} ({module})\n"
        f"- **Qualität:** {quality_score}/5\n"
        f"- **Neues Intervall:** {interval} Tage (Ease {ease})\n"
        f"- **Nächste Wiederholung:** {next_review}\n"
        f"- **Streak:** {cur} Tage (Rekord: {longest})"
    )


# ==================================================================
# TOOL 4: KALENDER-OPTIMIERUNG (SBB-aware)
# ==================================================================
def _mins_to_hhmm(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def _sbb_travel_minutes(frm: str, to: str) -> int:
    if not _HAS_REQUESTS or not frm or not to:
        return SBB_FALLBACK_MIN
    try:
        r = requests.get(f"{SBB_API_BASE}/connections",
                         params={"from": frm, "to": to, "limit": 1}, timeout=6)
        data = r.json()
        dur = data["connections"][0]["duration"]  # Format "00d01:23:00"
        m = re.search(r"(\d+)d(\d+):(\d+):", dur)
        if m:
            days, hh, mm = int(m.group(1)), int(m.group(2)), int(m.group(3))
            return days * 1440 + hh * 60 + mm
    except Exception:
        pass
    return SBB_FALLBACK_MIN


@mcp.tool()
def optimize_study_slots(raw_calendar_events: List[str], current_energy_level: str,
                         from_station: Optional[str] = None,
                         to_station: Optional[str] = None) -> str:
    """
    Berechnet freie Lern-Slots, zieht Reise-/Essenspuffer ab und schuetzt vor
    kognitiver Ueberlast (Burnout). Wenn from_station/to_station gesetzt sind,
    wird die echte SBB-Reisezeit als Puffer verwendet (sonst Fallback-Minuten).

    raw_calendar_events: ["08:00-10:00 Vorlesung Math", "12:00-13:00 Gym"]
    current_energy_level: 'high' oder 'low'
    """
    travel = _sbb_travel_minutes(from_station, to_station)
    puffer_str = (f"🚆 {travel} Min Reisezeit ({from_station}→{to_station})"
                  if from_station and to_station else f"{travel} Min Puffer")

    estimated_load = len(raw_calendar_events) * 60
    today_str = date.today().isoformat()
    conn = _db()
    conn.execute("INSERT OR REPLACE INTO cognitive_load (date, load_points) VALUES (?, ?)",
                 (today_str, estimated_load))
    conn.commit()
    conn.close()

    if estimated_load >= 300:
        return (f"⚠️ BURNOUT-WARNUNG: Kognitive Tagesbelastung {estimated_load} Punkte. "
                f"Intensive Lernphasen heute gesperrt.")

    out = [f"Kognitive Tageslast: {estimated_load}/300 (grün).",
           f"Chronotyp-Phase: '{current_energy_level.upper()}'  |  {puffer_str}"]

    if current_energy_level.lower() == "high":
        s1 = 14 * 60 + travel
        s2 = 17 * 60 + travel
        out.append("\nEmpfohlene Slots:")
        out.append(f"-> {_mins_to_hhmm(s1)} - {_mins_to_hhmm(s1 + 75)} (75 Min) – komplexe, fällige Themen (Score 0-2).")
        out.append(f"-> {_mins_to_hhmm(s2)} - {_mins_to_hhmm(s2 + 45)} (45 Min) – schnelle Quizzes / Repetition.")
    else:
        s = 15 * 60 + travel
        out.append("\nEmpfohlene Slots – Energiesparmodus:")
        out.append(f"-> {_mins_to_hhmm(s)} - {_mins_to_hhmm(s + 45)} (45 Min) – leichte Repetition / Karteikarten.")
    return "\n".join(out)


# ==================================================================
# TOOL 5: SBB-VERBINDUNG
# ==================================================================
@mcp.tool()
def get_sbb_connection(from_station: str, to_station: str) -> str:
    """Fragt die SBB OpenData API ab und gibt die naechsten Verbindungen zurueck."""
    if not _HAS_REQUESTS:
        return "⚠️ 'requests' ist nicht installiert – SBB-Abfrage nicht möglich."
    try:
        r = requests.get(f"{SBB_API_BASE}/connections",
                         params={"from": from_station, "to": to_station, "limit": 3}, timeout=6)
        conns = r.json().get("connections", [])
        if not conns:
            return f"Keine Verbindungen {from_station} → {to_station} gefunden."
        out = [f"🚆 **{from_station} → {to_station}**:"]
        for c in conns:
            dep = c["from"]["departure"][11:16]
            arr = c["to"]["arrival"][11:16]
            out.append(f"- {dep} → {arr}")
        return "\n".join(out)
    except Exception as e:
        return f"⚠️ SBB-Abfrage fehlgeschlagen: {e}"


# ==================================================================
# TOOL 6: STREAK
# ==================================================================
@mcp.tool()
def get_streak() -> str:
    """Zeigt den aktuellen und laengsten Lern-Streak."""
    conn = _db()
    row = conn.execute("SELECT current_streak, longest_streak, last_active FROM streak WHERE id=1").fetchone()
    conn.close()
    return (f"🔥 Aktueller Streak: {row['current_streak']} Tage\n"
            f"🏆 Rekord: {row['longest_streak']} Tage\n"
            f"📅 Zuletzt aktiv: {row['last_active'] or '–'}")


# ==================================================================
# TOOL 7: RESYNC – Vault ist die Wahrheit
# ==================================================================
@mcp.tool()
def resync_index() -> str:
    """
    Liest ALLE Lernthemen-Notizen aus dem Vault und baut den SQLite-Index neu auf.
    Nuetzlich, wenn du Notizen manuell in Obsidian bearbeitet hast.
    Obsidian = Source of Truth, SQLite wird daraus regeneriert.
    """
    err = _vault_ok()
    if err:
        return err

    base = Path(VAULT_PATH) / LERNEN_DIR
    if not base.is_dir():
        return f"⚠️ Lernen-Ordner nicht gefunden: {base}"

    conn = _db()
    conn.execute("DELETE FROM topics")
    count = 0
    for md in base.rglob("*.md"):
        try:
            post = frontmatter.load(str(md))
        except Exception:
            continue

        # Modul aus Ordnername, Topic aus Frontmatter oder Dateiname
        module = post.get("module", md.parent.name)
        topic  = post.get("topic", md.stem)

        # Fehlende Frontmatter-Felder mit Defaults ergaenzen und zurueckschreiben
        changed = False
        defaults = {
            "type": "lernthema",
            "module": module,
            "topic": topic,
            "understanding_score": 3,
            "ease_factor": 2.5,
            "interval": 1,
            "repetitions": 0,
            "next_review": date.today().isoformat(),
            "last_reviewed": "",
        }
        for key, val in defaults.items():
            if key not in post:
                post[key] = val
                changed = True
        if changed:
            _write_note(md, post)

        rel = str(md.relative_to(VAULT_PATH))
        conn.execute("""
            INSERT OR REPLACE INTO topics
              (topic_name, module, vault_path, understanding_score, ease_factor,
               interval, repetitions, next_review, last_reviewed)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (topic, module, rel,
              post.get("understanding_score", 3), post.get("ease_factor", 2.5),
              post.get("interval", 1), post.get("repetitions", 0),
              str(post.get("next_review", date.today().isoformat())),
              str(post.get("last_reviewed", ""))))
        count += 1
    conn.commit()
    conn.close()
    return f"♻️ Index neu aufgebaut – {count} Lernthemen aus dem Vault eingelesen."


# ------------------------------------------------------------------
if __name__ == "__main__":
    init_db()
    mcp.run()
