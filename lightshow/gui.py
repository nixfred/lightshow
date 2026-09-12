"""LightShow for Omarchy - native GTK4 / libadwaita desktop app.

Drives the Engine in-process. No HTTP server, no browser, no port.

Threading note: the Engine owns a worker thread that writes to the keyboard.
GTK is not thread-safe, so the only cross-thread traffic is the UI *reading*
engine.last_frame from a GLib timeout on the main loop. Nothing in here touches
widgets from the worker.
"""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, GLib, Gtk, Pango  # noqa: E402

from . import effects, kbd  # noqa: E402
from .engine import Engine  # noqa: E402
from .kbd import hex_rgb, rgb_hex  # noqa: E402

CSS = b"""
.zone        { border-radius: 8px; min-height: 76px; }
.zone-label  { font-size: 10px; font-weight: 800; letter-spacing: 2px;
               color: alpha(#000, .62); margin: 6px; }
.title-grad  { font-size: 19px; font-weight: 800; letter-spacing: 4px; }
.mono        { font-family: "JetBrains Mono", monospace; }
.dim         { opacity: .62; font-size: 11px; }
.fxname      { font-weight: 700; letter-spacing: 1px; }
.fxdesc      { font-size: 11px; opacity: .6; }
.kindtag     { font-size: 9px; letter-spacing: 2px; opacity: .55; }
.card-sel    { background: alpha(@accent_bg_color, .18);
               box-shadow: inset 0 0 0 1px @accent_bg_color; }
"""


