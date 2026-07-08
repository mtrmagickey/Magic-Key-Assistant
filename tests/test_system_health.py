"""
Systemic health check  --  HARD MODE scored evaluation.

Tight latency budgets (5–16x tighter than v1), structural HTML validation,
type-checked API responses, cross-consistency, and throughput stress.

Run:
    pytest tests/test_system_health.py -v -s              # full report
    pytest tests/test_system_health.py -k stress          # just throughput
    pytest tests/test_system_health.py -k cross_check     # just consistency
"""

import importlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

ROOT_DIR = Path(__file__).parent.parent
LEISURELLM_DIR = ROOT_DIR / "LeisureLLM"
sys.path.insert(0, str(LEISURELLM_DIR))
sys.path.insert(0, str(ROOT_DIR))

os.environ["ADMIN_AUTH_DISABLED"] = "1"
os.environ.setdefault("DISCORD_TOKEN", "test-discord-token")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-openai-key")
os.environ.setdefault("TAVILY_API_KEY", "tvly-test-key")
os.environ.setdefault("SERVER_ID", "123456789")


# ═════════════════════════════════════════════════════════════════════════════
#  SCORING ENGINE
# ═════════════════════════════════════════════════════════════════════════════

_SCORES: list[dict[str, Any]] = []

# HARD MODE latency budgets  --  TestClient is in-process, no excuse for slow
_LATENCY_BUDGET = {
    "page": 30,         # was 500ms  --  16x tighter
    "api": 30,          # was 400ms  --  13x tighter
    "import": 15,       # was 50ms   --  3x tighter
    "seed": 1000,       # was 2000ms
    "onboarding": 100,  # was 800ms  --  8x tighter
}


def _latency_score(elapsed_ms: float, budget_ms: float) -> int:
    """Score 0–100 on a curve:  ≤25% budget>100, =budget>70, 2x>40, 4x>0."""
    if budget_ms <= 0:
        return 100
    ratio = elapsed_ms / budget_ms
    if ratio <= 0.25:
        return 100
    if ratio <= 1.0:
        return round(100 - 30 * ((ratio - 0.25) / 0.75))
    if ratio <= 2.0:
        return round(70 - 30 * ((ratio - 1.0) / 1.0))
    if ratio <= 4.0:
        return round(40 - 40 * ((ratio - 2.0) / 2.0))
    return 0


def _completeness_score(present: int, expected: int) -> int:
    if expected == 0:
        return 100
    return round(100 * min(present, expected) / expected)


def _record(category: str, name: str, score: int, elapsed_ms: float = 0,
            detail: str = "", criteria: str = ""):
    _SCORES.append({
        "category": category, "test": name,
        "score": max(0, min(100, score)),
        "ms": round(elapsed_ms, 2),
        "detail": detail, "criteria": criteria,
    })


class _Timer:
    def __enter__(self):
        self.start = time.perf_counter()
        self.elapsed_ms = 0
        return self
    def __exit__(self, *_):
        self.elapsed_ms = (time.perf_counter() - self.start) * 1000


# ── Quality helpers ──────────────────────────────────────────────────────────

# Jinja template syntax that should NEVER survive into rendered output
_TEMPLATE_LEAK_RE = re.compile(r'\{\{[^}]+\}\}|\{%[^%]+%\}')
# Python errors in rendered HTML
_RENDER_ERROR_RE = re.compile(
    r'Traceback \(most recent call last\)|TemplateSyntaxError|UndefinedError'
    r'|jinja2\.exceptions|AttributeError.*MagicMock',
    re.IGNORECASE,
)


def _html_quality(html: str) -> dict[str, bool]:
    """Assess 7 quality signals in rendered HTML."""
    return {
        "has_title": bool(re.search(r'<title[^>]*>.+?</title>', html, re.S)),
        "has_viewport": 'name="viewport"' in html,
        "no_template_leaks": not _TEMPLATE_LEAK_RE.search(html),
        "no_render_errors": not _RENDER_ERROR_RE.search(html),
        "has_nav": 'id="sidebar"' in html or 'nav-item' in html,
        "has_lucide_init": 'createIcons' in html,
        "size_ok": len(html.encode("utf-8")) > 2048,
    }


def _tag_balance(html: str) -> dict[str, int]:
    """Count open/close imbalance for key HTML tags."""
    result = {}
    for tag in ("div", "span", "form", "table", "section", "ul", "li"):
        opens = len(re.findall(rf'<{tag}[\s>]', html, re.I))
        closes = len(re.findall(rf'</{tag}\s*>', html, re.I))
        result[tag] = abs(opens - closes)
    return result


# ── Shared DB mock + TestClient ──────────────────────────────────────────────

def _mock_db():
    db = MagicMock()
    conn = MagicMock()

    class FakeRow(dict):
        def __getattr__(self, name):
            try:
                return self[name]
            except KeyError:
                raise AttributeError(name)

    cursor = MagicMock()
    cursor.fetchone = AsyncMock(return_value=(0,))
    cursor.fetchall = AsyncMock(return_value=[])
    cursor.__aiter__ = MagicMock(return_value=iter([]))

    class _CursorCM:
        """Supports both `await conn.execute(...)` and `async with conn.execute(...):`."""
        def __await__(self):
            yield
            return cursor
        async def __aenter__(self):
            return cursor
        async def __aexit__(self, *args):
            pass

    conn.execute = MagicMock(side_effect=lambda *a, **kw: _CursorCM())
    conn.executemany = AsyncMock()
    conn.commit = AsyncMock()

    class AcquireCM:
        async def __aenter__(self):
            return conn
        async def __aexit__(self, *args):
            pass

    db.acquire = lambda: AcquireCM()
    return db


@pytest.fixture(scope="module", autouse=True)
def _suppress_llamacpp_health():
    """LlamaCppManager._health_check does urlopen with a 3-second timeout
    against a server that isn't running, causing a ~2 s Windows TCP timeout
    on every dashboard render.  Suppress it module-wide."""
    with patch("services.llamacpp_manager.LlamaCppManager._health_check",
               return_value=False):
        yield


@pytest.fixture(scope="module")
def client():
    from admin import dependencies, server

    server.app.router.on_startup.clear()
    server.app.router.on_shutdown.clear()

    mock_mr = MagicMock()
    mock_mr.backends = {}
    mock_mr.pipeline = None
    mock_mr.clients = {}
    mock_mr.close = AsyncMock()
    dependencies._model_router = mock_mr

    mock_bot = MagicMock()
    mock_bot.db = _mock_db()
    dependencies._bot_instance = mock_bot

    with TestClient(server.app, raise_server_exceptions=False) as c:
        # Warmup: pre-compile Jinja templates so first-render latency
        # doesn't pollute every scored test that touches these routes.
        with patch("admin.server.is_first_run", return_value=False), \
             patch("services.system_tools.SystemTools.get_ollama_status",
                   return_value={"installed": False, "running": False, "models": []}), \
             patch("admin.routers.settings._build_onboarding_state",
                   AsyncMock(return_value={"provider_detected": True,
                                           "provider_connected": True,
                                           "phase1_saved": True})):
            c.get("/dashboard")
            c.get("/router")
            c.get("/setup")
        os.environ["ADMIN_AUTH_DISABLED"] = "0"
        c.get("/login")
        os.environ["ADMIN_AUTH_DISABLED"] = "1"
        yield c


