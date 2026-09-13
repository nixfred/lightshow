"""LightShow for Omarchy - native GTK4 / libadwaita desktop app.

Drives the Engine in-process. No HTTP server, no browser, no port.

Threading note: the Engine owns a worker thread that writes to the keyboard.
GTK is not thread-safe, so the only cross-thread traffic is the UI *reading*
engine.last_frame from a GLib timeout on the main loop. Nothing in here touches
widgets from the worker.

Layout rule: the page never scrolls. Every setting is on screen at once; width
is the remedy, not height. The only scroller is the favourites list, which
scrolls in place inside its own box.
"""

import re

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, GLib, Gtk, Pango  # noqa: E402

from . import effects, kbd, remote  # noqa: E402
from .engine import Engine  # noqa: E402
from .kbd import hex_rgb, rgb_hex  # noqa: E402

CSS = b"""
.zone        { border-radius: 8px; min-height: 44px; }
.zone-label  { font-size: 10px; font-weight: 800; letter-spacing: 2px;
               color: alpha(#000, .62); margin: 5px; }
.title-grad  { font-size: 19px; font-weight: 800; letter-spacing: 4px; }
.mono        { font-family: "JetBrains Mono", monospace; }
.dim         { opacity: .62; font-size: 11px; }
.fxname      { font-weight: 700; letter-spacing: 1px; }
.fxdesc      { font-size: 11px; opacity: .6; }
.kindtag     { font-size: 9px; letter-spacing: 2px; opacity: .55; }
.card-sel    { background: alpha(@accent_bg_color, .18);
               box-shadow: inset 0 0 0 1px @accent_bg_color; }
"""

RAIL_WIDTH = 460
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _clear(container):
    child = container.get_first_child()
    while child:
        nxt = child.get_next_sibling()
        container.remove(child)
        child = nxt


def _set_text_if_changed(widget, text):
    # set_text on an unchanged entry still moves the cursor, which fights the
    # user mid-edit when a sync lands while they type.
    if widget.get_text() != text:
        widget.set_text(text)


