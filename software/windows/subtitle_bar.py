"""Always-on-top live subtitle bar, adapted from Live Subtitles' overlay.py (github.com/ted91/teams-subtitles).

Appears when a live recording starts and hides a few seconds after it stops. Shows what's being said
(from live_transcriber) -- translated to English when a spoken language is set (live_translator).
Stable words are white, words that may still change are grey; your own microphone's lines start "You:".

* Drag with the left mouse button; Ctrl + mouse wheel changes the font size; Esc hides the bar.
* Right-click: font size, transparency, lines shown, translation language, show original text,
  speaker labels, hide, stop recording, open the dashboard.
Font size, transparency, lines and position are remembered (settings.json, subtitle_* keys).

Tk runs in its own thread for the whole app lifetime (one Tk instance, shown/hidden as needed); every
other thread talks to it only through a queue, since Tk objects must stay on the thread that made them.
"""
import logging
import queue
import threading
import time

log = logging.getLogger("subtitle_bar")

FG_STABLE, FG_DIM, FG_ME, FG_ORIG, FG_STATUS, BG = "#ffffff", "#9aa0a6", "#7fd4ff", "#b8bec4", "#6f767d", "#101010"
HOLD_S = 6.0           # finished lines stay this long
HIDE_AFTER_STOP_S = 4.0
LIVE = 10 ** 6         # sort key of an utterance's still-growing line


def _setting(key, default):
    import settings
    return settings.get_all().get(key, default)