class Window(Adw.ApplicationWindow):
    def __init__(self, app, engine):
        super().__init__(application=app, title="LightShow for Omarchy")
        self.engine = engine
        self.set_default_size(1080, 760)
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

        scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                       margin_top=18, margin_bottom=24,
                       margin_start=18, margin_end=18)
        scroller.set_child(body)
        toolbar.set_content(scroller)
        self.set_content(toolbar)

        body.append(self._build_preview())
        body.append(self._build_effects())

        cols = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=18,
                       homogeneous=True)
        cols.append(self._build_colour())
        cols.append(self._build_controls())
        body.append(cols)

        body.append(self._build_favorites())
        body.append(self._build_daynight())

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
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                      margin_top=6)
        self.zone_widgets, self.zone_css = [], []
        weights = [1, 3, 1, 2]  # rough physical proportions of each group
        for (name, _mask, label), w in zip(kbd.ZONES, weights):
            f = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                        valign=Gtk.Align.FILL, hexpand=True)
            f.add_css_class("zone")
            f.set_size_request(60 * w, 76)
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
        g.add(box)
        note = Gtk.Label(
            label="Four zone groups is everything the MS-1801 exposes. "
                  "Individual keys cannot be addressed, so this is the real "
                  "resolution of the hardware.",
            wrap=True, xalign=0, margin_top=8)
        note.add_css_class("dim")
        g.add(note)
        return g

    def _build_effects(self):
        g = self._group("Effects")
        flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE,
                           max_children_per_line=5, min_children_per_line=2,
                           row_spacing=8, column_spacing=8, margin_top=6,
                           homogeneous=True)
        self.fx_buttons = {}
        for e in effects.all_effects():
            btn = Gtk.Button()
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                            margin_top=8, margin_bottom=8,
                            margin_start=8, margin_end=8)
            n = Gtk.Label(label=e["name"].upper(), xalign=0)
            n.add_css_class("fxname")
            d = Gtk.Label(label=e["desc"], xalign=0, wrap=True,
                          wrap_mode=Pango.WrapMode.WORD_CHAR, max_width_chars=22)
            d.add_css_class("fxdesc")
            k = Gtk.Label(label=e["kind"].upper(), xalign=0)
            k.add_css_class("kindtag")
            inner.append(n); inner.append(d); inner.append(k)
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

        pal_row = Adw.ActionRow(
            title="Theme palette",
            subtitle="Click a swatch to use it as your only colour")
        self.pal_flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE,
                                    max_children_per_line=13, row_spacing=4,
                                    column_spacing=4, margin_top=8,
                                    margin_bottom=8)
        g.add(pal_row)
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
        go = Gtk.Button(label="Scroll it", valign=Gtk.Align.CENTER)
        go.add_css_class("suggested-action")
        go.connect("clicked", self._on_scroll)
        self.word_row.add_suffix(go)
        g.add(self.word_row)

        note = Gtk.Label(
            label="Scroll shows one travelling pulse per letter. With four "
                  "zones the letters cannot be drawn as shapes, so you are "
                  "watching the word's rhythm cross the keyboard.",
            wrap=True, xalign=0, margin_top=8)
        note.add_css_class("dim")
        g.add(note)
        return g

    def _build_favorites(self):
        g = self._group("Favourites")
        self.fav_entry = Adw.EntryRow(title="Name this look")
        save = Gtk.Button(label="Save current", valign=Gtk.Align.CENTER)
        save.add_css_class("suggested-action")
        save.connect("clicked", self._on_fav_save)
        self.fav_entry.add_suffix(save)
        g.add(self.fav_entry)
        self.fav_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE,
                                    margin_top=8)
        self.fav_list.add_css_class("boxed-list")
        g.add(self.fav_list)
        self.fav_group = g
        return g

    def _build_daynight(self):
        g = self._group("Day and Night")

        for which, icon in (("day", "☀"), ("night", "☾")):
            row = Adw.ActionRow(title=f"{icon}  {which.capitalize()} profile")
            load = Gtk.Button(label="Load", valign=Gtk.Align.CENTER)
            save = Gtk.Button(label="Save current", valign=Gtk.Align.CENTER)
            load.connect("clicked", self._on_profile_load, which)
            save.connect("clicked", self._on_profile_save, which)
            row.add_suffix(load)
            row.add_suffix(save)
            g.add(row)
            setattr(self, f"row_{which}", row)

        self.sched_row = Adw.SwitchRow(
            title="Auto-switch",
            subtitle="Applies whichever profile the clock falls into")
        self.sched_row.connect("notify::active", self._on_sched)
        g.add(self.sched_row)

        self.day_at = Adw.EntryRow(title="Day starts (HH:MM)")
        self.night_at = Adw.EntryRow(title="Night starts (HH:MM)")
        for r in (self.day_at, self.night_at):
            r.connect("apply", self._on_sched)
            r.set_show_apply_button(True)
            g.add(r)
        return g

    # ------------------------------------------------------------- state

    def sync_from_engine(self):
        """Repaint every widget from engine config. Guarded against feedback."""
        self._building = True
        cur = self.engine.cfg["current"]

        self.status.set_text(
            f"{kbd.theme_name()}   ·   {self.engine.kb.node}"
            + (f"   ·   {self.engine.active_profile()}"
               if self.engine.active_profile() else ""))

        for name, btn in self.fx_buttons.items():
            if name == cur["effect"]:
                btn.add_css_class("card-sel")
            else:
                btn.remove_css_class("card-sel")

        self.theme_row.set_active(bool(cur.get("use_theme", True)))
        self.colour_row.set_sensitive(not cur.get("use_theme", True))

        child = self.colour_box.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.colour_box.remove(child)
            child = nxt
        for i, hexc in enumerate(cur.get("colors") or ["#7aa2f7"]):
            btn = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog())
            rgba = Gdk.RGBA()
            rgba.parse(hexc)
            btn.set_rgba(rgba)
            btn.connect("notify::rgba", self._on_colour_changed, i)
            self.colour_box.append(btn)

        child = self.pal_flow.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.pal_flow.remove(child)
            child = nxt
        for name, rgb in kbd.load_palette().items():
            hexc = rgb_hex(rgb)
            sw = Gtk.Button(tooltip_text=f"{name}  {hexc}")
            sw.set_size_request(26, 26)
            prov = Gtk.CssProvider()
            prov.load_from_data(
                f"button{{background-color:{hexc};min-width:22px;"
                f"min-height:22px;padding:0;border-radius:5px;}}".encode())
            sw.get_style_context().add_provider(
                prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            sw.connect("clicked", self._on_palette_pick, hexc)
            self.pal_flow.append(sw)

        self.speed_row.set_value(float(cur.get("speed", 1.0)))
        self.bright_row.set_value(float(cur.get("brightness", 1.0)))
        self.word_row.set_text(cur.get("word") or "")

        row = self.fav_list.get_first_child()
        while row:
            nxt = row.get_next_sibling()
            self.fav_list.remove(row)
            row = nxt
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
        self.sched_row.set_active(bool(sc.get("enabled")))
        self.day_at.set_text(sc.get("day_at", "07:00"))
        self.night_at.set_text(sc.get("night_at", "20:00"))

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

    def _on_fav_save(self, _b):
        name = self.fav_entry.get_text().strip()
        if not name:
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

    def _on_sched(self, *_a):
        if self._building:
            return
        self.engine.set_schedule(self.sched_row.get_active(),
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
            self.engine = Engine()
        win = Window(self, self.engine)
        win.present()


def run():
    return App().run(None)