# =============================================================================
#  1 · PAGE ROUTES  --  11 quality signals + latency (budget: 30 ms)
# =============================================================================

_SIMPLE_PAGES = [
    "/actions", "/leads", "/meetings", "/analytics",
    "/obligations", "/feedback",
    "/kb-search", "/knowledge", "/gaps", "/teach",
    "/settings", "/org",
    "/retrieval-log",
    "/inbox", "/chat", "/activity", "/jobs", "/explorer", "/guide",
]


class TestPageRoutes:
    """Each page scored on 11 binary quality checks (60 pts) + latency curve (40 pts).
    Budget dropped from 500 ms > 30 ms.  You earn 100 only if the page renders
    fast AND has proper HTML structure, title, viewport, nav, icons, no leaks."""

    @pytest.mark.parametrize("path", _SIMPLE_PAGES)
    def test_page_scored(self, client, path):
        with _Timer() as t:
            resp = client.get(path)

        html = resp.text if resp.status_code == 200 else ""
        got_200 = resp.status_code == 200
        is_html = "text/html" in resp.headers.get("content-type", "")
        has_doctype = html.strip()[:15].lower().startswith("<!doctype") if html else False
        has_body = "</body>" in html
        q = _html_quality(html) if html else {k: False for k in _html_quality("")}

        # 60 pts quality  ·  40 pts latency
        checks = [
            ("status_200",        got_200,                    8),
            ("content_type",      is_html,                    4),
            ("doctype",           has_doctype,                3),
            ("body_close",        has_body,                   3),
            ("title_tag",         q["has_title"],             6),
            ("viewport_meta",     q["has_viewport"],          5),
            ("size_gt_2kb",       q["size_ok"],               8),
            ("no_jinja_leaks",    q["no_template_leaks"],     8),
            ("no_render_errors",  q["no_render_errors"],      5),
            ("nav_present",       q["has_nav"],               5),
            ("lucide_init",       q["has_lucide_init"],       5),
        ]

        pts = sum(w for _, ok, w in checks if ok)
        lat = round(_latency_score(t.elapsed_ms, _LATENCY_BUDGET["page"]) * 0.40)
        pts += lat

        flags = [f"{n}={'OK' if ok else 'X'}" for n, ok, _ in checks]
        flags.append(f"latency={t.elapsed_ms:.1f}ms({lat}pts)")
        _record("page", path, pts, t.elapsed_ms, ", ".join(flags),
                "60 quality (11 checks) + 40 latency @30ms budget")
        assert got_200, f"{path} > {resp.status_code}"

    # ── Special pages that need patches ──────────────────────────────────

    def test_dashboard_renders(self, client):
        from contextlib import ExitStack
        with ExitStack() as stack:
            stack.enter_context(patch("admin.server.is_first_run", return_value=False))
            stack.enter_context(patch("services.system_tools.SystemTools.get_ollama_status",
                   return_value={"installed": False, "running": False, "models": []}))
            stack.enter_context(patch("admin.routers.settings._build_onboarding_state",
                   AsyncMock(return_value={"setup_complete": False, "phase1_saved": False,
                                           "provider_detected": False, "provider_connected": False})))
            with _Timer() as t:
                resp = client.get("/dashboard")
        html = resp.text
        q = _html_quality(html)

        checks = [
            ("status_200",       resp.status_code == 200,           8),
            ("body_close",       "</body>" in html,                 3),
            ("title_tag",        q["has_title"],                    6),
            ("size_gt_2kb",      q["size_ok"],                      8),
            ("no_jinja_leaks",   q["no_template_leaks"],            8),
            ("no_render_errors", q["no_render_errors"],             5),
            ("nav_present",      q["has_nav"],                      5),
            ("lucide_init",      q["has_lucide_init"],              5),
            ("install_btn",      'id="installBtn"' in html,         4),
            ("model_banner",     'id="modelSetupBanner"' in html,   4),
        ]
        pts = sum(w for _, ok, w in checks if ok)
        pts += round(_latency_score(t.elapsed_ms, _LATENCY_BUDGET["page"]) * 0.44)

        flags = [f"{n}={'OK' if ok else 'X'}" for n, ok, _ in checks]
        _record("page", "/dashboard", min(pts, 100), t.elapsed_ms, ", ".join(flags))
        assert resp.status_code == 200

    def test_setup_page_renders(self, client):
        with patch("admin.routers.settings._build_onboarding_state",
                   AsyncMock(return_value={"setup_complete": False, "phase1_saved": False,
                                           "provider_detected": False, "provider_connected": False})):
            with _Timer() as t:
                resp = client.get("/setup")
        html = resp.text
        q = _html_quality(html)
        is_html = "text/html" in resp.headers.get("content-type", "")

        checks = [
            ("status_200",       resp.status_code == 200,           8),
            ("content_type",     is_html,                          4),
            ("body_close",       "</body>" in html,                 3),
            ("title_tag",        q["has_title"],                    6),
            ("viewport_meta",    q["has_viewport"],                 5),
            ("size_gt_2kb",      q["size_ok"],                      6),
            ("no_jinja_leaks",   q["no_template_leaks"],            6),
            ("no_render_errors", q["no_render_errors"],             5),
            ("tagline",          "Private AI Operations Assistant" in html, 5),
            ("step_nav",         "goToStep" in html,                4),
            ("ollama_section",   'id="ollamaStatus"' in html,       4),
            ("lucide_init",      q["has_lucide_init"],              4),
        ]
        pts = sum(w for _, ok, w in checks if ok)
        pts += round(_latency_score(t.elapsed_ms, _LATENCY_BUDGET["page"]) * 0.40)
        flags = [f"{n}={'OK' if ok else 'X'}" for n, ok, _ in checks]
        _record("page", "/setup", min(pts, 100), t.elapsed_ms, ", ".join(flags))
        assert resp.status_code == 200

    def test_login_page_renders(self, client):
        os.environ["ADMIN_AUTH_DISABLED"] = "0"
        with _Timer() as t:
            resp = client.get("/login")
        os.environ["ADMIN_AUTH_DISABLED"] = "1"
        html = resp.text
        q = _html_quality(html)
        is_html = "text/html" in resp.headers.get("content-type", "")

        checks = [
            ("status_200",       resp.status_code == 200,           8),
            ("content_type",     is_html,                          4),
            ("body_close",       "</body>" in html,                 3),
            ("title_tag",        q["has_title"],                    6),
            ("size_gt_2kb",      q["size_ok"],                      6),
            ("no_jinja_leaks",   q["no_template_leaks"],            6),
            ("no_render_errors", q["no_render_errors"],             5),
            ("tagline",          "Private AI Operations Assistant" in html, 5),
            ("password_field",   'type="password"' in html,         5),
            ("toggle_vis",       "togglePasswordVisibility" in html, 4),
            ("responsive_grid",  "login-split" in html,             4),
            ("lucide_icons",     "data-lucide" in html,             4),
        ]
        pts = sum(w for _, ok, w in checks if ok)
        pts += round(_latency_score(t.elapsed_ms, _LATENCY_BUDGET["page"]) * 0.40)
        flags = [f"{n}={'OK' if ok else 'X'}" for n, ok, _ in checks]
        _record("page", "/login", min(pts, 100), t.elapsed_ms, ", ".join(flags))
        assert resp.status_code == 200

    def test_router_page_renders(self, client):
        with patch("admin.routers.settings._build_onboarding_state",
                   AsyncMock(return_value={"provider_detected": True,
                                           "provider_connected": True,
                                           "phase1_saved": True})):
            with patch("services.system_tools.SystemTools.get_ollama_status",
                       return_value={"installed": False, "running": False, "models": []}):
                with _Timer() as t:
                    resp = client.get("/router")
        html = resp.text
        q = _html_quality(html)
        is_html = "text/html" in resp.headers.get("content-type", "")
        has_doctype = html.strip()[:15].lower().startswith("<!doctype") if html else False

        checks = [
            ("status_200",       resp.status_code == 200,           8),
            ("content_type",     is_html,                          4),
            ("doctype",          has_doctype,                      3),
            ("body_close",       "</body>" in html,                 3),
            ("title_tag",        q["has_title"],                    6),
            ("viewport_meta",    q["has_viewport"],                 5),
            ("size_gt_2kb",      q["size_ok"],                      6),
            ("no_jinja_leaks",   q["no_template_leaks"],            6),
            ("no_render_errors", q["no_render_errors"],             5),
            ("nav_present",      q["has_nav"],                      5),
            ("lucide_init",      q["has_lucide_init"],              5),
            ("backend_listing",  'ollama' in html.lower() or 'openai' in html.lower(), 4),
        ]
        pts = sum(w for _, ok, w in checks if ok)
        pts += round(_latency_score(t.elapsed_ms, _LATENCY_BUDGET["page"]) * 0.40)
        flags = [f"{n}={'OK' if ok else 'X'}" for n, ok, _ in checks]
        _record("page", "/router", min(pts, 100), t.elapsed_ms, ", ".join(flags))
        assert resp.status_code == 200