class Window(Adw.ApplicationWindow):
    def __init__(self, app, engine):
        super().__init__(application=app, title="LightShow for Omarchy")
        self.engine = engine
        self.set_default_size(1280, 820)
        self._building = False

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        title = Gtk.Label(label="LIGHTSHOW")
        title.add_css_class("title-grad")
        header.set_title_widget(title)

        self.status = Gtk.Label()
        self.status.add_css_class("dim")
        self.status.add_css_class("mono")
        header.pack_end(self.status)
        toolbar.add_top_bar(header)

        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=18,
                       margin_top=12, margin_bottom=14,
                       margin_start=18, margin_end=18)
        toolbar.set_content(body)
        self.set_content(toolbar)

        # Wide left pane: what you look at and pick from.
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14,
                       hexpand=True, vexpand=True)
        left.append(self._build_preview())
        left.append(self._build_effects())
        left.append(self._build_favorites())
        body.append(left)

        # Fixed rail on the right: every setting, stacked, always visible.
        rail = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14,
                       hexpand=False)
        rail.set_size_request(RAIL_WIDTH, -1)
        rail.append(self._build_colour())
        rail.append(self._build_controls())
        rail.append(self._build_daynight())
        body.append(rail)

        self.sync_from_engine()
        # Mirror the hardware at ~20fps. Reading last_frame is a plain list
        # read; no lock needed and a torn read would cost one stale frame.
        GLib.timeout_add(50, self._tick)

    # ---------------------------------------------------------------- ui

    def _group(self, title):
        g = Adw.PreferencesGroup(title=title)
        return g

    def _build_preview(self):
        g = self._group("Live zones")
        g.set_tooltip_text(
            "Four zone groups is everything the MS-1801 exposes. Individual "
            "keys cannot be addressed, so this is the real resolution of the "
            "hardware.")
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                      margin_top=4)
        self.zone_widgets, self.zone_css = [], []
        weights = [1, 3, 1, 2]  # rough physical proportions of each group
        for (name, _mask, label), w in zip(kbd.ZONES, weights):
            f = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                        valign=Gtk.Align.FILL, hexpand=True)
            f.add_css_class("zone")
            f.set_size_request(60 * w, 44)
            lb = Gtk.Label(label=label.upper(), halign=Gtk.Align.START,
                           valign=Gtk.Align.END, vexpand=True)
            lb.add_css_class("zone-label")
            f.append(lb)
            prov = Gtk.CssProvider()
            f.get_style_context().add_provider(
                prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            self.zone_widgets.append(f)
            self.zone_css.append(prov)
            box.append(f)
        # The labels vexpand to sit at the bottom of each zone; stop that
        # propagating up, or the strip soaks up the favourites list's height.
        box.set_vexpand(False)
        g.add(box)
        return g

    def _build_effects(self):
        g = self._group("Effects")
        flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE,
                           max_children_per_line=7, min_children_per_line=3,
                           row_spacing=6, column_spacing=6, margin_top=4,
                           homogeneous=True)
        self.fx_buttons = {}
        for e in effects.all_effects():
            btn = Gtk.Button(tooltip_text=e["desc"])
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1,
                            margin_top=5, margin_bottom=5,
                            margin_start=6, margin_end=6)
            top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            n = Gtk.Label(label=e["name"].upper(), xalign=0, hexpand=True)
            n.add_css_class("fxname")
            k = Gtk.Label(label={"hardware": "HW", "software": "SW"}.get(
                e["kind"], e["kind"].upper()), xalign=1)
            k.add_css_class("kindtag")
            top.append(n); top.append(k)
            # One line, ellipsized: the full description is the tooltip.
            d = Gtk.Label(label=e["desc"], xalign=0,
                          ellipsize=Pango.EllipsizeMode.END,
                          width_chars=16, max_width_chars=16)
            d.add_css_class("fxdesc")
            inner.append(top); inner.append(d)
            btn.set_child(inner)
            btn.connect("clicked", self._on_effect, e["name"])
            self.fx_buttons[e["name"]] = btn
            flow.append(btn)
        g.add(flow)
        return g

    def _build_colour(self):
        g = self._group("Colour")

        self.theme_row = Adw.SwitchRow(
            title="Match Omarchy theme",
            subtitle="Colours follow whatever theme you switch to")
        self.theme_row.connect("notify::active", self._on_theme_toggle)
        g.add(self.theme_row)

        self.colour_row = Adw.ActionRow(title="Custom colours")
        self.colour_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                                  spacing=6, valign=Gtk.Align.CENTER)
        add = Gtk.Button(icon_name="list-add-symbolic", valign=Gtk.Align.CENTER,
                         tooltip_text="Add a colour")
        rem = Gtk.Button(icon_name="list-remove-symbolic", valign=Gtk.Align.CENTER,
                         tooltip_text="Remove the last colour")
        add.connect("clicked", self._on_add_colour)
        rem.connect("clicked", self._on_del_colour)
        self.colour_row.add_suffix(self.colour_box)
        self.colour_row.add_suffix(add)
        self.colour_row.add_suffix(rem)
        g.add(self.colour_row)

        self.pal_flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE,
                                    max_children_per_line=16, row_spacing=4,
                                    column_spacing=4, margin_top=8,
                                    tooltip_text="Theme palette: click a swatch "
                                                 "to use it as your only colour")
        g.add(self.pal_flow)
        return g

    def _build_controls(self):
        g = self._group("Controls")

        self.speed_row = Adw.SpinRow.new_with_range(0.1, 3.0, 0.05)
        self.speed_row.set_title("Speed")
        self.speed_row.connect("notify::value", self._on_speed)
        g.add(self.speed_row)

        self.bright_row = Adw.SpinRow.new_with_range(0.05, 1.0, 0.05)
        self.bright_row.set_title("Brightness")
        self.bright_row.connect("notify::value", self._on_bright)
        g.add(self.bright_row)

        self.word_row = Adw.EntryRow(title="Scroll text")
        self.word_row.set_tooltip_text(
            "Scroll shows one travelling pulse per letter. With four zones the "
            "letters cannot be drawn as shapes, so you are watching the word's "
            "rhythm cross the keyboard.")
        go = Gtk.Button(label="Scroll it", valign=Gtk.Align.CENTER)
        go.add_css_class("suggested-action")
        go.connect("clicked", self._on_scroll)
        self.word_row.add_suffix(go)
        g.add(self.word_row)
        return g

    def _build_favorites(self):
        # A plain box, not a PreferencesGroup, so the list can take the rest of
        # the pane's height and scroll inside it.
        g = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                    vexpand=True)
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title = Gtk.Label(label="Favourites", xalign=0)
        title.add_css_class("heading")
        self.fav_entry = Gtk.Entry(placeholder_text="Name this look",
                                   hexpand=True)
        self.fav_entry.connect("activate", self._on_fav_save)
        save = Gtk.Button(label="Save current")
        save.add_css_class("suggested-action")
        save.connect("clicked", self._on_fav_save)
        head.append(title); head.append(self.fav_entry); head.append(save)
        g.append(head)

        self.fav_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE,
                                    valign=Gtk.Align.START)
        self.fav_list.add_css_class("boxed-list")
        scroller = Gtk.ScrolledWindow(vexpand=True,
                                      hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.set_min_content_height(110)
        scroller.set_child(self.fav_list)
        g.append(scroller)
        return g

    def _build_daynight(self):
        g = self._group("Day and Night")

        auto = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                       valign=Gtk.Align.CENTER)
        lbl = Gtk.Label(label="Auto-switch")
        lbl.add_css_class("dim")
        self.sched_switch = Gtk.Switch(
            valign=Gtk.Align.CENTER,
            tooltip_text="Applies whichever profile the clock falls into")
        self.sched_switch.connect("notify::active", self._on_sched)
        auto.append(lbl); auto.append(self.sched_switch)
        g.set_header_suffix(auto)

        # One row per profile: start time, load, save. Five tall rows became two.
        for which, icon in (("day", "☀"), ("night", "☾")):
            row = Adw.ActionRow(title=f"{icon}  {which.capitalize()}")
            frm = Gtk.Label(label="from")
            frm.add_css_class("dim")
            at = Gtk.Entry(width_chars=5, max_length=5, valign=Gtk.Align.CENTER,
                           tooltip_text=f"{which.capitalize()} starts at (HH:MM)")
            at.add_css_class("mono")
            at.connect("changed", self._on_time_changed)
            load = Gtk.Button(label="Load", valign=Gtk.Align.CENTER)
            save = Gtk.Button(label="Save current", valign=Gtk.Align.CENTER)
            load.connect("clicked", self._on_profile_load, which)
            save.connect("clicked", self._on_profile_save, which)
            for w in (frm, at, load, save):
                row.add_suffix(w)
            g.add(row)
            setattr(self, f"row_{which}", row)
            setattr(self, f"{which}_at", at)
        return g

    # ------------------------------------------------------------- state

    def sync_from_engine(self):
        """Repaint every widget from engine config. Guarded against feedback."""
        self._building = True
        cur = self.engine.cfg["current"]

        mode = "daemon" if isinstance(self.engine, remote.RemoteEngine) else "local"
        self.status.set_text(
            f"{kbd.theme_name()}   ·   {self.engine.kb.node}   ·   {mode}"
            + (f"   ·   {self.engine.active_profile()}"
               if self.engine.active_profile() else ""))

        for name, btn in self.fx_buttons.items():
            if name == cur["effect"]:
                btn.add_css_class("card-sel")
            else:
                btn.remove_css_class("card-sel")

        self.theme_row.set_active(bool(cur.get("use_theme", True)))
        self.colour_row.set_sensitive(not cur.get("use_theme", True))

        _clear(self.colour_box)
        for i, hexc in enumerate(cur.get("colors") or ["#7aa2f7"]):
            btn = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog())
            rgba = Gdk.RGBA()
            rgba.parse(hexc)
            btn.set_rgba(rgba)
            btn.connect("notify::rgba", self._on_colour_changed, i)
            self.colour_box.append(btn)

        _clear(self.pal_flow)
        for name, rgb in kbd.load_palette().items():
            hexc = rgb_hex(rgb)
            sw = Gtk.Button(tooltip_text=f"{name}  {hexc}")
            sw.set_size_request(22, 22)
            prov = Gtk.CssProvider()
            prov.load_from_data(
                f"button{{background-color:{hexc};min-width:20px;"
                f"min-height:20px;padding:0;border-radius:5px;}}".encode())
            sw.get_style_context().add_provider(
                prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            sw.connect("clicked", self._on_palette_pick, hexc)
            self.pal_flow.append(sw)

        self.speed_row.set_value(float(cur.get("speed", 1.0)))
        self.bright_row.set_value(float(cur.get("brightness", 1.0)))
        _set_text_if_changed(self.word_row, cur.get("word") or "")

        _clear(self.fav_list)
        favs = self.engine.cfg.get("favorites", {})
        if not favs:
            empty = Adw.ActionRow(title="No favourites saved yet")
            empty.set_sensitive(False)
            self.fav_list.append(empty)
        for name, st in favs.items():
            r = Adw.ActionRow(title=name, subtitle=st.get("effect", ""))
            load = Gtk.Button(label="Load", valign=Gtk.Align.CENTER)
            dele = Gtk.Button(icon_name="user-trash-symbolic",
                              valign=Gtk.Align.CENTER)
            dele.add_css_class("destructive-action")
            load.connect("clicked", self._on_fav_load, name)
            dele.connect("clicked", self._on_fav_del, name)
            r.add_suffix(load)
            r.add_suffix(dele)
            self.fav_list.append(r)

        sc = self.engine.cfg.get("schedule", {})
        self.sched_switch.set_active(bool(sc.get("enabled")))
        _set_text_if_changed(self.day_at, sc.get("day_at", "07:00"))
        _set_text_if_changed(self.night_at, sc.get("night_at", "20:00"))

        self._building = False

    def _tick(self):
        for prov, rgb in zip(self.zone_css, self.engine.last_frame):
            prov.load_from_data(
                f".zone{{background-color:{rgb_hex(rgb)};}}".encode())
        return True

    def push(self, patch):
        if self._building:
            return
        self.engine.apply(patch)
        self.sync_from_engine()

    # ---------------------------------------------------------- handlers

    def _on_effect(self, _b, name):
        self.push({"effect": name})

    def _on_theme_toggle(self, row, _p):
        self.push({"use_theme": row.get_active()})

    def _on_colour_changed(self, btn, _p, idx):
        if self._building:
            return
        c = btn.get_rgba()
        hexc = "#%02x%02x%02x" % (int(c.red * 255), int(c.green * 255),
                                  int(c.blue * 255))
        cols = list(self.engine.cfg["current"].get("colors") or ["#7aa2f7"])
        while len(cols) <= idx:
            cols.append(hexc)
        cols[idx] = hexc
        self.push({"colors": cols, "use_theme": False})

    def _on_add_colour(self, _b):
        cols = list(self.engine.cfg["current"].get("colors") or [])
        cols.append("#ff7ac6")
        self.push({"colors": cols, "use_theme": False})

    def _on_del_colour(self, _b):
        cols = list(self.engine.cfg["current"].get("colors") or [])
        if len(cols) > 1:
            cols.pop()
            self.push({"colors": cols, "use_theme": False})

    def _on_palette_pick(self, _b, hexc):
        self.push({"colors": [hexc], "use_theme": False})

    def _on_speed(self, row, _p):
        self.push({"speed": round(row.get_value(), 2)})

    def _on_bright(self, row, _p):
        self.push({"brightness": round(row.get_value(), 2)})

    def _on_scroll(self, _b):
        self.push({"effect": "scroll",
                   "word": self.word_row.get_text() or "OMARCHY"})

    def _on_fav_save(self, _w):
        name = self.fav_entry.get_text().strip()
        if not name:
            self.fav_entry.grab_focus()
            return
        self.engine.save_favorite(name)
        self.fav_entry.set_text("")
        self.sync_from_engine()

    def _on_fav_load(self, _b, name):
        self.engine.load_favorite(name)
        self.sync_from_engine()

    def _on_fav_del(self, _b, name):
        self.engine.delete_favorite(name)
        self.sync_from_engine()

    def _on_profile_save(self, _b, which):
        self.engine.save_profile(which)
        self.sync_from_engine()

    def _on_profile_load(self, _b, which):
        self.engine.load_profile(which)
        self.sync_from_engine()

    def _on_time_changed(self, _entry):
        # No apply button: a time is saved the moment both read as HH:MM.
        if self._building:
            return
        if HHMM.match(self.day_at.get_text()) and \
                HHMM.match(self.night_at.get_text()):
            self._on_sched()

    def _on_sched(self, *_a):
        if self._building:
            return
        self.engine.set_schedule(self.sched_switch.get_active(),
                                 self.day_at.get_text(),
                                 self.night_at.get_text())
        self.sync_from_engine()


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id="com.nixfred.LightShow")
        self.engine = None

    def do_activate(self):
        prov = Gtk.CssProvider()
        prov.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), prov,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        if self.engine is None:
            # A running daemon already owns the controller. Drive it over its
            # API rather than opening the device too, or the two fight and the
            # effects stutter.
            self.engine = remote.connect() or Engine()
        win = Window(self, self.engine)
        win.present()


def run():
    return App().run(None)
