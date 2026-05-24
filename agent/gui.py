"""CustomTkinter GUI for the agent.

Runs on the main thread. The network code runs on a daemon thread and
pushes status updates into an ``AgentState`` instance; we poll it via
``Tk.after()`` every 500ms and repaint the relevant widgets.

Color palette is a sRGB approximation of the website's oklch design
tokens (templates/base.html) so the agent visually matches autoalert.pro.
"""
from __future__ import annotations

import logging
import threading
import tkinter as tk
import tkinter.font
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


# ---- Palette: sRGB approximations of the web app's oklch tokens. -------
# These match templates/base.html so the agent visually belongs to the
# same product. Tweak with care — keep contrast WCAG-AA.
PAL = {
    "shell_bg":           "#f7f6f3",   # oklch(0.985 0.003 80)
    "shell_panel":        "#ffffff",   # oklch(1 0 0)
    "shell_sunken":       "#f0eeea",   # oklch(0.965 0.004 80)
    "shell_border":       "#dcd9d3",   # oklch(0.91 0.005 80)
    "shell_border_strong": "#c0bdb6",  # oklch(0.83 0.006 80)
    "text":               "#1f242e",   # oklch(0.22 0.012 250)
    "text_muted":         "#6e7382",   # oklch(0.5 0.012 250)
    "text_dim":           "#9097a4",   # oklch(0.65 0.008 250)
    "accent":             "#2f6fd3",   # oklch(0.58 0.15 245)
    "accent_hover":       "#3e7fdf",   # oklch(0.64 0.16 245)
    "accent_soft":        "#e1ecfa",   # accent at 12% over white
    "ok":                 "#239e62",   # oklch(0.62 0.16 150)
    "warn":               "#c98a26",   # oklch(0.68 0.14 80)
    "err":                "#c4332c",   # oklch(0.55 0.20 25)
}


