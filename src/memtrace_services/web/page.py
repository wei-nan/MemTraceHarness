"""The status page: a small HTML shell plus static CSS and JavaScript (see `static/`).

The server fills the shell's `__INITIAL_JSON__` and serves `/static/app.css` and `/static/app.js`. All
interactivity runs against data already pushed over SSE: switch project and tab, filter by name, search
recent conversations. The write actions it offers (models, approve/reject, preferences) are POSTed to
this service, which forwards each to the Harness that owns the project (see server.py) — the page never
talks to a Harness itself.
"""

from __future__ import annotations

from pathlib import Path

STATIC_DIR = Path(__file__).with_name("static")
STATIC_TYPES = {"app.css": "text/css; charset=utf-8", "app.js": "text/javascript; charset=utf-8"}

PAGE_TEMPLATE = (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def render_page(initial_json: str) -> str:
    # "</" would end the inline <script> early if it appears inside a string in the data.
    return PAGE_TEMPLATE.replace("__INITIAL_JSON__", initial_json.replace("</", "<\\/"))