# =============================================================================
#  2 · TEMPLATE CONTENT  --  structural depth + tag balance
# =============================================================================

class TestTemplateContent:
    """Score templates on structural HTML quality, tag balance, and design-spec.
    Tag balance catches unclosed divs/spans that the old tests missed entirely."""

    def _score_template(self, html: str, name: str, criteria: dict[str, bool],
                        balance_threshold: int = 5):
        balance = _tag_balance(html)
        total_imbalance = sum(balance.values())
        div_imbalance = balance.get("div", 0)

        criteria["tag_balance_ok"] = total_imbalance <= balance_threshold
        criteria["div_balance_ok"] = div_imbalance <= 3
        criteria["no_jinja_leaks"] = not _TEMPLATE_LEAK_RE.search(html)
        criteria["no_render_errors"] = not _RENDER_ERROR_RE.search(html)
        criteria["size_gt_2kb"] = len(html.encode("utf-8")) > 2048

        passed = sum(1 for v in criteria.values() if v)
        score = _completeness_score(passed, len(criteria))
        detail = ", ".join(f"{k}={'OK' if v else 'X'}" for k, v in criteria.items())
        _record("template", name, score, 0, detail,
                f"{passed}/{len(criteria)}  --  tag imbalance: {total_imbalance} (div: {div_imbalance})")
        return passed, len(criteria)

    def test_login_structural(self, client):
        os.environ["ADMIN_AUTH_DISABLED"] = "0"
        resp = client.get("/login")
        os.environ["ADMIN_AUTH_DISABLED"] = "1"
        passed, total = self._score_template(resp.text, "login_structural", {
            "tagline_updated": "Private AI Operations Assistant" in resp.text,
            "old_tagline_gone": "Local Ops for Tiny Teams" not in resp.text,
            "tagline_muted": "text-gray-400 text-sm" in resp.text,
            "password_padded": "pr-16" in resp.text,
            "visibility_toggle": "togglePasswordVisibility" in resp.text,
            "lucide_icons": "data-lucide" in resp.text,
            "no_eval": "eval(" not in resp.text,
            "responsive_grid": "login-split" in resp.text,
        })
        assert passed >= 9, f"Login structural: {passed}/{total}"

    def test_setup_structural(self, client):
        with patch("admin.routers.settings._build_onboarding_state",
                   AsyncMock(return_value={"setup_complete": False, "phase1_saved": False,
                                           "provider_detected": False, "provider_connected": False})):
            resp = client.get("/setup")
        passed, total = self._score_template(resp.text, "setup_structural", {
            "tagline_updated": "Private AI Operations Assistant" in resp.text,
            "old_tagline_gone": "Local Ops for Tiny Teams" not in resp.text,
            "ollama_status": 'id="ollamaStatus"' in resp.text,
            "install_button": "setupInstallOllama" in resp.text,
            "step_nav": "goToStep" in resp.text,
            "device_scan": "scanDevice" in resp.text or "deviceReport" in resp.text,
            "lucide_icons": "data-lucide" in resp.text,
        })
        assert passed >= 7, f"Setup structural: {passed}/{total}"

    def test_dashboard_structural(self, client):
        from contextlib import ExitStack
        with ExitStack() as stack:
            stack.enter_context(patch("admin.server.is_first_run", return_value=False))
            stack.enter_context(patch("services.system_tools.SystemTools.get_ollama_status",
                   return_value={"installed": False, "running": False, "models": []}))
            resp = client.get("/dashboard")
        passed, total = self._score_template(resp.text, "dashboard_structural", {
            "model_banner": 'id="modelSetupBanner"' in resp.text,
            "silent_errors": "silentErrors" in resp.text,
            "bot_insight": 'id="botInsightCard"' in resp.text,
            "action_tiles": 'id="actionTiles"' in resp.text,
            "nudge_section": 'id="nudgeSection"' in resp.text,
            "urgent_section": 'id="urgentSection"' in resp.text,
            "momentum_line": 'id="momentumLine"' in resp.text,
            "savings_tile": 'id="savingsTile"' in resp.text,
        })
        assert passed >= 9, f"Dashboard structural: {passed}/{total}"

    def test_actions_structural(self, client):
        resp = client.get("/actions")
        passed, total = self._score_template(resp.text, "actions_structural", {
            "cancel_label": "Cancel Task" in resp.text,
            "dot_menu_fixed": "position = 'fixed'" in resp.text,
            "scroll_closes": "_closeDotMenus" in resp.text,
            "edit_button": "editAction" in resp.text,
            "sort_columns": "toggleSort" in resp.text,
            "pagination": "goPage" in resp.text or "pagination" in resp.text,
        })
        assert passed >= 7, f"Actions structural: {passed}/{total}"


# =============================================================================
#  3 · API ENDPOINTS  --  type-validated + content-type + no-HTML (budget: 30 ms)
# =============================================================================