# Map connection status -> (dot color, label text)
_CONN_VIEW = {
    CONN_STARTING:     (PAL["text_dim"], "Starting up…"),
    CONN_CONNECTING:   (PAL["warn"],     "Connecting…"),
    CONN_ONLINE:       (PAL["ok"],       "Connected"),
    CONN_DISCONNECTED: (PAL["warn"],     "Reconnecting…"),
    CONN_AUTH_FAILED:  (PAL["err"],      "Re-pair required"),
    CONN_STOPPED:      (PAL["text_dim"], "Stopped"),
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


class AgentGUI:
    """Main agent window. One instance per process."""

    POLL_MS = 500
    LOG_FONT_FAMILIES = ("Geist Mono", "JetBrains Mono", "Cascadia Mono",
                         "Consolas", "Menlo", "Courier New")

    def __init__(self, state: AgentState, shutdown_event: threading.Event) -> None:
        self.state = state
        self.shutdown_event = shutdown_event
        self._pairing_dialog_open = False

        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("blue")

        self.root = ctk.CTk()
        self.root.title("Daily Life Distributor — Agent")
        self.root.geometry("520x560")
        self.root.minsize(440, 480)
        self.root.configure(fg_color=PAL["shell_bg"])

        # Closing the window means "stop the agent" — same semantics as Ctrl+C
        # in CLI mode.
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_ui()
        # Kick off the polling loop.
        self.root.after(self.POLL_MS, self._poll)

    # ---- layout --------------------------------------------------------
    def _build_ui(self) -> None:
        font_family = _pick_font_family()
        mono_family = self._pick_mono_family()

        # Header — brand mark + title. Mirror the website's sb-brand-mark:
        # a small accent-colored rounded square on the left so the agent
        # window has the same visual signature as the dashboard sidebar.
        header = ctk.CTkFrame(
            self.root, fg_color=PAL["shell_panel"],
            corner_radius=0,
            height=68,
        )
        header.pack(fill="x", side="top")
        header.pack_propagate(False)
        header.configure(border_width=1, border_color=PAL["shell_border"])

        # Accent brand mark (filled rounded square).
        brand_mark = ctk.CTkFrame(
            header, fg_color=PAL["accent"], corner_radius=8,
            width=34, height=34,
        )
        brand_mark.place(x=18, y=17)
        brand_mark.pack_propagate(False)

        ctk.CTkLabel(
            header, text="Daily Life", text_color=PAL["text"],
            font=(font_family, 15, "bold"),
        ).place(x=64, y=14)
        ctk.CTkLabel(
            header, text="DISTRIBUTOR", text_color=PAL["accent"],
            font=(font_family, 10, "bold"),
        ).place(x=64, y=34)
        # Agent badge — accent-soft pill on the right.
        agent_badge = ctk.CTkLabel(
            header, text="  AGENT  ", text_color=PAL["accent"],
            font=(font_family, 10, "bold"),
            fg_color=PAL["accent_soft"], corner_radius=6,
        )
        agent_badge.place(relx=1.0, x=-22, y=24, anchor="ne")

        body = ctk.CTkFrame(self.root, fg_color=PAL["shell_bg"], corner_radius=0)
        body.pack(fill="both", expand=True, padx=18, pady=18)

        # --- Status card ------------------------------------------------
        # Wrap in a horizontal container so we can paint an accent-colored
        # 3px vertical stripe down the left edge (mirrors the website's
        # .section-header treatment — accent bar = "this is the active
        # surface"). The stripe color flips green when CONN_ONLINE in
        # _refresh_from_state so the card reads its own status at a glance.
        card_wrap = ctk.CTkFrame(body, fg_color="transparent")
        card_wrap.pack(fill="x")
        self.status_stripe = ctk.CTkFrame(
            card_wrap, fg_color=PAL["text_dim"],
            width=4, corner_radius=2,
        )
        self.status_stripe.pack(side="left", fill="y", padx=(0, 0), pady=2)

        self.status_card = ctk.CTkFrame(
            card_wrap, fg_color=PAL["shell_panel"],
            border_width=1, border_color=PAL["shell_border"],
            corner_radius=12,
        )
        self.status_card.pack(side="left", fill="x", expand=True, padx=(8, 0))

        inner = ctk.CTkFrame(self.status_card, fg_color="transparent")
        inner.pack(fill="x", padx=18, pady=16)

        # Connection row
        conn_row = ctk.CTkFrame(inner, fg_color="transparent")
        conn_row.pack(fill="x")
        self.conn_dot = tk.Canvas(
            conn_row, width=12, height=12, highlightthickness=0,
            bg=PAL["shell_panel"],
        )
        self.conn_dot.pack(side="left", padx=(0, 10))
        self._conn_dot_id = self.conn_dot.create_oval(
            2, 2, 10, 10, fill=PAL["text_dim"], outline="",
        )
        self.conn_label = ctk.CTkLabel(
            conn_row, text="Starting up…", text_color=PAL["text"],
            font=(font_family, 14, "bold"),
        )
        self.conn_label.pack(side="left")

        self.server_label = ctk.CTkLabel(
            inner, text="", text_color=PAL["text_muted"],
            font=(font_family, 11),
        )
        self.server_label.pack(anchor="w", pady=(2, 12))

        # Divider
        ctk.CTkFrame(inner, fg_color=PAL["shell_border"],
                     height=1).pack(fill="x", pady=4)

        # Activity row
        act_wrap = ctk.CTkFrame(inner, fg_color="transparent")
        act_wrap.pack(fill="x", pady=(12, 0))
        ctk.CTkLabel(
            act_wrap, text="ACTIVITY", text_color=PAL["accent"],
            font=(font_family, 9, "bold"),
        ).pack(anchor="w")
        self.activity_label = ctk.CTkLabel(
            act_wrap, text="Idle", text_color=PAL["text"],
            font=(font_family, 13),
        )
        self.activity_label.pack(anchor="w", pady=(2, 0))
        self.activity_detail = ctk.CTkLabel(
            act_wrap, text="", text_color=PAL["text_muted"],
            font=(font_family, 11),
        )
        self.activity_detail.pack(anchor="w")

        # Device identity strip
        ident_wrap = ctk.CTkFrame(inner, fg_color="transparent")
        ident_wrap.pack(fill="x", pady=(14, 0))
        self.device_label = ctk.CTkLabel(
            ident_wrap, text="", text_color=PAL["text_dim"],
            font=(font_family, 10),
        )
        self.device_label.pack(anchor="w")

        # --- Keep-open notice ------------------------------------------
        self.notice = ctk.CTkLabel(
            body,
            text="Keep this window open while uploads are running. "
                 "Closing it stops the agent.",
            text_color=PAL["text_muted"],
            font=(font_family, 10),
            wraplength=460, justify="left",
        )
        self.notice.pack(fill="x", pady=(12, 0), anchor="w")

        # --- Activity log -----------------------------------------------
        log_label = ctk.CTkLabel(
            body, text="ACTIVITY LOG", text_color=PAL["accent"],
            font=(font_family, 9, "bold"),
        )
        log_label.pack(anchor="w", pady=(16, 4))

        log_frame = ctk.CTkFrame(
            body, fg_color=PAL["shell_panel"],
            border_width=1, border_color=PAL["shell_border"],
            corner_radius=10,
        )
        log_frame.pack(fill="both", expand=True)

        self.log_box = ctk.CTkTextbox(
            log_frame,
            fg_color=PAL["shell_panel"],
            text_color=PAL["text_muted"],
            border_width=0,
            font=(mono_family, 10),
            wrap="none",
        )
        self.log_box.pack(fill="both", expand=True, padx=12, pady=12)
        self.log_box.configure(state="disabled")

        # --- Footer actions ---------------------------------------------
        footer = ctk.CTkFrame(self.root, fg_color=PAL["shell_panel"],
                              corner_radius=0, height=46)
        footer.pack(fill="x", side="bottom")
        footer.pack_propagate(False)
        footer.configure(border_width=1, border_color=PAL["shell_border"])

        self.open_dashboard_btn = ctk.CTkButton(
            footer, text="Open Dashboard",
            fg_color=PAL["accent"], hover_color=PAL["accent_hover"],
            text_color="#ffffff", corner_radius=8,
            font=(font_family, 11, "bold"),
            command=self._open_dashboard, height=28,
        )
        self.open_dashboard_btn.pack(side="right", padx=(8, 18), pady=9)

        self.quit_btn = ctk.CTkButton(
            footer, text="Quit",
            fg_color=PAL["shell_sunken"], hover_color=PAL["shell_border"],
            text_color=PAL["text"], corner_radius=8,
            font=(font_family, 11),
            command=self._on_close, height=28, width=72,
        )
        self.quit_btn.pack(side="right", pady=9)

    def _pick_mono_family(self) -> str:
        try:
            installed = set(tkinter.font.families())
        except Exception:
            installed = set()
        for f in self.LOG_FONT_FAMILIES:
            if f in installed:
                return f
        return "TkFixedFont"

    # ---- polling -------------------------------------------------------
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

        # Connection indicator + left-edge stripe color (reads its own
        # status at a glance: green=online, blue=connecting, red=auth,
        # gray=stopped/starting).
        color, label = _CONN_VIEW.get(
            snap["connection"], (PAL["text_dim"], snap["connection"])
        )
        # Stripe uses accent when connecting (in-progress) so it picks
        # up the website's primary action color during the most common
        # transient state.
        stripe_color = (
            PAL["accent"] if snap["connection"] == CONN_CONNECTING else color
        )
        try:
            self.status_stripe.configure(fg_color=stripe_color)
        except Exception:
            pass
        self.conn_dot.itemconfig(self._conn_dot_id, fill=color)
        self.conn_label.configure(text=label)

        server = snap["server_url"] or "—"
        self.server_label.configure(text=server)

        # Activity
        if snap["activity"] == ACT_UPLOADING:
            self.activity_label.configure(
                text="Uploading", text_color=PAL["accent"],
            )
        elif snap["activity"] == ACT_IDLE:
            self.activity_label.configure(
                text="Idle", text_color=PAL["text"],
            )
        else:
            self.activity_label.configure(
                text=snap["activity"].title(), text_color=PAL["text"],
            )
        self.activity_detail.configure(text=snap["activity_detail"] or "")

        # Identity strip
        bits = []
        if snap["hostname"]:
            bits.append(snap["hostname"])
        if snap["hwid_short"]:
            bits.append(f"hwid {snap['hwid_short']}")
        if snap["version"]:
            bits.append(f"v{snap['version']}")
        self.device_label.configure(text=" · ".join(bits))

        # Log tail — only rewrite when the line count actually changed,
        # otherwise we flicker the textbox every 500ms.
        lines = snap["log_lines"]
        current = self.log_box.get("1.0", "end-1c").splitlines()
        if lines != current:
            self.log_box.configure(state="normal")
            self.log_box.delete("1.0", "end")
            self.log_box.insert("end", "\n".join(lines))
            self.log_box.see("end")
            self.log_box.configure(state="disabled")

        # Show / hide the keep-open notice (highlight it during uploads).
        if snap["activity"] == ACT_UPLOADING:
            self.notice.configure(text_color=PAL["accent"])
        else:
            self.notice.configure(text_color=PAL["text_muted"])

        # Pairing dialog handshake — open exactly once per request.
        if snap["needs_pairing_code"] and not self._pairing_dialog_open:
            self._pairing_dialog_open = True
            self._open_pairing_dialog()

    # ---- pairing modal ------------------------------------------------
    def _open_pairing_dialog(self) -> None:
        dialog = ctk.CTkToplevel(self.root)
        dialog.title("Pair this device")
        dialog.geometry("420x280")
        dialog.resizable(False, False)
        dialog.configure(fg_color=PAL["shell_bg"])
        dialog.transient(self.root)
        dialog.grab_set()

        font_family = _pick_font_family()
        ctk.CTkLabel(
            dialog, text="Pair this device",
            text_color=PAL["text"], font=(font_family, 16, "bold"),
        ).pack(pady=(22, 4))
        ctk.CTkLabel(
            dialog,
            text="Open autoalert.pro → Settings → Download the agent. "
                 "Copy the pairing code shown there and paste it below.",
            text_color=PAL["text_muted"], font=(font_family, 11),
            wraplength=360, justify="center",
        ).pack(padx=20, pady=(0, 14))

        code_var = tk.StringVar()
        code_entry = ctk.CTkEntry(
            dialog, textvariable=code_var,
            width=240, height=36, corner_radius=8,
            fg_color=PAL["shell_panel"], border_color=PAL["shell_border_strong"],
            text_color=PAL["text"], font=("TkFixedFont", 14),
            justify="center", placeholder_text="Pairing code",
        )
        code_entry.pack()
        code_entry.focus_set()

        error_label = ctk.CTkLabel(
            dialog, text="", text_color=PAL["err"],
            font=(font_family, 10),
        )
        error_label.pack(pady=(6, 0))

        def submit():
            code = code_var.get().strip()
            if not code:
                error_label.configure(text="Enter a pairing code.")
                return
            self._pairing_dialog_open = False
            dialog.destroy()
            self.state.provide_pairing_code(code)

        def cancel():
            self._pairing_dialog_open = False
            dialog.destroy()
            self.state.provide_pairing_code(None)
            # Cancelling pairing means stopping the agent.
            self._on_close()

        btn_row = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_row.pack(pady=18)
        ctk.CTkButton(
            btn_row, text="Cancel", command=cancel,
            fg_color=PAL["shell_sunken"], hover_color=PAL["shell_border"],
            text_color=PAL["text"], corner_radius=8, width=100,
        ).pack(side="left", padx=6)
        ctk.CTkButton(
            btn_row, text="Pair", command=submit,
            fg_color=PAL["accent"], hover_color=PAL["accent_hover"],
            text_color="#ffffff", corner_radius=8, width=100,
        ).pack(side="left", padx=6)

        dialog.bind("<Return>", lambda _e: submit())
        dialog.bind("<Escape>", lambda _e: cancel())
        dialog.protocol("WM_DELETE_WINDOW", cancel)

    # ---- actions ------------------------------------------------------
    def _open_dashboard(self) -> None:
        url = self.state.server_url or "https://autoalert.pro"
        try:
            webbrowser.open(url)
        except Exception:
            log.warning("Failed to launch browser for %s", url, exc_info=True)

    def _on_close(self) -> None:
        # Tell the network thread to stop, but don't block waiting on it
        # — the daemon thread will be killed when the process exits.
        self.shutdown_event.set()
        # If we're blocked on a pairing prompt, unblock it.
        self.state.provide_pairing_code(None)
        try:
            self.root.after(50, self.root.destroy)
        except Exception:
            pass

    # ---- entry --------------------------------------------------------
    def run(self) -> None:
        """Block on the GUI mainloop. Returns when the window closes."""
        self.root.mainloop()


# ---- logging bridge -----------------------------------------------------

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
