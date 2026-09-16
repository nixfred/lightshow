"""LightShow for Omarchy - native GTK4 / libadwaita desktop app.

Drives the Engine in-process, or a running daemon over its API. No browser.

Threading note: the Engine owns a worker thread that writes to the keyboard.
GTK is not thread-safe, so the only cross-thread traffic is the UI *reading*
engine.last_frame from a GLib timeout on the main loop. Nothing in here touches
widgets from the worker.

Layout rule: the page never scrolls. Everything is on screen at once: the
colours on the keys right now, the theme palette they come from, and the
effects. There is nothing else to set: colours always follow the Omarchy theme.
"""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, GLib, Gtk, Pango  # noqa: E402

from . import effects, kbd, remote  # noqa: E402
from .engine import Engine  # noqa: E402
from .kbd import rgb_hex  # noqa: E402

CSS = b"""
.zone        { border-radius: 8px; min-height: 64px; }
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


def _clear(container):
    child = container.get_first_child()
    while child:
        nxt = child.get_next_sibling()
        container.remove(child)
        child = nxt


class Window(Adw.ApplicationWindow):
    def __init__(self, app, engine):
        super().__init__(application=app, title="LightShow for Omarchy")
        self.engine = engine
        self.set_default_size(1180, 700)
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

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14,
                       margin_top=12, margin_bottom=14,
                       margin_start=18, margin_end=18)
        body.append(self._build_preview())
        body.append(self._build_effects())
        toolbar.set_content(body)
        self.set_content(toolbar)

        self.sync_from_engine()
        # Mirror the hardware at ~20fps. Reading last_frame is a plain list
        # read; no lock needed and a torn read would cost one stale frame.
        GLib.timeout_add(50, self._tick)

    # ---------------------------------------------------------------- ui

    def _group(self, title):
        return Adw.PreferencesGroup(title=title)

    def _build_preview(self):
        g = self._group("On the keys now")
        g.set_tooltip_text(
            "The four zone groups both keyboards are driven in. This is what "
            "the keys show right now; the swatches under it are the theme "
            "palette those colours come from.")
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                      margin_top=4)
        self.zone_widgets, self.zone_css = [], []
        weights = [1, 3, 1, 2]  # rough physical proportions of each group
        for (name, _mask, label), w in zip(kbd.ZONES, weights):
            f = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                        valign=Gtk.Align.FILL, hexpand=True)
            f.add_css_class("zone")
            f.set_size_request(60 * w, 64)
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
        box.set_vexpand(False)
        g.add(box)

        # The theme palette, as a read-out: colours are never chosen here.
        self.theme_row = Adw.ActionRow(title="Omarchy theme colours")
        self.pal_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4,
                               valign=Gtk.Align.CENTER)
        self.theme_row.add_suffix(self.pal_box)
        g.add(self.theme_row)
        return g

    def _build_effects(self):
        g = self._group("Effects")
        flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE,
                           max_children_per_line=7, min_children_per_line=3,
                           row_spacing=6, column_spacing=6, margin_top=4,
                           homogeneous=True, vexpand=True, valign=Gtk.Align.START)
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

    # ------------------------------------------------------------- state

    def sync_from_engine(self):
        """Repaint every widget from engine config. Guarded against feedback."""
        self._building = True
        cur = self.engine.cfg["current"]

        mode = "daemon" if isinstance(self.engine, remote.RemoteEngine) else "local"
        self.status.set_text(
            f"{kbd.theme_name()}   ·   {self.engine.kb.node}   ·   {mode}")

        for name, btn in self.fx_buttons.items():
            if name == cur["effect"]:
                btn.add_css_class("card-sel")
            else:
                btn.remove_css_class("card-sel")

        self.theme_row.set_subtitle(
            f"Following {kbd.theme_name()}. Switch themes and both keyboards follow.")

        _clear(self.pal_box)
        for name, rgb in kbd.load_palette().items():
            hexc = rgb_hex(rgb)
            sw = Gtk.Box(tooltip_text=f"{name}  {hexc}")
            sw.set_size_request(22, 22)
            prov = Gtk.CssProvider()
            prov.load_from_data(
                f"box{{background-color:{hexc};border-radius:5px;}}".encode())
            sw.get_style_context().add_provider(
                prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            self.pal_box.append(sw)

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