# (path, {key: expected_type})   --  types are actually checked, not just presence
_API_TYPE_CHECKS = [
    ("/api/v1/ollama/status",      {"installed": bool, "running": bool, "models": list}),
    ("/api/v1/actions/stats",      {"success": bool}),
    ("/api/v1/config/sections",    {"success": bool, "sections": list}),
    ("/api/v1/router/pipeline",    {"success": bool}),
    ("/api/v1/router/backends",    {"success": bool}),
    ("/api/v1/router/presets",     {"success": bool}),
    ("/api/v1/kb/stats",           {"success": bool}),
    ("/api/v1/bot/status",         {"online": bool}),
    ("/api/v1/inbox/unread-count", {"count": int}),
    ("/api/v1/prompts",            {}),
    ("/api/v1/secrets/list",       {}),
    ("/api/v1/config/all",         {}),
    ("/api/v1/activity",           {}),
]


class TestAPIEndpoints:
    """7-dimension scoring per endpoint (budget: 30 ms, not 400 ms).
    15 status + 10 content-type + 10 json + 10 no-html + 15 types + 10 size + 30 latency."""

    @pytest.mark.parametrize("path,type_map", _API_TYPE_CHECKS,
                             ids=[p for p, _ in _API_TYPE_CHECKS])
    def test_api_scored(self, client, path, type_map):
        with _Timer() as t:
            resp = client.get(path)

        pts = 0
        flags = []

        # Status (15)
        if resp.status_code == 200:
            pts += 15
            flags.append("status=200")
        else:
            flags.append(f"status={resp.status_code}")

        # Content-Type = application/json (10)
        ct = resp.headers.get("content-type", "")
        if "application/json" in ct:
            pts += 10
            flags.append("ct=json")
        elif resp.status_code == 200:
            flags.append(f"ct_WRONG={ct[:30]}")

        # Valid JSON (10)
        data = None
        if resp.status_code == 200:
            try:
                data = resp.json()
                pts += 10
                flags.append("json=valid")
            except Exception:
                flags.append("json=INVALID")

        # No HTML in JSON response (10)  --  catches template-fallback bugs
        if resp.status_code == 200:
            body = resp.text
            if "<html" not in body.lower() and "<!doctype" not in body.lower():
                pts += 10
                flags.append("no_html=OK")
            else:
                flags.append("no_html=X HTML_IN_JSON!")

        # Type-checked shape (15)  --  values must be the declared type
        if data is not None and type_map:
            type_ok = 0
            for key, expected_type in type_map.items():
                if key in data:
                    if isinstance(expected_type, tuple):
                        if isinstance(data[key], expected_type):
                            type_ok += 1
                        else:
                            flags.append(f"{key}:{type(data[key]).__name__}!={expected_type}")
                    elif isinstance(data[key], expected_type):
                        type_ok += 1
                    else:
                        flags.append(f"{key}:{type(data[key]).__name__}!={expected_type.__name__}")
                else:
                    flags.append(f"{key}:MISSING")
            pts += round(15 * type_ok / len(type_map))
            flags.append(f"types={type_ok}/{len(type_map)}")
        elif data is not None:
            pts += 15

        # Response size > 20 bytes (10)  --  not an empty stub
        if resp.status_code == 200:
            sz = len(resp.content)
            if sz > 20:
                pts += 10
            elif sz > 5:
                pts += 5
            flags.append(f"size={sz}B")

        # Latency (30)  --  budget: 30 ms
        lat = round(_latency_score(t.elapsed_ms, _LATENCY_BUDGET["api"]) * 0.30)
        pts += lat
        flags.append(f"latency={t.elapsed_ms:.1f}ms({lat}pts)")

        _record("api", path, pts, t.elapsed_ms, ", ".join(flags),
                "15 status + 10 ct + 10 json + 10 no-html + 15 types + 10 size + 30 latency")
        assert resp.status_code in (200, 307), f"{path} > {resp.status_code}"

    # ── Paginated list endpoints  --  envelope consistency ──────────────────

    _PAGINATED = [
        ("/api/v1/actions?page=1&per_page=5",     "actions",     ["success", "page", "per_page", "total", "total_pages"]),
        ("/api/v1/leads?page=1&per_page=5",       "leads",       ["success", "page", "per_page", "total", "total_pages"]),
        ("/api/v1/decisions?page=1&per_page=5",    "decisions",   ["success", "page", "total"]),
        ("/api/v1/obligations?page=1&per_page=5",  "obligations", ["success", "page", "total"]),
        ("/api/v1/meetings?page=1&per_page=5",     "meetings",    ["success", "page", "total"]),
        ("/api/v1/feedback?page=1&per_page=5",     None,          []),
        ("/api/v1/gaps/stats",                     None,          ["success"]),
        ("/api/v1/knowledge/stats",                None,          ["success"]),
    ]

    @pytest.mark.parametrize("path,collection_key,required_keys", _PAGINATED,
                             ids=["actions", "leads", "decisions", "obligations",
                                  "meetings", "feedback", "gap_stats", "kb_stats"])
    def test_paginated_api(self, client, path, collection_key, required_keys):
        with _Timer() as t:
            resp = client.get(path)

        data = resp.json() if resp.status_code == 200 else {}
        pts = 0
        flags = []

        # Status (15)
        if resp.status_code == 200:
            pts += 15

        # Content-type (10)
        if "application/json" in resp.headers.get("content-type", ""):
            pts += 10

        # Collection key present + is a list (15)
        if collection_key and collection_key in data:
            if isinstance(data[collection_key], list):
                pts += 15
                flags.append(f"{collection_key}=list({len(data[collection_key])})")
            else:
                pts += 5
                flags.append(f"{collection_key}=NOT_LIST")
        elif collection_key:
            flags.append(f"{collection_key}=MISSING")
        else:
            pts += 15

        # Required keys (10)
        if required_keys:
            present = sum(1 for k in required_keys if k in data)
            pts += round(10 * present / len(required_keys))
            flags.append(f"keys={present}/{len(required_keys)}")
        else:
            pts += 10

        # Pagination fields are ints (10)
        int_fields = [f for f in ("page", "per_page", "total", "total_pages") if f in data]
        if int_fields:
            type_ok = sum(1 for f in int_fields if isinstance(data[f], int))
            pts += round(10 * type_ok / len(int_fields))
            if type_ok < len(int_fields):
                bad = [f for f in int_fields if not isinstance(data[f], int)]
                flags.append(f"non_int={bad}")
        else:
            pts += 10

        # Latency (30)
        lat = round(_latency_score(t.elapsed_ms, _LATENCY_BUDGET["api"]) * 0.30)
        pts += lat

        _record("api", f"{path.split('?')[0]} (paginated)", pts, t.elapsed_ms,
                ", ".join(flags))
        assert resp.status_code == 200


# =============================================================================
#  4 · ONBOARDING STATE MACHINE  --  latency now counts (budget: 100 ms)
# =============================================================================

