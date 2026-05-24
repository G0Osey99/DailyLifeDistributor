"""CustomTkinter GUI for the agent — "Hero status" direction.

Same threading model as the previous version: this whole UI runs on the
main thread, the network code lives on a daemon thread, and we pull
state via `Tk.after()` every 500 ms. The big change is purely visual —
connection status is now the hero element of the window so the user can
read it from across the room, and the rest of the surface (chips, log,
section headers) borrows the website's tokens directly so the agent
visually belongs to autoalert.pro.

Drop in over the previous file — public API is identical:
    AgentGUI(state, shutdown_event).run()
    StateLogHandler(state)
"""
from __future__ import annotations

import logging
import math
import threading
import tkinter as tk
import tkinter.font
import urllib.request
import urllib.parse
import json as _json
import webbrowser

import customtkinter as ctk

from agent.state import (
    AgentState,
    CONN_AUTH_FAILED,
    CONN_CONNECTING,
    CONN_DISCONNECTED,
    CONN_ONLINE,
    CONN_STARTING,
    CONN_STOPPED,
    ACT_IDLE,
    ACT_UPLOADING,
)


log = logging.getLogger(__name__)


# ─── Palette ────────────────────────────────────────────────────────────
# sRGB approximations of the website's oklch tokens (templates/base.html).
# Match these to the web app so the agent visually reads as the same
# product. Keep contrast WCAG-AA.
PAL = {
    "shell_bg":           "#f7f6f3",
    "shell_panel":        "#ffffff",
    "shell_sunken":       "#f0eeea",
    "shell_border":       "#dcd9d3",
    "shell_border_strong":"#c0bdb6",
    "text":               "#1f242e",
    "text_muted":         "#6e7382",
    "text_dim":           "#9097a4",
    "accent":             "#2f6fd3",
    "accent_hover":       "#3e7fdf",
    "accent_soft":        "#e1ecfa",
    "ok":                 "#239e62",
    "ok_soft":            "#dff2e7",
    "warn":               "#c98a26",
    "warn_soft":          "#faecd1",
    "err":                "#c4332c",
    "err_soft":           "#f7dad7",
}


# ─── State → visual mapping ──────────────────────────────────────────────
# (color, soft_bg, glyph_kind, title, pulse_on)
#   glyph_kind ∈ {"check", "spinner", "arrow", "key", "dot"}
#
# Activity overrides connection's hero painting when uploading — a green
# "Connected" hero while a noisy upload is in progress hides the more
# interesting thing the user wants to see.
def _hero_view(connection: str, activity: str) -> dict:
    if activity == ACT_UPLOADING and connection == CONN_ONLINE:
        return {
            "color": PAL["accent"], "soft": PAL["accent_soft"],
            "glyph": "arrow", "title": "Uploading", "pulse": True,
        }
    if connection == CONN_ONLINE:
        return {
            "color": PAL["ok"], "soft": PAL["ok_soft"],
            "glyph": "check", "title": "Connected", "pulse": True,
        }
    if connection in (CONN_CONNECTING, CONN_STARTING):
        return {
            "color": PAL["warn"], "soft": PAL["warn_soft"],
            "glyph": "spinner",
            "title": "Connecting…" if connection == CONN_CONNECTING else "Starting up…",
            "pulse": True,
        }
    if connection == CONN_DISCONNECTED:
        return {
            "color": PAL["warn"], "soft": PAL["warn_soft"],
            "glyph": "spinner", "title": "Reconnecting…", "pulse": True,
        }
    if connection == CONN_AUTH_FAILED:
        return {
            "color": PAL["err"], "soft": PAL["err_soft"],
            "glyph": "key", "title": "Re-pair required", "pulse": False,
        }
    # CONN_STOPPED or unknown
    return {
        "color": PAL["text_dim"], "soft": PAL["shell_sunken"],
        "glyph": "dot", "title": "Stopped", "pulse": False,
    }


def _pick_font_family() -> str:
    """Prefer Geist (matches the website), then common system sans-serifs."""
    try:
        installed = set(tkinter.font.families())
    except Exception:
        installed = set()
    for name in ("Geist", "Inter", "SF Pro Display",
                 "Segoe UI", "Helvetica Neue", "Helvetica", "Arial"):
        if name in installed:
            return name
    return "TkDefaultFont"


def _blend(hex_a: str, hex_b: str, t: float) -> str:
    """Linear-blend two hex colors. t=0 → a, t=1 → b. Used by the pulse
    halo to fade from hero color → shell_panel without alpha support."""
    t = max(0.0, min(1.0, t))
    ar, ag, ab = int(hex_a[1:3], 16), int(hex_a[3:5], 16), int(hex_a[5:7], 16)
    br, bg, bb = int(hex_b[1:3], 16), int(hex_b[3:5], 16), int(hex_b[5:7], 16)
    r = round(ar + (br - ar) * t)
    g = round(ag + (bg - ag) * t)
    b = round(ab + (bb - ab) * t)
    return f"#{r:02x}{g:02x}{b:02x}"