class SubtitleBar:
    def __init__(self, status_fn=None, on_stop=None, on_open_dashboard=None):
        self._q = queue.Queue()
        self._status_fn, self._on_stop, self._on_open = status_fn, on_stop, on_open_dashboard
        self._thread = threading.Thread(target=self._run, daemon=True, name="subtitle-bar")

    # ---- thread-safe API ------------------------------------------------------------------------
    def start(self):
        self._thread.start()

    def show(self):
        if _setting("subtitle_bar", True):
            self._q.put(("show",))

    def hide(self, after_s: float = 0.0):
        self._q.put(("hide", time.monotonic() + after_s))

    def on_update(self, update):
        self._q.put(("update", update))

    # ---- Tk thread ------------------------------------------------------------------------------
    def _run(self):
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)   # crisp text on scaled displays
        except Exception:
            pass
        import tkinter as tk
        import tkinter.font as tkfont
        self.tk = tk
        r = self.root = tk.Tk()
        r.withdraw()
        r.title("Clicky subtitles")
        r.overrideredirect(True)
        r.attributes("-topmost", True)
        r.attributes("-alpha", float(_setting("subtitle_opacity", 0.85)))
        r.configure(bg=BG)
        sw, sh = r.winfo_screenwidth(), r.winfo_screenheight()
        self.w = int(sw * 0.7)
        self.x = int(_setting("subtitle_bar_x", (sw - self.w) // 2))
        self.y = int(_setting("subtitle_bar_y", sh - 140))       # bottom edge of the bar
        self.font_size = int(_setting("subtitle_font_size", 24))
        self.font = tkfont.Font(root=r, family="Segoe UI", size=self.font_size)
        self.orig_font = tkfont.Font(root=r, family="Segoe UI", size=max(9, self.font_size // 2))
        self.small = tkfont.Font(root=r, family="Segoe UI", size=9)
        self.text = tk.Text(r, wrap="word", bg=BG, fg=FG_STABLE, bd=0, highlightthickness=0, font=self.font,
                            height=1, padx=14, pady=8, cursor="fleur", state="disabled", spacing1=2, spacing3=2)
        for tag, color in (("stable", FG_STABLE), ("dim", FG_DIM), ("me", FG_ME), ("status", FG_STATUS)):
            self.text.tag_configure(tag, foreground=color)
        self.text.tag_configure("orig", foreground=FG_ORIG, font=self.orig_font)
        self.text.pack(fill="x")
        self.status = tk.Label(r, text="", bg=BG, fg=FG_STATUS, font=self.small, anchor="e", padx=10)
        self.status.pack(fill="x")
        for wdg in (r, self.text, self.status):
            wdg.bind("<ButtonPress-1>", self._drag_start)
            wdg.bind("<B1-Motion>", self._drag)
            wdg.bind("<ButtonRelease-1>", lambda e: self._save_position())
            wdg.bind("<Button-3>", self._menu)
            wdg.bind("<Control-MouseWheel>", lambda e: self._font(+2 if e.delta > 0 else -2))
        r.bind("<Escape>", lambda e: self._do_hide())
        self.entries, self.utt_first = {}, {}
        self.visible, self.hide_at = False, None
        self.menu_open = False
        r.after(30, self._tick)
        r.mainloop()

    def _tick(self):
        changed = False
        try:
            while True:
                cmd = self._q.get_nowait()
                if cmd[0] == "show":
                    self.hide_at = None
                    self.entries.clear()
                    self.utt_first.clear()
                    if not self.visible:
                        self.visible = True
                        self.root.deiconify()
                        self.root.attributes("-topmost", True)
                    changed = True
                elif cmd[0] == "hide":
                    self.hide_at = cmd[1]
                elif cmd[0] == "update":
                    self._apply(cmd[1])
                    changed = True
        except queue.Empty:
            pass
        if self.hide_at is not None and time.monotonic() >= self.hide_at:
            self._do_hide()
        if self.visible:
            self._render()   # every tick while shown: keeps the REC timer and line expiry current
        self.root.after(100 if self.visible else 250, self._tick)

    def _do_hide(self):
        self.hide_at = None
        if self.visible:
            self.visible = False
            self.root.withdraw()

    # ---- content (same entry model as Live Subtitles' overlay) -----------------------------------
    def _apply(self, u):
        from live_translator import clean_subtitle
        src, utt, now = u["source"], u["utt_id"], time.monotonic()
        self.utt_first.setdefault((src, utt), now)
        tr = u.get("translation")
        if tr:
            for i, s in enumerate(tr["sentences"]):
                s = clean_subtitle(s)                 # "Yeah, yeah, yeah." lines are dropped
                if s:
                    self.entries[(src, utt, i)] = dict(stable=s, dim="", orig="", t=now)
            live_stable, live_dim = tr["stable"], tr["dim"]
        else:
            live_stable, live_dim = u["stable"], u["dim"]
            final_text = clean_subtitle(u["stable"]) if u["final"] else ""
            if final_text:
                self.entries[(src, utt, 0)] = dict(stable=final_text, dim="", orig="", t=now)
        if not clean_subtitle((live_stable + " " + live_dim).strip()):
            live_stable = live_dim = ""               # a draft that is only filler isn't worth a line
        orig = (u["stable"] + (" " + u["dim"] if u["dim"] else "")).strip() if tr else ""
        live = (src, utt, LIVE)
        if u["final"] or not (live_stable or live_dim):
            self.entries.pop(live, None)
            if tr and u["final"] and orig:          # keep the original under the last English sentence
                keys = sorted(k for k in self.entries if k[:2] == (src, utt))
                if keys:
                    self.entries[keys[-1]]["orig"] = orig
        else:
            self.entries[live] = dict(stable=live_stable, dim=live_dim, orig=orig, t=now)
        for k in [k for k in self.entries if k[:2] == (src, utt)]:
            self.entries[k]["utt_final"] = u["final"]

    def _render(self):
        import settings
        cfg = settings.get_all()
        size = int(cfg.get("subtitle_font_size", self.font_size))
        if size != self.font_size:                       # changed on the Settings page
            self.font_size = size
            self.font.configure(size=size)
            self.orig_font.configure(size=max(9, size // 2))
        now = time.monotonic()
        for k in [k for k, v in self.entries.items() if v.get("utt_final") and now - v["t"] > HOLD_S]:
            del self.entries[k]
        lines = int(cfg.get("subtitle_lines", 3))
        keys = sorted(self.entries, key=lambda k: (self.utt_first.get(k[:2], 0), k[2]))[-lines:]
        show_src, show_orig = cfg.get("subtitle_show_source", True), cfg.get("subtitle_show_original", False)
        parts = []
        for n, k in enumerate(keys):
            e = self.entries[k]
            if n:
                parts.append(("\n", "stable"))
            if show_src and k[0] == "you":
                parts.append(("You: ", "me"))
            if e["stable"]:
                parts.append((e["stable"], "stable"))
            if e["dim"]:
                parts.append((("" if not e["stable"] else " ") + e["dim"], "dim"))
            if show_orig and e.get("orig"):
                parts.append(("\n" + e["orig"], "orig"))
        if not parts:
            parts = [("Listening...", "dim")]
        self._set_text(parts)
        self.status.configure(text=self._status_text())
        self._place()

    def _status_text(self):
        import live_translator
        bits = []
        if self._status_fn:
            try:
                s = self._status_fn()
                if s.get("subtitles_only"):
                    bits.append("SUBTITLES ONLY · not recording")
                elif s.get("recording"):
                    e = int(s.get("elapsed", 0))
                    bits.append(f"● REC {e // 60}:{e % 60:02d}")
                    if s.get("mic_state") == "muted_in_call":
                        bits.append("your mic is muted - recording the meeting")
                    elif s.get("mic_state") == "muted":
                        bits.append("your mic is muted")
                elif not s.get("active"):
                    bits.append("saving...")
            except Exception:
                pass
        lang = live_translator.current_language()
        bits.append(f"{live_translator.lang_name(lang)} → English" if lang else "original language (no translation)")
        return "   |   ".join(bits)

    def _set_text(self, parts):
        t = self.text
        t.configure(state="normal")
        t.delete("1.0", "end")
        for s, tag in parts:
            t.insert("end", s, tag)
        t.update_idletasks()
        n = t.count("1.0", "end", "displaylines")
        t.configure(height=max(1, (n[0] if n else 1)), state="disabled")

    # ---- window handling ----------------------------------------------------------------------------
    def _place(self):
        self.root.update_idletasks()
        h = self.root.winfo_reqheight()
        self.root.geometry(f"{self.w}x{h}+{self.x}+{max(0, self.y - h)}")

    def _drag_start(self, e):
        self._dx, self._dy = e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y()

    def _drag(self, e):
        x, y = e.x_root - self._dx, e.y_root - self._dy
        self.x, self.y = x, y + self.root.winfo_height()
        self.root.geometry(f"+{x}+{y}")

    def _save_position(self):
        import settings
        settings.update(subtitle_bar_x=self.x, subtitle_bar_y=self.y)

    def _font(self, d):
        import settings
        self.font_size = max(10, min(80, self.font_size + d))
        self.font.configure(size=self.font_size)
        self.orig_font.configure(size=max(9, self.font_size // 2))
        settings.update(subtitle_font_size=self.font_size)
        self._render()

    def _alpha(self, d):
        import settings
        a = round(max(0.2, min(1.0, float(self.root.attributes("-alpha")) + d)), 2)
        self.root.attributes("-alpha", a)
        settings.update(subtitle_opacity=a)

    def _menu(self, e):
        import live_translator
        import settings
        tk = self.tk
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label="Bigger text (Ctrl + wheel)", command=lambda: self._font(+2))
        m.add_command(label="Smaller text", command=lambda: self._font(-2))
        m.add_command(label="More opaque", command=lambda: self._alpha(+0.1))
        m.add_command(label="More transparent", command=lambda: self._alpha(-0.1))
        lines = tk.Menu(m, tearoff=0)
        self._lines_var = tk.IntVar(master=self.root, value=int(_setting("subtitle_lines", 3)))
        for n in (1, 2, 3, 4, 5):
            lines.add_radiobutton(label=str(n), value=n, variable=self._lines_var,
                                  command=lambda n=n: settings.update(subtitle_lines=n))
        m.add_cascade(label="Lines shown", menu=lines)
        m.add_separator()
        langs = tk.Menu(m, tearoff=0)
        self._lang_var = tk.StringVar(master=self.root, value=live_translator.current_language())
        langs.add_radiobutton(label="Off - show what's said, untranslated", value="", variable=self._lang_var,
                              command=lambda: settings.update(subtitle_translate_from=""))
        for code in live_translator.available_languages():
            langs.add_radiobutton(label=f"{live_translator.lang_name(code)} → English", value=code,
                                  variable=self._lang_var,
                                  command=lambda c=code: settings.update(subtitle_translate_from=c))
        m.add_cascade(label="Translate (spoken language)", menu=langs)
        self._orig_var = tk.BooleanVar(master=self.root, value=bool(_setting("subtitle_show_original", False)))
        m.add_checkbutton(label="Also show the original text", variable=self._orig_var,
                          command=lambda: settings.update(subtitle_show_original=self._orig_var.get()))
        self._own_var = tk.BooleanVar(master=self.root, value=bool(_setting("subtitle_translate_own_mic", True)))
        m.add_checkbutton(label="Translate my own microphone too", variable=self._own_var,
                          command=lambda: settings.update(subtitle_translate_own_mic=self._own_var.get()))
        self._src_var = tk.BooleanVar(master=self.root, value=bool(_setting("subtitle_show_source", True)))
        m.add_checkbutton(label='Label my lines with "You:"', variable=self._src_var,
                          command=lambda: settings.update(subtitle_show_source=self._src_var.get()))
        m.add_separator()
        if self._on_open:
            m.add_command(label="Open Clicky dashboard", command=self._on_open)
        if self._on_stop:
            m.add_command(label="Stop recording", command=lambda: threading.Thread(target=self._on_stop, daemon=True).start())
        m.add_command(label="Hide subtitles (Esc)", command=self._do_hide)
        m.tk_popup(e.x_root, e.y_root)