class TestOnboardingState:
    """Correctness (60) + latency (40) at 100 ms budget (was 800 ms)."""

    def _check(self, client, name, patches, url, expect_status, expect_in=None):
        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            with _Timer() as t:
                resp = client.get(url, follow_redirects=False)

        pts = 0
        status_ok = resp.status_code == expect_status
        content_ok = True
        if status_ok:
            pts += 40
        if expect_in and resp.status_code == 200:
            content_ok = expect_in in resp.text
            if content_ok:
                pts += 20
        elif status_ok:
            pts += 20
        # Latency now actually scores (40 pts at 100 ms budget)
        pts += round(_latency_score(t.elapsed_ms, _LATENCY_BUDGET["onboarding"]) * 0.40)
        _record("onboarding", name, pts, t.elapsed_ms,
                f"status={resp.status_code}(want {expect_status}), content={content_ok}")
        return status_ok, content_ok

    def test_first_run_redirects_to_setup(self, client):
        ok, _ = self._check(client, "first_run_redirect", [
            patch("admin.server.is_first_run", return_value=True),
            patch("admin.routers.settings._build_onboarding_state",
                  AsyncMock(return_value={"setup_complete": False, "phase1_saved": False})),
        ], "/", 302)
        assert ok

    def test_phase1_saved_skips_setup(self, client):
        ok, _ = self._check(client, "phase1_skips_setup", [
            patch("admin.server.is_first_run", return_value=True),
            patch("admin.routers.settings._build_onboarding_state",
                  AsyncMock(return_value={"setup_complete": False, "phase1_saved": True})),
            patch("services.system_tools.SystemTools.get_ollama_status",
                  return_value={"installed": False, "running": False, "models": []}),
        ], "/", 302)
        assert ok

    def test_setup_redirects_when_usable(self, client):
        ok, _ = self._check(client, "setup_redirects_when_usable", [
            patch("admin.routers.settings._build_onboarding_state",
                  AsyncMock(return_value={"setup_complete": True, "phase1_saved": True})),
        ], "/setup", 302)
        assert ok

    def test_setup_force_bypasses_redirect(self, client):
        ok, _ = self._check(client, "setup_force_bypass", [
            patch("admin.routers.settings._build_onboarding_state",
                  AsyncMock(return_value={"setup_complete": False, "phase1_saved": True,
                                          "provider_detected": True, "provider_connected": True})),
        ], "/setup?force=true", 200)
        assert ok

    def test_model_banner_visible_when_no_provider(self, client):
        ok, cok = self._check(client, "model_banner_visible", [
            patch("admin.server.is_first_run", return_value=False),
            patch("admin.routers.settings._build_onboarding_state",
                  AsyncMock(return_value={"setup_complete": False, "phase1_saved": False,
                                          "provider_detected": False, "provider_connected": False})),
            patch("services.system_tools.SystemTools.get_ollama_status",
                  return_value={"installed": False, "running": False, "models": []}),
        ], "/dashboard", 200, 'id="modelSetupBanner"')
        assert ok and cok

    def test_model_banner_hidden_after_phase1(self, client):
        ok, cok = self._check(client, "model_banner_hidden", [
            patch("admin.server.is_first_run", return_value=False),
            patch("admin.routers.settings._build_onboarding_state",
                  AsyncMock(return_value={"setup_complete": False, "phase1_saved": True,
                                          "provider_detected": True, "provider_connected": True})),
            patch("services.system_tools.SystemTools.get_ollama_status",
                  return_value={"installed": True, "running": True, "models": ["gemma3:4b"]}),
        ], "/dashboard", 200, 'style="display:none;"')
        assert ok and cok


# =============================================================================
#  5 · SERVICE IMPORTS  --  method contracts + docstring (budget: 15 ms)
# =============================================================================

# (module, expected_attrs, {ClassName: [methods_that_must_be_callable]})
_SERVICE_CONTRACTS = [
    ("services.model_router",
     ["ModelRouter", "BackendType", "PipelineRole", "RoleConfig", "PipelineConfig"],
     {"ModelRouter": ["register_backend", "configure_pipeline", "generate_single",
                      "generate_with_fallback", "to_config_dict", "close"]}),
    ("services.bot_config", ["BotConfigManager"], {}),
    ("services.response_cache", ["ResponseCache", "get_response_cache"],
     {"ResponseCache": ["get", "put", "invalidate_all", "stats"]}),
    ("services.token_estimator", [], {}),
    ("services.device_capability", ["DeviceReport", "CapabilityTier"], {}),
    ("services.chat_policy", ["decide_chat_policy"], {}),
    ("services.chat_telemetry", ["ChatRequestTelemetry"], {}),
    ("services.circuit_breaker", ["CircuitBreaker", "CircuitBreakerRegistry"],
     {"CircuitBreaker": ["allow_request", "record_success", "record_failure", "reset", "status"],
      "CircuitBreakerRegistry": ["get_or_create", "all_status", "reset_all"]}),
    ("services.answer_self_assessment", ["assess_answer_quality"], {}),
    ("services.pipeline_presets", [], {}),
    ("services.corpus_health", ["CorpusHealthService"], {}),
    ("services.corpus_interrogator", ["run_strategic_interrogation"], {}),
]


class TestServiceImports:
    """Score = 20 import + 20 API surface + 25 method contracts + 5 docstring + 30 speed.
    Budget dropped from 50 ms > 15 ms.  Method contracts verify that classes
    actually have the methods the rest of the codebase depends on."""

    @pytest.mark.parametrize("module_path,expected_attrs,method_contracts",
                             _SERVICE_CONTRACTS,
                             ids=[m for m, _, _ in _SERVICE_CONTRACTS])
    def test_service_scored(self, module_path, expected_attrs, method_contracts):
        pts = 0
        flags = []

        with _Timer() as t:
            try:
                mod = importlib.import_module(module_path)
                pts += 20
            except Exception as e:
                _record("import", module_path, 0, 0, f"IMPORT ERROR: {e}")
                pytest.fail(f"Failed to import {module_path}: {e}")
                return

        # API surface (20)
        if expected_attrs:
            present = sum(1 for a in expected_attrs if hasattr(mod, a))
            pts += round(20 * present / len(expected_attrs))
            flags.append(f"attrs={present}/{len(expected_attrs)}")
        else:
            pts += 20
            flags.append("attrs=no_check")

        # Method contracts (25)  --  classes must have expected callable methods
        if method_contracts:
            methods_ok = 0
            methods_total = 0
            for cls_name, method_list in method_contracts.items():
                cls = getattr(mod, cls_name, None)
                if cls is None:
                    methods_total += len(method_list)
                    flags.append(f"{cls_name}:MISSING")
                    continue
                for method in method_list:
                    methods_total += 1
                    if hasattr(cls, method):
                        attr = getattr(cls, method)
                        if callable(attr) or isinstance(attr, (property, classmethod, staticmethod)):
                            methods_ok += 1
                        else:
                            flags.append(f"{cls_name}.{method}:not_callable")
                    else:
                        flags.append(f"{cls_name}.{method}:MISSING")
            if methods_total > 0:
                pts += round(25 * methods_ok / methods_total)
                flags.append(f"methods={methods_ok}/{methods_total}")
            else:
                pts += 25
        else:
            pts += 25

        # Module docstring (5)
        if mod.__doc__ and len(mod.__doc__.strip()) > 10:
            pts += 5
            flags.append("docstring=OK")
        else:
            flags.append("docstring=X")

        # Speed (30)  --  budget: 15 ms
        speed_pts = round(_latency_score(t.elapsed_ms, _LATENCY_BUDGET["import"]) * 0.30)
        pts += speed_pts
        flags.append(f"import_ms={t.elapsed_ms:.1f}")

        _record("import", module_path, pts, t.elapsed_ms, ", ".join(flags),
                "20 import + 20 surface + 25 methods + 5 docstring + 30 speed @15ms")
        assert pts >= 40