class AgentGUI:
    """Main agent window. One instance per process."""

    POLL_MS = 500
    PULSE_MS = 33          # ~30 fps — smoother breathing
    PULSE_PERIOD_MS = 2000 # one full ping every 2s, matches the design CSS
    SESSIONS_POLL_MS = 15_000   # match the web app's sb-conn-list cadence
    SESSIONS_TIMEOUT_S = 5
    # Display order + pretty names for the rows. The endpoint also returns
    # "agent" — that's us, so we suppress it.
    SESSION_ORDER = (
        ("youtube",      "YouTube"),
        ("simplecast",   "SimpleCast"),
        ("vista_social", "Vista Social"),
        ("rock",         "Rock"),
        ("ollama",       "Ollama"),
    )
    LOG_FONT_FAMILIES = ("Geist Mono", "JetBrains Mono", "Cascadia Mono",
                         "Consolas", "Menlo", "Courier New")

    def __init__(self, state: AgentState, shutdown_event: threading.Event) -> None:
        self.state = state
        self.shutdown_event = shutdown_event
        self._pairing_dialog_open = False
        self._pulse_t0_ms = 0    # set on first pulse tick
        self._chip_widgets: list[ctk.CTkFrame] = []
        self._session_row_widgets: dict[str, dict] = {}  # key -> {row, dot, detail}
        self._sessions_inflight = False
        self._sessions_data: dict = {}
        self._sessions_updated_at: float | None = None
        self._sessions_unavailable = False  # endpoint returned non-2xx once

        # Last-rendered hero descriptor — lets us skip a full hero repaint
        # when nothing material has changed (just the pulse animates).
        self._last_hero: dict | None = None

        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("blue")

        self.root = ctk.CTk()
        self.root.title("Daily Life Distributor — Agent")
        self.root.geometry("540x720")
        self.root.minsize(460, 620)
        self.root.configure(fg_color=PAL["shell_bg"])
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._font = _pick_font_family()
        self._mono = self._pick_mono_family()

        self._build_ui()
        # Start the polling + pulse loops.
        self.root.after(self.POLL_MS, self._poll)
        self.root.after(self.PULSE_MS, self._tick_pulse)
        # First sessions poll fires immediately so the panel isn't blank
        # for 15 s after launch; subsequent ticks schedule themselves.
        self.root.after(800, self._tick_sessions)

    # ─── layout ─────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        # Header — slim brand row + tiny AGENT badge + version mono text.
        header = ctk.CTkFrame(
            self.root, fg_color=PAL["shell_panel"], corner_radius=0,
            height=52,
        )
        header.pack(fill="x", side="top")
        header.pack_propagate(False)
        header.configure(border_width=0)
        # Bottom hairline only — CTkFrame doesn't do per-side borders, so
        # a 1px placed-frame is the cheap way to get a divider line.
        tk.Frame(header, bg=PAL["shell_border"], height=1).place(
            relx=0, rely=1.0, relwidth=1, anchor="sw",
        )

        brand_mark = ctk.CTkFrame(
            header, fg_color=PAL["accent"], corner_radius=7,
            width=28, height=28,
        )
        brand_mark.place(x=18, y=12)
        brand_mark.pack_propagate(False)
        # Tiny doc-icon glyph inside the brand mark, matching the website.
        glyph = tk.Canvas(brand_mark, width=18, height=18,
                          highlightthickness=0, bg=PAL["accent"])
        glyph.place(relx=0.5, rely=0.5, anchor="center")
        glyph.create_line(4, 5, 14, 5, fill="#ffffff", width=1.6, capstyle="round")
        glyph.create_line(4, 9, 11, 9, fill="#ffffff", width=1.6, capstyle="round")
        glyph.create_line(4, 13, 8, 13, fill="#ffffff", width=1.6, capstyle="round")
        glyph.create_oval(12, 11, 16, 15, fill="#ffffff", outline="")

        # Title gets 175px of horizontal room — "Daily Life Distributor"
        # at 13pt bold needs ~165px and was being truncated by the AGENT
        # badge sitting at x=202. Bumped the badge x and widened the
        # label's allotted span to fix the cut-off.
        ctk.CTkLabel(
            header, text="Daily Life Distributor",
            text_color=PAL["text"], font=(self._font, 13, "bold"),
            anchor="w",
        ).place(x=58, y=16, width=180)

        agent_badge = ctk.CTkLabel(
            header, text="AGENT", text_color=PAL["accent"],
            font=(self._font, 9, "bold"),
            fg_color=PAL["accent_soft"], corner_radius=4,
            padx=6, pady=2,
        )
        agent_badge.place(x=240, y=19)

        self.version_label = ctk.CTkLabel(
            header, text="", text_color=PAL["text_dim"],
            font=(self._mono, 10),
        )
        self.version_label.place(relx=1.0, x=-20, y=18, anchor="ne")

        # ── Hero status block ──────────────────────────────────────────
        hero = ctk.CTkFrame(self.root, fg_color=PAL["shell_panel"], corner_radius=0)
        hero.pack(fill="x", side="top")
        tk.Frame(hero, bg=PAL["shell_border"], height=1).pack(
            side="bottom", fill="x",
        )

        hero_inner = ctk.CTkFrame(hero, fg_color="transparent")
        hero_inner.pack(fill="x", padx=22, pady=22)

        # Pulse disc — a single tk.Canvas we redraw every PULSE_MS.
        self.hero_canvas = tk.Canvas(
            hero_inner, width=64, height=64,
            highlightthickness=0, bg=PAL["shell_panel"],
        )
        self.hero_canvas.pack(side="left", padx=(0, 18))

        hero_text = ctk.CTkFrame(hero_inner, fg_color="transparent")
        hero_text.pack(side="left", fill="x", expand=True)

        self.hero_title = ctk.CTkLabel(
            hero_text, text="Starting up…",
            text_color=PAL["text"], font=(self._font, 22, "bold"),
            anchor="w", justify="left",
        )
        self.hero_title.pack(anchor="w", pady=(0, 2))

        # Subtitle row — mixed inline color so the server hostname can
        # render as a clickable accent-blue link, matching the design.
        # Tk's CTkLabel doesn't support inline rich text, so we build
        # the subtitle as a horizontal row of three labels (left chunk
        # + clickable hostname + right chunk). _refresh_from_state
        # configures each label's text per state.
        self.hero_subtitle_row = ctk.CTkFrame(hero_text, fg_color="transparent")
        self.hero_subtitle_row.pack(anchor="w", pady=(2, 10), fill="x")

        self.hero_subtitle_left = ctk.CTkLabel(
            self.hero_subtitle_row, text="",
            text_color=PAL["text_muted"], font=(self._font, 11),
        )
        self.hero_subtitle_left.pack(side="left")

        self.hero_subtitle_link = ctk.CTkLabel(
            self.hero_subtitle_row, text="",
            text_color=PAL["accent"], font=(self._font, 11),
            cursor="hand2",
        )
        # Clicking the hostname opens the dashboard, same as the
        # primary footer CTA. Bound on the label so it acts as a link.
        self.hero_subtitle_link.bind(
            "<Button-1>", lambda _e: self._open_dashboard(),
        )
        self.hero_subtitle_link.pack(side="left")

        self.hero_subtitle_right = ctk.CTkLabel(
            self.hero_subtitle_row, text="",
            text_color=PAL["text_muted"], font=(self._font, 11),
        )
        self.hero_subtitle_right.pack(side="left")

        # Back-compat shim — older code paths still set .hero_subtitle.
        # Point it at the left label so existing configure() calls don't
        # crash; _refresh_from_state has been updated to use the row
        # API directly.
        self.hero_subtitle = self.hero_subtitle_left

        # Chips row — built dynamically from snapshot state.
        self.chips_row = ctk.CTkFrame(hero_text, fg_color="transparent")
        self.chips_row.pack(anchor="w", fill="x")

        # Optional progress bar — only shown when uploading.
        self.progress = ctk.CTkProgressBar(
            hero_text, height=5, corner_radius=999,
            fg_color=PAL["shell_sunken"], progress_color=PAL["accent"],
            border_width=0,
        )
        # Don't pack yet — toggled in _refresh_from_state().
        self.progress.set(0)

        # ── Footer ─────────────────────────────────────────────────────
        # IMPORTANT: footer must pack BEFORE body even though it visually
        # sits below. Tk's pack uses first-claim semantics: side="bottom"
        # only reserves space from what's left after earlier siblings.
        # If body packs first with expand=True it consumes everything
        # and the footer gets clipped to height=0 — exactly what the
        # user reported in v0.6.11 ("buttons at the bottom are cut off").
        footer = ctk.CTkFrame(self.root, fg_color=PAL["shell_panel"],
                              corner_radius=0, height=52)
        footer.pack(fill="x", side="bottom")
        footer.pack_propagate(False)
        tk.Frame(footer, bg=PAL["shell_border"], height=1).pack(side="top", fill="x")

        self.notice = ctk.CTkLabel(
            footer, text="Keep open while uploads run.",
            text_color=PAL["text_dim"], font=(self._font, 10),
        )
        self.notice.place(x=18, rely=0.5, anchor="w")

        # Primary CTA — text + color may flip to "Re-pair device" in
        # auth-failed state, so we keep a handle to it.
        self.primary_btn = ctk.CTkButton(
            footer, text="Open dashboard  →",
            command=self._primary_action,
            fg_color=PAL["accent"], hover_color=PAL["accent_hover"],
            text_color="#ffffff", corner_radius=7,
            font=(self._font, 11, "bold"),
            height=30, width=150,
        )
        self.primary_btn.place(relx=1.0, x=-18, rely=0.5, anchor="e")

        self.quit_btn = ctk.CTkButton(
            footer, text="Quit", command=self._on_close,
            fg_color="transparent", hover_color=PAL["shell_sunken"],
            text_color=PAL["text"], corner_radius=7,
            font=(self._font, 11),
            border_width=1, border_color=PAL["shell_border_strong"],
            height=30, width=64,
        )
        self.quit_btn.place(relx=1.0, x=-180, rely=0.5, anchor="e")

        # ── Body: sessions panel + activity log ────────────────────────
        body = ctk.CTkFrame(self.root, fg_color=PAL["shell_bg"], corner_radius=0)
        body.pack(fill="both", expand=True, padx=18, pady=(14, 0))

        # ─── Sessions card ───
        # Polled from <server>/sessions/status every 15 s in a worker
        # thread. Mirrors templates/base.html's sb-conn-list so the agent
        # surfaces the same status the website's sidebar already shows.
        sess_header = ctk.CTkFrame(body, fg_color="transparent")
        sess_header.pack(fill="x", pady=(0, 8))
        self._section_rail(sess_header, "Sessions").pack(side="left")
        self.sessions_updated = ctk.CTkLabel(
            sess_header, text="—",
            text_color=PAL["text_dim"], font=(self._font, 10),
        )
        self.sessions_updated.pack(side="right")

        self.sessions_card = ctk.CTkFrame(
            body, fg_color=PAL["shell_panel"],
            border_width=1, border_color=PAL["shell_border"],
            corner_radius=10,
        )
        self.sessions_card.pack(fill="x", pady=(0, 12))
        # Pre-build a row per known service so re-renders just configure()
        # the existing widgets — no destroy/rebuild flicker every 15 s.
        for i, (key, name) in enumerate(self.SESSION_ORDER):
            self._build_session_row(
                key, name, last=(i == len(self.SESSION_ORDER) - 1)
            )

        # ─── Activity log ───
        log_header = ctk.CTkFrame(body, fg_color="transparent")
        log_header.pack(fill="x", pady=(0, 8))
        self._section_rail(log_header, "Activity log").pack(side="left")
        self.open_log_btn = ctk.CTkButton(
            log_header, text="Open file",
            command=self._open_log_file,
            fg_color="transparent", hover_color=PAL["shell_sunken"],
            text_color=PAL["text_dim"], font=(self._font, 11),
            corner_radius=6, width=70, height=22,
            border_width=0,
        )
        self.open_log_btn.pack(side="right")

        log_frame = ctk.CTkFrame(
            body, fg_color=PAL["shell_panel"],
            border_width=1, border_color=PAL["shell_border"],
            corner_radius=10,
        )
        log_frame.pack(fill="both", expand=True, pady=(0, 14))

        self.log_box = ctk.CTkTextbox(
            log_frame,
            fg_color=PAL["shell_panel"],
            text_color=PAL["text_muted"],
            border_width=0,
            font=(self._mono, 11),
            wrap="none",
            corner_radius=10,
        )
        self.log_box.pack(fill="both", expand=True, padx=14, pady=10)
        self.log_box.configure(state="disabled")

    # ─── small helpers ──────────────────────────────────────────────────
    def _build_session_row(self, key: str, name: str, last: bool) -> None:
        """Build one session row inside self.sessions_card and stash its
        widgets in self._session_row_widgets[key] so _render_sessions()
        can just configure() text/color in place — no destroy churn."""
        row = ctk.CTkFrame(self.sessions_card, fg_color="transparent")
        row.pack(fill="x", padx=13, pady=(8, 8 if last else 0))
        # Faux border-bottom — thin Frame packed below the row, except on
        # the last item. Lives inside the card not outside.
        if not last:
            tk.Frame(self.sessions_card, bg=PAL["shell_border"], height=1) \
                .pack(fill="x", padx=13)

        dot = tk.Canvas(row, width=9, height=9, highlightthickness=0,
                        bg=PAL["shell_panel"])
        dot.create_oval(1, 1, 8, 8, fill=PAL["text_dim"], outline="",
                        tags="d")
        dot.pack(side="left", padx=(0, 10))

        ctk.CTkLabel(
            row, text=name, text_color=PAL["text"],
            font=(self._font, 12, "bold"),
        ).pack(side="left")

        detail = ctk.CTkLabel(
            row, text="checking…", text_color=PAL["text_dim"],
            font=(self._font, 11),
        )
        detail.pack(side="right")

        self._session_row_widgets[key] = {"row": row, "dot": dot, "detail": detail}

    def _section_rail(self, parent, text: str) -> ctk.CTkFrame:
        """Renders the website's section-header treatment: a 3px accent
        bar on the left, then an uppercase label. Returns the container
        frame so the caller decides how it packs.

        CTkFrame defaults to height=200 when not specified — using
        ``fill="y"`` on the bar made it absorb that 200px and pushed the
        footer ~400px off-screen (two section headers, ~200px each of
        phantom vertical space). Fix is to clamp the wrap to label
        height, disable propagation, and give the bar an explicit
        height that matches.
        """
        # Match the label's natural height for a 10pt bold font (≈14-16px
        # depending on the system DPI). A bit of inset on top + bottom
        # makes the bar read as a section rail, not a divider.
        rail_h = 16
        wrap = ctk.CTkFrame(parent, fg_color="transparent",
                            height=rail_h, width=120)
        wrap.pack_propagate(False)
        bar = ctk.CTkFrame(
            wrap, fg_color=PAL["accent"],
            width=3, height=rail_h - 2, corner_radius=2,
        )
        bar.pack_propagate(False)
        bar.pack(side="left", pady=1)
        ctk.CTkLabel(
            wrap, text=text.upper(),
            text_color=PAL["text_muted"],
            font=(self._font, 10, "bold"),
        ).pack(side="left", padx=(9, 0))
        return wrap

    def _make_chip(self, parent, text: str, dot_color: str | None = None) -> ctk.CTkFrame:
        chip = ctk.CTkFrame(
            parent, fg_color=PAL["shell_sunken"],
            border_width=1, border_color=PAL["shell_border"],
            corner_radius=999, height=22,
        )
        chip.pack_propagate(False)
        if dot_color is not None:
            dot = tk.Canvas(chip, width=8, height=8, highlightthickness=0,
                            bg=PAL["shell_sunken"])
            dot.create_oval(1, 1, 7, 7, fill=dot_color, outline="")
            dot.pack(side="left", padx=(10, 0))
            label_padx = (5, 12)
        else:
            label_padx = (12, 12)
        ctk.CTkLabel(
            chip, text=text, text_color=PAL["text_muted"],
            font=(self._font, 10),
        ).pack(side="left", padx=label_padx)
        return chip

    def _pick_mono_family(self) -> str:
        try:
            installed = set(tkinter.font.families())
        except Exception:
            installed = set()
        for f in self.LOG_FONT_FAMILIES:
            if f in installed:
                return f
        return "TkFixedFont"

    # ─── sessions panel ─────────────────────────────────────────────────
    def _tick_sessions(self) -> None:
        """Schedule the next sessions poll. Self-rescheduling so we don't
        need to track multiple after() handles."""
        if self.shutdown_event.is_set():
            return
        self._fetch_sessions_async()
        self.root.after(self.SESSIONS_POLL_MS, self._tick_sessions)

    def _fetch_sessions_async(self) -> None:
        """Kick off a one-shot worker thread that GETs /sessions/status.
        Result is posted back to the Tk main thread via root.after(0, …)
        so the only widget mutation happens on the GUI thread."""
        if self._sessions_inflight:
            return
        server = (self.state.server_url or "").strip()
        if not server:
            # Nothing to poll until pairing finishes and a server URL is set.
            return
        # Normalize wss/ws/etc. → https/http for the GET. The HTTP endpoint
        # lives at the same host the WebSocket connects to. Append the
        # agent's pair token as ?token= so the inline auth check on
        # /sessions/status admits us — without it the endpoint redirects
        # to /login (no cookie) and every row stays "unknown".
        # Prefer the in-memory token mirrored onto AgentState at pair
        # time. Falling through to the keyring lookup only when state
        # has no token yet (very early boot, or running without the
        # main.py wiring that sets it). Keyring under PyInstaller is
        # flaky on Windows; the state copy is the reliable path.
        token = (getattr(self.state, "token", "") or "").strip()
        if not token:
            try:
                from agent import config as _agent_config
                raw = _agent_config.get_token()
                token = (raw or "").strip()
                if not token:
                    log.warning(
                        "sessions poll: no token on state and "
                        "config.get_token() returned empty; sessions "
                        "panel will read 'unavailable'.",
                    )
            except Exception as e:
                log.warning("sessions poll: get_token raised: %s", e, exc_info=True)
        url = _to_http(server.rstrip("/")) + "/sessions/status"
        if token:
            url = url + "?" + urllib.parse.urlencode({"token": token})
        self._sessions_inflight = True

        def worker() -> None:
            data = None
            unreachable = False
            try:
                req = urllib.request.Request(
                    url, headers={"Accept": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=self.SESSIONS_TIMEOUT_S) as resp:
                    if 200 <= resp.status < 300:
                        body = resp.read().decode("utf-8", errors="replace")
                        data = _json.loads(body)
                    else:
                        unreachable = True
            except Exception:
                log.debug("sessions/status fetch failed", exc_info=True)
                unreachable = True
            # Hop back to the main thread. Tk widget config from a
            # background thread is undefined behavior on some platforms.
            self.root.after(0, self._on_sessions_data, data, unreachable)

        # Daemon so it never blocks process exit. Throwaway thread — one
        # per 15 s — is fine; this is cheaper than maintaining a pool.
        threading.Thread(
            target=worker, name="agent-sessions-poll", daemon=True,
        ).start()

    def _on_sessions_data(self, data: dict | None, unreachable: bool) -> None:
        """Apply a sessions poll result to the UI. Runs on the Tk thread."""
        self._sessions_inflight = False
        if data is not None:
            self._sessions_data = data
            self._sessions_unavailable = False
            import time as _time
            self._sessions_updated_at = _time.time()
        else:
            # Don't blank the cached data on a single failed poll — keep
            # showing the last known state and just flip the "unavailable"
            # hint so the user knows it's stale.
            self._sessions_unavailable = unreachable
        self._render_sessions()

    def _render_sessions(self) -> None:
        """Repaint every session row from self._sessions_data. Cheap —
        we configure existing widgets, never destroy/rebuild them."""
        data = self._sessions_data or {}
        any_known = False
        for key, _name in self.SESSION_ORDER:
            entry = data.get(key)
            widgets = self._session_row_widgets.get(key)
            if not widgets:
                continue
            if entry is None:
                # No data yet for this service.
                widgets["dot"].itemconfig("d", fill=PAL["text_dim"])
                widgets["detail"].configure(
                    text="checking…" if not self._sessions_unavailable else "unknown",
                    text_color=PAL["text_dim"],
                )
                continue
            any_known = True
            ok = bool(entry.get("ok"))
            # Strip the leading service name from label_on/label_off so
            # we don't render "YouTube" twice ("YouTube connected" next
            # to the row name "YouTube"). Lowercase the trimmed bit so
            # it reads as a tag, not a sentence fragment.
            full_label = entry.get("label_on" if ok else "label_off") or ""
            detail_txt = _trim_service_prefix(full_label, _name) or (
                "connected" if ok else "needs login"
            )
            widgets["dot"].itemconfig(
                "d", fill=PAL["ok"] if ok else PAL["warn"],
            )
            widgets["detail"].configure(
                text=detail_txt,
                text_color=PAL["text_muted"] if ok else PAL["warn"],
            )

        # Updated-at stamp on the right of the section header.
        if self._sessions_unavailable and not any_known:
            self.sessions_updated.configure(
                text="unavailable", text_color=PAL["warn"],
            )
        elif self._sessions_updated_at:
            import time as _time
            stamp = _time.strftime("%H:%M", _time.localtime(self._sessions_updated_at))
            suffix = " (stale)" if self._sessions_unavailable else ""
            self.sessions_updated.configure(
                text=f"updated {stamp}{suffix}",
                text_color=PAL["warn"] if self._sessions_unavailable else PAL["text_dim"],
            )
        else:
            self.sessions_updated.configure(
                text="—", text_color=PAL["text_dim"],
            )

    # ─── hero painting ──────────────────────────────────────────────────
    def _paint_hero_static(self, view: dict) -> None:
        """Redraw the disc + glyph (everything except the animated halo)."""
        c = self.hero_canvas
        c.delete("static")

        # Static soft ring (inset 10) + solid disc (inset 20)
        c.create_oval(10, 10, 54, 54, fill=view["soft"], outline="", tags="static")
        c.create_oval(20, 20, 44, 44, fill=view["color"], outline="", tags="static")

        # Glyph on top of the solid disc — drawn in white. Coordinates
        # are local to the canvas; the disc is centered at (32, 32) with
        # radius 12.
        glyph = view["glyph"]
        if glyph == "check":
            c.create_line(25, 32, 30, 37, fill="#fff", width=2.4,
                          capstyle="round", joinstyle="round", tags="static")
            c.create_line(30, 37, 40, 27, fill="#fff", width=2.4,
                          capstyle="round", joinstyle="round", tags="static")
        elif glyph == "arrow":
            c.create_line(32, 39, 32, 25, fill="#fff", width=2.6,
                          capstyle="round", tags="static")
            c.create_line(26, 31, 32, 25, fill="#fff", width=2.6,
                          capstyle="round", tags="static")
            c.create_line(38, 31, 32, 25, fill="#fff", width=2.6,
                          capstyle="round", tags="static")
        elif glyph == "key":
            c.create_oval(24, 33, 33, 42, outline="#fff", width=2.2, tags="static")
            c.create_line(31, 35, 41, 25, fill="#fff", width=2.2,
                          capstyle="round", tags="static")
            c.create_line(38, 28, 40, 30, fill="#fff", width=2.2,
                          capstyle="round", tags="static")
        elif glyph == "spinner":
            # Drawn dynamically in _tick_pulse so it rotates; nothing
            # static needed here.
            pass
        else:  # "dot"
            c.create_oval(28, 28, 36, 36, fill="#fff", outline="", tags="static")

    def _tick_pulse(self) -> None:
        """Animate the pulse halo + spinner glyph.

        Redesigned vs. the previous version to fix two stutters:

        1. _paint_hero_static was being called on every frame, re-deleting
           and re-drawing 5-10 canvas items each tick. The disc + glyph
           flickered through every transition. Now the static layer is
           drawn once per state-change (from _refresh_from_state) and the
           pulse layer is stacked BELOW it so it can't obscure the disc
           — no static repaint needed here.

        2. Linear easing made the motion feel robotic. Now uses an
           ease-out-cubic ramp so the ring decelerates as it expands,
           and holds invisible for the last 30% of the cycle before
           restarting. This matches the CSS @keyframes agentPulse
           recipe from the design HTML (scale 1.0 → 2.4, opacity
           0.6 → 0 in the first 70%, hold for the remainder).

        Two staggered rings (large + small) give the breathing more
        depth without doubling the cost — both are simple ovals.
        """
        if self.shutdown_event.is_set():
            return
        try:
            view = self._last_hero or _hero_view(CONN_STARTING, ACT_IDLE)
            now = int(_now_ms())
            phase = (now % self.PULSE_PERIOD_MS) / self.PULSE_PERIOD_MS

            c = self.hero_canvas
            c.delete("pulse")

            if view["pulse"]:
                self._draw_pulse_ring(
                    c, phase,
                    base_color=view["color"],
                    r_start=12.0, r_end=28.0,
                    visible_for=0.70,
                    start_alpha=0.60,
                    width=2,
                )
                # Second, slightly-offset ring for depth — matches the
                # design's agentPulseLarge keyframes (smaller travel,
                # longer visible window, lower max alpha).
                offset_phase = (phase + 0.35) % 1.0
                self._draw_pulse_ring(
                    c, offset_phase,
                    base_color=view["color"],
                    r_start=14.0, r_end=22.0,
                    visible_for=0.80,
                    start_alpha=0.45,
                    width=1,
                )
                # Stack the freshly-drawn pulse layer BENEATH the static
                # disc so the disc never has to be repainted. raise_()
                # ensures any newly-drawn 'static' items also stay on top.
                try:
                    c.tag_lower("pulse", "static")
                except tk.TclError:
                    pass  # 'static' not drawn yet (first tick) — no-op.

            if view["glyph"] == "spinner":
                c.delete("spin")
                # Rotating arc — start angle cycles 0..360 over the period.
                angle = (now % 1200) / 1200 * 360
                c.create_arc(24, 24, 40, 40, start=angle, extent=260,
                             style="arc", outline="#fff", width=2.5,
                             tags="spin")
        except Exception:
            log.debug("pulse tick failed", exc_info=True)
        finally:
            if not self.shutdown_event.is_set():
                self.root.after(self.PULSE_MS, self._tick_pulse)

    def _draw_pulse_ring(self, c: tk.Canvas, phase: float, *,
                         base_color: str, r_start: float, r_end: float,
                         visible_for: float, start_alpha: float,
                         width: int) -> None:
        """Draw one frame of a radar-ping ring on canvas *c*.

        ``visible_for`` is the fraction of the cycle the ring is
        visible (the rest is hold-invisible time). ``start_alpha`` is
        the simulated opacity at phase=0 — Tk has no real alpha so we
        blend the base color toward shell_panel to fake it.
        """
        if phase >= visible_for:
            return  # hold-invisible portion of the cycle
        # Normalize phase into [0, 1] within the visible window.
        p = phase / visible_for
        # Ease-out cubic: starts fast, decelerates as it expands. The
        # eye reads this as a 'ping' more than a 'pulse'.
        eased = 1 - (1 - p) ** 3
        r = r_start + (r_end - r_start) * eased
        # Fade alpha linearly within the visible window. The
        # blend-toward-bg approximation is good enough that the ring
        # appears to fade out smoothly against the white panel.
        alpha = start_alpha * (1 - p)
        ring_color = _blend(PAL["shell_panel"], base_color, alpha)
        c.create_oval(
            32 - r, 32 - r, 32 + r, 32 + r,
            outline=ring_color, fill="", width=width,
            tags="pulse",
        )

    # ─── polling ───────────────────────────────────────────────────────
    def _poll(self) -> None:
        try:
            self._refresh_from_state()
        except Exception:
            log.exception("GUI refresh failed")
        finally:
            if not self.shutdown_event.is_set():
                self.root.after(self.POLL_MS, self._poll)

    def _refresh_from_state(self) -> None:
        snap = self.state.snapshot()

        view = _hero_view(snap["connection"], snap["activity"])

        # Hero — only repaint static layers when something material changed.
        if self._last_hero is None or self._last_hero["glyph"] != view["glyph"] \
                or self._last_hero["color"] != view["color"]:
            self._paint_hero_static(view)
        self._last_hero = view

        self.hero_title.configure(text=view["title"])

        # Subtitle row — three-label composition lets the server hostname
        # render as a clickable accent-blue link in the middle, matching
        # the design's "Linked to autoalert.pro as RykersLaptop". Each
        # branch sets all three labels (left, link, right); empty text
        # ('' ) collapses a label so it doesn't show.
        if snap["activity"] == ACT_UPLOADING and snap["activity_detail"]:
            self.hero_subtitle_left.configure(text=snap["activity_detail"])
            self.hero_subtitle_link.configure(text="")
            self.hero_subtitle_right.configure(text="")
        elif snap["connection"] == CONN_ONLINE:
            host = snap["hostname"] or snap["device_name"] or "this device"
            server = _strip_scheme(snap["server_url"]) or "the server"
            self.hero_subtitle_left.configure(text="Linked to ")
            self.hero_subtitle_link.configure(text=server)
            self.hero_subtitle_right.configure(text=f" as {host}")
        elif snap["connection"] == CONN_AUTH_FAILED:
            self.hero_subtitle_left.configure(
                text="Token rejected — paste a fresh pairing code to continue.",
            )
            self.hero_subtitle_link.configure(text="")
            self.hero_subtitle_right.configure(text="")
        elif snap["connection"] in (CONN_CONNECTING, CONN_STARTING, CONN_DISCONNECTED):
            server = _strip_scheme(snap["server_url"]) or "server"
            self.hero_subtitle_left.configure(text="Reaching ")
            self.hero_subtitle_link.configure(text=server)
            self.hero_subtitle_right.configure(text="…")
        else:
            self.hero_subtitle_left.configure(text="")
            self.hero_subtitle_link.configure(text="")
            self.hero_subtitle_right.configure(text="")

        # Version line in the header
        bits = []
        if snap["version"]:
            bits.append(f"v{snap['version']}")
        if snap["hwid_short"]:
            bits.append(snap["hwid_short"])
        self.version_label.configure(text=" · ".join(bits))

        # Chips — rebuild on each repaint. Cheap (<5 widgets) and saves
        # us from having to diff their internals.
        for w in self._chip_widgets:
            w.destroy()
        self._chip_widgets.clear()

        # Queue depth — the agent's single-job invariant means this is
        # 0 or 1 in practice. AgentState doesn't expose it yet; until it
        # does, render "Queue 0" idle and "Queue 1" while uploading so
        # the chip pair matches the design mockup. Wire to a real
        # counter when AgentState.queue_depth lands.
        queue_chip_text = "Queue 1" if snap["activity"] == ACT_UPLOADING else "Queue 0"

        if snap["activity"] == ACT_UPLOADING:
            self._chip_widgets.append(
                self._make_chip(self.chips_row, "Uploading", dot_color=view["color"])
            )
            self._chip_widgets.append(
                self._make_chip(self.chips_row, queue_chip_text)
            )
        elif snap["connection"] == CONN_ONLINE:
            self._chip_widgets.append(
                self._make_chip(self.chips_row, "Idle", dot_color=PAL["ok"])
            )
            self._chip_widgets.append(
                self._make_chip(self.chips_row, queue_chip_text)
            )
        elif snap["connection"] in (CONN_CONNECTING, CONN_STARTING, CONN_DISCONNECTED):
            self._chip_widgets.append(
                self._make_chip(self.chips_row, "Reconnecting", dot_color=PAL["warn"])
            )
        elif snap["connection"] == CONN_AUTH_FAILED:
            self._chip_widgets.append(
                self._make_chip(self.chips_row, "Auth failed", dot_color=PAL["err"])
            )

        for chip in self._chip_widgets:
            chip.pack(side="left", padx=(0, 6))

        # Progress bar — shown only while uploading. We let the network
        # code push a 0..1 value into activity_detail like "row 3/12" and
        # parse it here; no progress info ⇒ show an indeterminate-looking
        # full-width bar at low opacity. (Cheap fallback; replace with a
        # real fraction once the upload path emits one.)
        if snap["activity"] == ACT_UPLOADING:
            frac = _parse_progress(snap["activity_detail"])
            if frac is None:
                self.progress.configure(mode="indeterminate")
                self.progress.start()
            else:
                self.progress.stop()
                self.progress.configure(mode="determinate")
                self.progress.set(frac)
            if not self.progress.winfo_ismapped():
                self.progress.pack(fill="x", pady=(10, 0))
        else:
            if self.progress.winfo_ismapped():
                self.progress.stop()
                self.progress.pack_forget()

        # Footer CTA flip — red "Re-pair device" when auth_failed, else
        # the standard blue "Open Dashboard".
        if snap["connection"] == CONN_AUTH_FAILED:
            self.primary_btn.configure(
                text="Re-pair device",
                fg_color=PAL["err"], hover_color="#a8261f",
            )
            self.notice.configure(
                text="Token expired — the agent paused uploads.",
                text_color=PAL["err"],
            )
        else:
            self.primary_btn.configure(
                text="Open dashboard  →",
                fg_color=PAL["accent"], hover_color=PAL["accent_hover"],
            )
            tone = PAL["accent"] if snap["activity"] == ACT_UPLOADING else PAL["text_dim"]
            self.notice.configure(
                text="Keep open while uploads run.", text_color=tone,
            )

        # Log tail — diff-based to avoid flickering the textbox every poll.
        lines = snap["log_lines"]
        current = self.log_box.get("1.0", "end-1c").splitlines()
        if lines != current:
            self.log_box.configure(state="normal")
            self.log_box.delete("1.0", "end")
            self.log_box.insert("end", "\n".join(lines))
            self.log_box.see("end")
            self.log_box.configure(state="disabled")

        # Pairing handshake — open the dialog exactly once per request.
        if snap["needs_pairing_code"] and not self._pairing_dialog_open:
            self._pairing_dialog_open = True
            self._open_pairing_dialog()

    # ─── pairing modal ─────────────────────────────────────────────────
    def _open_pairing_dialog(self) -> None:
        dialog = ctk.CTkToplevel(self.root)
        dialog.title("Pair this device")
        dialog.geometry("420x300")
        dialog.resizable(False, False)
        dialog.configure(fg_color=PAL["shell_bg"])
        dialog.transient(self.root)
        dialog.grab_set()

        # Top accent rail to anchor the dialog to the brand.
        tk.Frame(dialog, bg=PAL["accent"], height=3).pack(side="top", fill="x")

        ctk.CTkLabel(
            dialog, text="Pair this device",
            text_color=PAL["text"], font=(self._font, 16, "bold"),
        ).pack(pady=(22, 4))
        ctk.CTkLabel(
            dialog,
            text="Open autoalert.pro → Settings → Download the agent. "
                 "Copy the pairing code shown there and paste it below.",
            text_color=PAL["text_muted"], font=(self._font, 11),
            wraplength=360, justify="center",
        ).pack(padx=20, pady=(0, 16))

        code_var = tk.StringVar()
        code_entry = ctk.CTkEntry(
            dialog, textvariable=code_var,
            width=240, height=38, corner_radius=8,
            fg_color=PAL["shell_panel"], border_color=PAL["shell_border_strong"],
            text_color=PAL["text"], font=(self._mono, 14),
            justify="center", placeholder_text="Pairing code",
        )
        code_entry.pack()
        code_entry.focus_set()

        error_label = ctk.CTkLabel(
            dialog, text="", text_color=PAL["err"],
            font=(self._font, 10),
        )
        error_label.pack(pady=(6, 0))

        def submit() -> None:
            code = code_var.get().strip()
            if not code:
                error_label.configure(text="Enter a pairing code.")
                return
            self._pairing_dialog_open = False
            dialog.destroy()
            self.state.provide_pairing_code(code)

        def cancel() -> None:
            self._pairing_dialog_open = False
            dialog.destroy()
            self.state.provide_pairing_code(None)
            self._on_close()

        btn_row = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_row.pack(pady=18)
        ctk.CTkButton(
            btn_row, text="Cancel", command=cancel,
            fg_color="transparent", hover_color=PAL["shell_sunken"],
            text_color=PAL["text"], corner_radius=7,
            border_width=1, border_color=PAL["shell_border_strong"],
            width=100, height=32,
        ).pack(side="left", padx=6)
        ctk.CTkButton(
            btn_row, text="Pair", command=submit,
            fg_color=PAL["accent"], hover_color=PAL["accent_hover"],
            text_color="#ffffff", corner_radius=7,
            font=(self._font, 11, "bold"),
            width=100, height=32,
        ).pack(side="left", padx=6)

        dialog.bind("<Return>", lambda _e: submit())
        dialog.bind("<Escape>", lambda _e: cancel())
        dialog.protocol("WM_DELETE_WINDOW", cancel)

    # ─── actions ───────────────────────────────────────────────────────
    def _primary_action(self) -> None:
        """Primary CTA: opens the dashboard normally, but in auth_failed
        state it instead triggers the pairing flow so the user can
        recover without leaving the window."""
        snap = self.state.snapshot()
        if snap["connection"] == CONN_AUTH_FAILED:
            if not self._pairing_dialog_open:
                self._pairing_dialog_open = True
                self._open_pairing_dialog()
            return
        self._open_dashboard()

    def _open_dashboard(self) -> None:
        url = self.state.server_url or "https://autoalert.pro"
        try:
            webbrowser.open(url)
        except Exception:
            log.warning("Failed to launch browser for %s", url, exc_info=True)

    def _open_log_file(self) -> None:
        """Stub — wire to the agent's actual log file path. Falls back
        to a no-op if the agent isn't writing to a known path."""
        try:
            from agent.config import LOG_PATH  # type: ignore
        except Exception:
            LOG_PATH = None
        if not LOG_PATH:
            return
        try:
            webbrowser.open(f"file://{LOG_PATH}")
        except Exception:
            log.debug("open log file failed", exc_info=True)

    def _on_close(self) -> None:
        self.shutdown_event.set()
        self.state.provide_pairing_code(None)
        try:
            self.root.after(50, self.root.destroy)
        except Exception:
            pass

    # ─── entry ─────────────────────────────────────────────────────────
    def run(self) -> None:
        """Block on the GUI mainloop. Returns when the window closes."""
        self.root.mainloop()


# ─── module-level helpers ───────────────────────────────────────────────

def _strip_scheme(url: str) -> str:
    """'https://autoalert.pro' → 'autoalert.pro' for display."""
    if not url:
        return ""
    for prefix in ("https://", "http://", "wss://", "ws://"):
        if url.startswith(prefix):
            return url[len(prefix):].rstrip("/")
    return url.rstrip("/")


def _to_http(url: str) -> str:
    """Convert wss:// → https://, ws:// → http://. Anything else passes
    through. Used by the sessions poller so we can reuse the WebSocket
    server URL for plain HTTP GETs."""
    if not url:
        return url
    if url.startswith("wss://"):
        return "https://" + url[len("wss://"):]
    if url.startswith("ws://"):
        return "http://" + url[len("ws://"):]
    return url


def _trim_service_prefix(label: str, name: str) -> str:
    """'YouTube connected' → 'connected' when the row is already named
    YouTube. Returns the original label if no prefix match — we'd rather
    show duplicate text than swallow a useful status word."""
    if not label or not name:
        return label
    lower_label = label.lower()
    lower_name = name.lower()
    if lower_label.startswith(lower_name):
        return label[len(name):].lstrip(" :·-").lower() or label
    return label


def _parse_progress(detail: str) -> float | None:
    """Pull a progress fraction out of an activity_detail string.

    Recognizes patterns like 'row 3/12', 'chunk 14 of 32', '43%'.
    Returns a float in [0, 1] or None if nothing parseable is found.
    """
    if not detail:
        return None
    import re
    # "43%"
    m = re.search(r"(\d+)\s*%", detail)
    if m:
        return max(0.0, min(1.0, int(m.group(1)) / 100))
    # "row 3/12" or "3 of 12"
    m = re.search(r"(\d+)\s*(?:/|of)\s*(\d+)", detail)
    if m:
        num = int(m.group(1)); den = int(m.group(2))
        if den > 0:
            return max(0.0, min(1.0, num / den))
    return None


def _now_ms() -> int:
    """Monotonic-ish ms clock for the pulse. time.monotonic is fine —
    Tk doesn't need wall-clock precision and monotonic avoids the
    'system clock changed' jump."""
    import time
    return int(time.monotonic() * 1000)


# ─── logging bridge ─────────────────────────────────────────────────────

class StateLogHandler(logging.Handler):
    """Logging handler that pushes formatted records into AgentState.log_lines.

    Installed by main.py when the GUI is active so the on-screen log box
    mirrors what would otherwise only land in agent.log.
    """

    def __init__(self, state: AgentState) -> None:
        super().__init__(level=logging.INFO)
        self.state = state

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
            self.state.append_log(line)
        except Exception:
            pass
