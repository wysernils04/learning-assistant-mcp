# Learning Assistant MCP Server

A personal spaced-repetition learning assistant that runs as an [MCP](https://modelcontextprotocol.io/) server. It schedules study sessions using the SM-2 algorithm, syncs scheduling state to your Obsidian vault's YAML frontmatter, and is aware of your SBB commute times to help you find optimal study slots.

## How it works

- **Obsidian vault** is the source of truth — each topic is an Obsidian note; scheduling metadata lives in the YAML frontmatter.
- **SQLite** acts as a fast query cache and stores streak/cognitive-load state that has no per-note equivalent.
- Every write operation (`log_lecture`, `review_topic`) updates both the note frontmatter and the SQLite row atomically.

## Quick start

1. **Clone and install**
   ```bash
   git clone https://github.com/wysernils04/learning-assistant-mcp.git
   cd learning-assistant-mcp
   python -m venv .venv
   source .venv/bin/activate        # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. **Add the server to Claude Desktop**

   Open `claude_desktop_config.json` in a text editor:
   - **Mac:** `~/Library/Application Support/Claude/claude_desktop_config.json`
   - **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

   Add the following block (replace the paths with your own):
   ```json
   {
     "mcpServers": {
       "learning-assistant": {
         "command": "/absolute/path/to/learning-assistant-mcp/.venv/bin/python",
         "args": ["/absolute/path/to/learning-assistant-mcp/learning_assistant_v3.py"],
         "env": {
           "OBSIDIAN_VAULT_PATH": "/absolute/path/to/your/obsidian/vault"
         }
       }
     }
   }
   ```
   > The `env` block is all you need — no `.env` file required for Claude Desktop.

3. **Restart Claude Desktop** — the config is only read on startup.

4. **Verify** — open a new conversation and ask:
   > *"What learning tools do you have access to?"*

   Claude should list all 7 tools (`log_lecture`, `review_topic`, `get_learning_queue`, `optimize_study_slots`, `get_sbb_connection`, `get_streak`, `resync_index`). If it doesn't, double-check the file paths in the config and restart again.

---

## Tools

| Tool | Description |
|---|---|
| `log_lecture` | Record a newly studied topic with an initial understanding score (0–5). Creates the Obsidian note if it doesn't exist. |
| `review_topic` | Log a review session and update the SM-2 interval, ease factor, and next-due date. |
| `get_learning_queue` | Return topics due for review today, sorted by priority. |
| `optimize_study_slots` | Given a list of calendar events and current energy level, suggest the best study windows — factoring in SBB travel times. |
| `get_sbb_connection` | Look up the next SBB connection between two stations. |
| `get_streak` | Return the current study streak and daily load summary. |
| `resync_index` | Rebuild the SQLite index from all notes in the vault. Use this if you edited notes manually in Obsidian or migrated existing notes. |

## Configuration reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `OBSIDIAN_VAULT_PATH` | Yes | — | Absolute path to your Obsidian vault root |
| `OBSIDIAN_LERNEN_DIR` | No | `📚 Lernen` | Subfolder inside the vault for learning notes |
| `LEARNING_DB_PATH` | No | `<vault>/.learning_index.db` | Path for the SQLite cache |
| `SBB_API_BASE` | No | `https://transport.opendata.ch/v1` | SBB transport API base URL |
| `SBB_TRAVEL_FALLBACK_MIN` | No | `30` | Fallback travel time in minutes if the API is unreachable |

Set these in the `env` block of `claude_desktop_config.json` (as shown in the quick start), or in a `.env` file next to the script if you want to run it directly from the command line.

## Vault structure

The server creates and manages notes automatically — no manual folder setup required. The layout is:

```
<vault>/
└── 📚 Lernen/          ← OBSIDIAN_LERNEN_DIR
    └── <Module>/       ← one folder per module (e.g. "Algebra")
        └── <topic>.md  ← one note per topic (slugified filename)
```

Each note gets the following YAML frontmatter written and kept up to date:

```yaml
---
type: lernthema
module: Algebra
topic: Lineare Funktionen
understanding_score: 4
ease_factor: 2.5
interval: 4
repetitions: 1
next_review: "2026-06-17"
last_reviewed: "2026-06-13"
---
```

If you have **existing notes** you want to import, add `type: lernthema` to their frontmatter and run `resync_index` — the server will pick them up and fill in any missing fields with sensible defaults.

## Usage with Claude Desktop

### Scope it to a project

Rather than enabling this server globally, add it to a specific Claude Desktop **project** (e.g. "Studies"). That way the tools are only active when you're in that context and won't clutter other conversations.

### System prompt

Add this to your project's system prompt so Claude uses the tools naturally without being asked:

```
You have access to a personal learning assistant (MCP server).
- When I tell you I finished a lecture or studied a topic, call log_lecture.
- When I say I reviewed or practiced something, call review_topic.
- When I ask what to study, call get_learning_queue.
- When I share my schedule for the day, call optimize_study_slots. Pass events as
  ["HH:MM-HH:MM description", ...] and ask for my energy level if I haven't mentioned it.
Always confirm the module and topic name before logging.
```

### Memory keys

Tell Claude the following once (or put them in the system prompt) so it can fill in tool parameters without asking every time:

| Key | Example | Used by |
|---|---|---|
| Your modules | `"My modules are: Algebra, Analysis, Physics"` | `log_lecture`, `review_topic` |
| Home station | `"My home station is Zurich HB"` | `optimize_study_slots`, `get_sbb_connection` |
| School/work station | `"My school station is Bern"` | `optimize_study_slots`, `get_sbb_connection` |
| Chronotype | `"I'm a morning person"` / `"I have high energy in the afternoon"` | `optimize_study_slots` |
| Vault subfolder | `"My learning folder is called 📚 Lernen"` | all vault tools (if you changed the default) |

## SM-2 Scheduling

Understanding scores and quality scores both use a 0–5 scale:

- **0–2** — Poor recall; interval resets, ease factor decreases.
- **3** — Marginal; interval stays short.
- **4–5** — Good/perfect recall; interval and ease factor increase.

Initial intervals by understanding score: `{0: 1d, 1: 1d, 2: 2d, 3: 2d, 4: 4d, 5: 6d}`.