# =============================================================================
#  6 · MODEL ROUTER  --  enum + alias completeness
# =============================================================================

class TestModelRouter:

    def test_backend_types_completeness(self):
        from services.model_router import BackendType
        known = {"ollama", "openai", "anthropic", "openrouter"}
        actual = {e.value for e in BackendType}
        present = len(known & actual)
        score = _completeness_score(present, len(known))
        _record("model_router", "backend_types", score, 0,
                f"expected={known}, found={actual}")
        assert "ollama" in actual

    def test_pipeline_roles_completeness(self):
        from services.model_router import PipelineRole
        expected = {"initial", "critique", "synthesize"}
        actual = {e.value for e in PipelineRole}
        present = len(expected & actual)
        score = _completeness_score(present, len(expected))
        _record("model_router", "pipeline_roles", score, 0,
                f"expected={expected}, found={actual}")
        assert expected <= actual

    def test_alias_resolution(self):
        try:
            from admin.routers.model_router_api import _normalize_backend_name
        except ImportError:
            _record("model_router", "alias_resolution", 0, 0, "not available")
            pytest.skip("_normalize_backend_name not available")

        aliases = {"local": "ollama", "ollama-local": "ollama", "ollama": "ollama"}
        results = {a: _normalize_backend_name(a) for a in aliases}
        correct = sum(1 for a, e in aliases.items() if results[a] == e)
        score = _completeness_score(correct, len(aliases))
        _record("model_router", "alias_resolution", score, 0, f"aliases={results}")
        assert correct == len(aliases)


# =============================================================================
#  7 · SEED WORKSPACE  --  idempotency + entity coverage
# =============================================================================

class TestSeedWorkspace:

    async def test_seed_scored(self, tmp_path):
        from contextlib import asynccontextmanager

        import aiosqlite

        migrations_dir = LEISURELLM_DIR / "migrations"
        db_path = tmp_path / "test_seed_health.db"
        conn = await aiosqlite.connect(str(db_path))
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        for sql_file in sorted(migrations_dir.glob("*.sqlite.sql")):
            sql = sql_file.read_text(encoding="utf-8")
            for stmt in sql.split(";"):
                lines = [l for l in stmt.strip().splitlines() if not l.strip().startswith("--")]
                clean = "\n".join(lines).strip()
                if clean:
                    try:
                        await conn.execute(clean)
                    except Exception:
                        pass
            await conn.commit()

        for tbl_sql in [
            "CREATE TABLE IF NOT EXISTS leads (id INTEGER PRIMARY KEY, name TEXT, contact_info TEXT, stage TEXT DEFAULT 'prospect', value REAL DEFAULT 0, notes TEXT, created_at TEXT DEFAULT (datetime('now')))",
            "CREATE TABLE IF NOT EXISTS sops (id INTEGER PRIMARY KEY, title TEXT, body TEXT, status TEXT DEFAULT 'active', created_at TEXT DEFAULT (datetime('now')))",
            "CREATE TABLE IF NOT EXISTS guardrails (id INTEGER PRIMARY KEY, name TEXT, condition TEXT, action TEXT DEFAULT 'warn', active INTEGER DEFAULT 1, created_at TEXT DEFAULT (datetime('now')))",
        ]:
            try:
                await conn.execute(tbl_sql)
            except Exception:
                pass
        await conn.commit()

        class FakeDB:
            def __init__(self, connection):
                self.connection = connection
                self.database_path = db_path
            @asynccontextmanager
            async def acquire(self):
                yield self.connection
            async def execute(self, query, *args):
                async with self.acquire() as conn:
                    await conn.execute(query, args[0] if len(args) == 1 and isinstance(args[0], (tuple, list, dict)) else args)
                    await conn.commit()
            async def fetchone(self, query, *args):
                async with self.acquire() as conn:
                    async with conn.execute(query, args[0] if len(args) == 1 and isinstance(args[0], (tuple, list, dict)) else args) as cur:
                        return await cur.fetchone()
            async def fetchall(self, query, *args):
                async with self.acquire() as conn:
                    async with conn.execute(query, args[0] if len(args) == 1 and isinstance(args[0], (tuple, list, dict)) else args) as cur:
                        return await cur.fetchall()

        fake_db = FakeDB(conn)
        from core.seed_workspace import seed_workspace

        pts = 0

        with _Timer() as t:
            try:
                await seed_workspace(fake_db, force=True)
                pts += 20
            except Exception as e:
                _record("seed", "scored", 0, t.elapsed_ms, f"first seed error: {e}")
                await conn.close()
                pytest.fail(f"Seed failed: {e}")

        counts_first: dict[str, int] = {}
        for table in ["tasks", "decisions", "leads", "sops", "guardrails", "obligations"]:
            try:
                cur = await conn.execute(f"SELECT COUNT(*) FROM {table}")
                row = await cur.fetchone()
                counts_first[table] = row[0] if row else 0
            except Exception:
                counts_first[table] = -1

        try:
            await seed_workspace(fake_db, force=True)
            pts += 20
        except Exception:
            pass

        counts_second: dict[str, int] = {}
        for table in counts_first:
            if counts_first[table] < 0:
                continue
            try:
                cur = await conn.execute(f"SELECT COUNT(*) FROM {table}")
                row = await cur.fetchone()
                counts_second[table] = row[0] if row else 0
            except Exception:
                counts_second[table] = -1

        tables_checked = [t for t in counts_first if counts_first[t] > 0 and counts_second.get(t, -1) >= 0]
        tables_idempotent = [t for t in tables_checked if counts_first[t] == counts_second[t]]
        if tables_checked:
            pts += round(40 * len(tables_idempotent) / len(tables_checked))

        tables_with_data = [t for t in counts_first if counts_first[t] > 0]
        diversity_pts = _completeness_score(len(tables_with_data), 5)
        pts += round(diversity_pts * 0.20)

        detail = f"first={counts_first}, second={counts_second}, idempotent={tables_idempotent}"
        _record("seed", "scored", pts, t.elapsed_ms, detail,
                "20 first + 20 second + 40 idempotency + 20 diversity")
        await conn.close()
        assert pts >= 40, f"Seed score too low: {pts}/100"


# =============================================================================
#  8 · MIGRATION FILES  --  coverage + parsability + table extraction
# =============================================================================

class TestMigrations:

    def test_migrations_scored(self):
        migrations_dir = LEISURELLM_DIR / "migrations"
        sql_files = sorted(migrations_dir.glob("*.sqlite.sql"))
        total_stmts = 0
        parsed_stmts = 0
        table_names: set[str] = set()
        parse_errors: list[str] = []

        valid_keywords = frozenset((
            "CREATE", "ALTER", "INSERT", "UPDATE", "DELETE", "DROP",
            "PRAGMA", "BEGIN", "COMMIT", "SELECT", "WITH", "REPLACE",
            "ATTACH", "DETACH", "VACUUM", "REINDEX", "ANALYZE", "EXPLAIN", "IF",
        ))

        with _Timer() as t:
            for f in sql_files:
                content = f.read_text(encoding="utf-8")
                for stmt in content.split(";"):
                    lines = [l for l in stmt.strip().splitlines()
                             if not l.strip().startswith("--")]
                    clean = "\n".join(lines).strip()
                    if not clean:
                        continue
                    total_stmts += 1
                    first_word = clean.split()[0].upper() if clean.split() else ""
                    if first_word in valid_keywords:
                        parsed_stmts += 1
                    else:
                        parse_errors.append(f"{f.name}: '{first_word}'")
                    # Extract table names from CREATE TABLE
                    m = re.search(r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)',
                                  clean, re.I)
                    if m:
                        table_names.add(m.group(1))

        # Check for duplicate migration numbers
        numbers = [f.name.split("_")[0] for f in sql_files]
        duplicate_numbers = len(numbers) - len(set(numbers))

        file_score = _completeness_score(len(sql_files), 10)
        stmt_score = _completeness_score(parsed_stmts, total_stmts)
        table_score = _completeness_score(len(table_names), 15)  # 15+ tables is healthy
        dup_penalty = 10 if duplicate_numbers > 0 else 0
        score = round(file_score * 0.25 + stmt_score * 0.40 + table_score * 0.25) + (10 - dup_penalty)

        detail = (f"{len(sql_files)} files, {parsed_stmts}/{total_stmts} stmts, "
                  f"{len(table_names)} tables, {duplicate_numbers} dup numbers")
        if parse_errors:
            detail += f", issues: {parse_errors[:3]}"
        _record("migration", "coverage_parsability_depth", score, t.elapsed_ms, detail,
                "25 files + 40 stmts + 25 tables + 10 no-duplicates")
        assert len(sql_files) > 0


# =============================================================================
#  9 · CROSS-CONSISTENCY  --  sidebar links > routes, static assets serve
# =============================================================================

_SIDEBAR_LINKS = [
    # Fast pages first so class-level cold start hits something cheap
    "/inbox", "/actions", "/analytics",
    "/teach", "/knowledge", "/gaps",
    "/settings", "/jobs", "/retrieval-log", "/org",
    "/guide",
    # Heavy pages last (need patches + more handler work)
    "/", "/router",
]

_STATIC_ASSETS = [
    "/static/style.css",
    "/static/tailwind.css",
    "/static/lucide.min.js",
    "/static/chart.umd.min.js",
    "/static/animations.css",
    "/static/fonts/inter.css",
]


class TestCrossConsistency:
    """Verify that sidebar nav links actually resolve and static assets serve.
    These catch broken hrefs, unmounted routes, and missing files."""

    # Routes that call external services and need patches to avoid timeouts
    _NEEDS_PATCHES = {"/", "/router"}

    @pytest.mark.parametrize("link", _SIDEBAR_LINKS)
    def test_sidebar_link_resolves(self, client, link):
        from contextlib import ExitStack
        with ExitStack() as stack:
            if link in self._NEEDS_PATCHES:
                stack.enter_context(patch("admin.server.is_first_run", return_value=False))
                stack.enter_context(patch("services.system_tools.SystemTools.get_ollama_status",
                       return_value={"installed": False, "running": False, "models": []}))
                # /router needs phase1_saved=True to avoid redirect; / uses minimal state
                ob_state = ({"provider_detected": True, "provider_connected": True,
                             "phase1_saved": True}
                            if link == "/router"
                            else {"setup_complete": False, "phase1_saved": False,
                                  "provider_detected": False, "provider_connected": False})
                stack.enter_context(patch("admin.routers.settings._build_onboarding_state",
                       AsyncMock(return_value=ob_state)))
            with _Timer() as t:
                resp = client.get(link, follow_redirects=False)
        reachable = resp.status_code in (200, 302, 307)
        pts = 0
        if reachable:
            pts += 50
        if resp.status_code == 200:
            pts += 10
        pts += round(_latency_score(t.elapsed_ms, _LATENCY_BUDGET["page"]) * 0.40)
        _record("cross_check", f"sidebar>{link}", pts, t.elapsed_ms,
                f"status={resp.status_code}")
        assert reachable, f"Sidebar link {link} > {resp.status_code}"

    @pytest.mark.parametrize("asset", _STATIC_ASSETS)
    def test_static_asset_serves(self, client, asset):
        with _Timer() as t:
            resp = client.get(asset)
        pts = 0
        if resp.status_code == 200:
            size = len(resp.content)
            pts += 40
            if size > 100:
                pts += 30
            elif size > 10:
                pts += 10
        pts += round(_latency_score(t.elapsed_ms, _LATENCY_BUDGET["api"]) * 0.30)
        _record("cross_check", f"static>{asset}", pts, t.elapsed_ms,
                f"status={resp.status_code}, "
                f"size={len(resp.content) if resp.status_code == 200 else 0}B")
        assert resp.status_code == 200, f"Static asset {asset} > {resp.status_code}"


# =============================================================================
# 10 · THROUGHPUT STRESS  --  rapid-fire sequential, p50/p95/spread/rps
# =============================================================================

class TestThroughput:
    """Real performance: 20 page hits + 15 API hits in quick succession.
    Scored on p50/p95 latency, p95/p50 spread (consistency), and throughput."""

    def test_rapid_page_requests(self, client):
        pages = (_SIMPLE_PAGES + ["/"])[:20]  # 20 rapid hits
        times: list[float] = []
        errors = 0
        # Patch external deps: "/" calls is_first_run + get_ollama_status + _build_onboarding_state
        with patch("admin.server.is_first_run", return_value=False), \
             patch("services.system_tools.SystemTools.get_ollama_status",
                   return_value={"installed": False, "running": False, "models": []}), \
             patch("admin.routers.settings._build_onboarding_state",
                   AsyncMock(return_value={"setup_complete": False, "phase1_saved": False,
                                           "provider_detected": False, "provider_connected": False})):
            wall_start = time.perf_counter()
            for p in pages:
                t0 = time.perf_counter()
                resp = client.get(p)
                times.append((time.perf_counter() - t0) * 1000)
                if resp.status_code != 200:
                    errors += 1
            wall_ms = (time.perf_counter() - wall_start) * 1000

        times.sort()
        n = len(times)
        p50 = times[n // 2]
        p95 = times[int(n * 0.95)]
        rps = n / (wall_ms / 1000) if wall_ms > 0 else 0

        pts = 0
        flags = []

        # Error rate (20)
        pts += round(20 * (1 - errors / n))
        flags.append(f"errors={errors}/{n}")

        # p50 (20)  --  aim for <15 ms
        if p50 < 10:
            pts += 20
        elif p50 < 20:
            pts += 15
        elif p50 < 50:
            pts += 10
        elif p50 < 100:
            pts += 5
        flags.append(f"p50={p50:.1f}ms")

        # p95 (20)  --  aim for <40 ms
        if p95 < 20:
            pts += 20
        elif p95 < 40:
            pts += 15
        elif p95 < 80:
            pts += 10
        elif p95 < 200:
            pts += 5
        flags.append(f"p95={p95:.1f}ms")

        # Spread (20)  --  p95/p50, aim for <2.5x
        spread = p95 / p50 if p50 > 0 else 999
        if spread < 1.5:
            pts += 20
        elif spread < 2.5:
            pts += 15
        elif spread < 4.0:
            pts += 10
        elif spread < 6.0:
            pts += 5
        flags.append(f"spread={spread:.1f}x")

        # Throughput (20)  --  aim for >40 rps
        if rps > 80:
            pts += 20
        elif rps > 40:
            pts += 15
        elif rps > 20:
            pts += 10
        elif rps > 10:
            pts += 5
        flags.append(f"rps={rps:.0f}")

        _record("stress", "rapid_pages (20 hits)", pts, wall_ms,
                ", ".join(flags),
                "20 errors + 20 p50 + 20 p95 + 20 spread + 20 throughput")
        assert errors == 0, f"{errors} page errors in rapid-fire"

    def test_rapid_api_requests(self, client):
        apis = [p for p, _ in _API_TYPE_CHECKS[:5]] * 3  # 15 rapid API hits
        times: list[float] = []
        errors = 0
        # Patch: /api/v1/ollama/status calls real Ollama binary (socket timeout)
        with patch("services.system_tools.SystemTools.get_ollama_status",
                   return_value={"installed": False, "running": False, "models": []}):
            wall_start = time.perf_counter()
            for p in apis:
                t0 = time.perf_counter()
                resp = client.get(p)
                times.append((time.perf_counter() - t0) * 1000)
                if resp.status_code != 200:
                    errors += 1
            wall_ms = (time.perf_counter() - wall_start) * 1000

        times.sort()
        n = len(times)
        p50 = times[n // 2]
        p95 = times[int(n * 0.95)]
        rps = n / (wall_ms / 1000) if wall_ms > 0 else 0

        pts = 0
        flags = []

        pts += round(20 * (1 - errors / n))
        flags.append(f"errors={errors}/{n}")

        if p50 < 5:
            pts += 20
        elif p50 < 15:
            pts += 15
        elif p50 < 30:
            pts += 10
        elif p50 < 60:
            pts += 5
        flags.append(f"p50={p50:.1f}ms")

        if p95 < 15:
            pts += 20
        elif p95 < 30:
            pts += 15
        elif p95 < 60:
            pts += 10
        elif p95 < 120:
            pts += 5
        flags.append(f"p95={p95:.1f}ms")

        spread = p95 / p50 if p50 > 0 else 999
        if spread < 1.5:
            pts += 20
        elif spread < 2.5:
            pts += 15
        elif spread < 4.0:
            pts += 10
        elif spread < 6.0:
            pts += 5
        flags.append(f"spread={spread:.1f}x")

        if rps > 150:
            pts += 20
        elif rps > 80:
            pts += 15
        elif rps > 40:
            pts += 10
        elif rps > 15:
            pts += 5
        flags.append(f"rps={rps:.0f}")

        _record("stress", "rapid_apis (15 hits)", pts, wall_ms,
                ", ".join(flags),
                "20 errors + 20 p50 + 20 p95 + 20 spread + 20 throughput")
        assert errors == 0, f"{errors} API errors in rapid-fire"


# =============================================================================
#  REPORT  --  weighted scorecard with updated weights
# =============================================================================

def _letter_grade(score: float) -> str:
    if score >= 95:
        return "A+"
    if score >= 90:
        return "A"
    if score >= 85:
        return "A-"
    if score >= 80:
        return "B+"
    if score >= 75:
        return "B"
    if score >= 70:
        return "B-"
    if score >= 65:
        return "C+"
    if score >= 60:
        return "C"
    if score >= 55:
        return "C-"
    if score >= 50:
        return "D"
    return "F"


_CATEGORY_WEIGHTS = {
    "page":         0.15,
    "template":     0.12,
    "api":          0.18,
    "onboarding":   0.10,
    "import":       0.08,
    "model_router": 0.05,
    "seed":         0.07,
    "migration":    0.05,
    "cross_check":  0.12,
    "stress":       0.08,
}


def test_zz_scored_report():
    """Final test  --  prints the scored health report with letter grades."""
    if not _SCORES:
        return

    categories: dict[str, dict] = {}
    for entry in _SCORES:
        cat = entry["category"]
        if cat not in categories:
            categories[cat] = {"scores": [], "times": []}
        categories[cat]["scores"].append(entry["score"])
        categories[cat]["times"].append(entry["ms"])

    graded: dict[str, dict] = {}
    for cat, data in categories.items():
        scores = data["scores"]
        times = data["times"]
        avg = sum(scores) / len(scores) if scores else 0
        graded[cat] = {
            "grade": _letter_grade(avg),
            "avg_score": round(avg, 1),
            "min_score": min(scores) if scores else 0,
            "max_score": max(scores) if scores else 0,
            "checks": len(scores),
            "avg_ms": round(sum(times) / len(times), 1) if times else 0,
            "max_ms": round(max(times), 1) if times else 0,
        }

    weighted_sum = 0.0
    weight_sum = 0.0
    for cat, info in graded.items():
        w = _CATEGORY_WEIGHTS.get(cat, 0.05)
        weighted_sum += info["avg_score"] * w
        weight_sum += w
    overall = round(weighted_sum / weight_sum, 1) if weight_sum else 0

    low_scorers = [e for e in _SCORES if e["score"] < 70]

    report = {
        "overall_score": overall,
        "overall_grade": _letter_grade(overall),
        "total_checks": len(_SCORES),
        "categories": graded,
        "low_scorers": low_scorers,
        "details": _SCORES,
    }

    border = "=" * 78
    print(f"\n{border}")
    print(f"  SYSTEM HEALTH SCORECARD (HARD MODE)    --    Overall: {overall}/100  ({_letter_grade(overall)})")
    print(border)
    header = f"  {'Category':<16} {'Grade':>6} {'Avg':>6} {'Min':>5} {'Max':>5} {'Checks':>7} {'Avg ms':>8}"
    print(header)
    print("  " + "-" * 64)
    for cat in sorted(graded, key=lambda c: graded[c]["avg_score"]):
        g = graded[cat]
        print(f"  {cat:<16} {g['grade']:>6} {g['avg_score']:>5.1f} "
              f"{g['min_score']:>5} {g['max_score']:>5} "
              f"{g['checks']:>7} {g['avg_ms']:>7.1f}")
    print(border)

    if low_scorers:
        print(f"  LOW SCORERS ({len(low_scorers)} checks below 70):")
        for e in low_scorers[:15]:
            print(f"    [{e['category']}] {e['test']}: {e['score']}/100   --   {e['detail'][:80]}")
        if len(low_scorers) > 15:
            print(f"    ... and {len(low_scorers) - 15} more")
        print(border)

    report_path = ROOT_DIR / "Output" / "reports" / "test_health_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"  Report > {report_path}")
    print(border)


