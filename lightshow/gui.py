"""LightShow for Omarchy - native GTK4 / libadwaita desktop app.

Drives the Engine in-process, or a running daemon over its API. No browser.

Threading note: the Engine owns a worker thread that writes to the keyboard.
GTK is not thread-safe, so the only cross-thread traffic is the UI *reading*
engine.last_frame from a GLib timeout on the main loop. Nothing in here touches
widgets from the worker.

Layout rule: the page never scrolls. Everything is on screen at once: the
colours on the keys right now, the theme palette, the effects, and the colour
panel: follow the Omarchy theme, or pick your own colours per effect on a
hue/saturation wheel with a brightness slider.
"""

import colorsys
import math

import cairo

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, GLib, GObject, Gtk, Pango  # noqa: E402

WHEEL = 220          # colour wheel size in px
PUSH_DELAY_MS = 90   # coalesce wheel drags into one keyboard update this often

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
.swatch      { min-width: 30px; min-height: 30px; padding: 0; border-radius: 7px; }
.swatch-sel  { box-shadow: 0 0 0 2px @window_bg_color, 0 0 0 4px @accent_bg_color; }
.colpanel    { min-width: 280px; }
"""


class ColorWheel(Gtk.DrawingArea):
    """Hue around the rim, saturation from the centre out; brightness is a
    separate slider, drawn here as a dark veil over the wheel."""

    def __init__(self, on_change):
        super().__init__()
        self.set_size_request(WHEEL, WHEEL)
        self.set_halign(Gtk.Align.CENTER)
        self.h, self.s, self.v = 0.0, 1.0, 1.0
        self._on_change = on_change
        self._surface = self._render()
        self.set_draw_func(self._draw)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", lambda g, x, y: self._pick(x, y))
        drag.connect("drag-update", self._drag_update)
        self.add_controller(drag)

    @staticmethod
    def _render():
        """The full-brightness wheel, once, as an ARGB32 image."""
        n, r = WHEEL, WHEEL / 2
        stride = cairo.ImageSurface.format_stride_for_width(cairo.FORMAT_ARGB32, n)
        buf = bytearray(stride * n)
        for y in range(n):
            dy = y + 0.5 - r
            row = y * stride
            for x in range(n):
                dx = x + 0.5 - r
                d = math.hypot(dx, dy)
                a = max(0.0, min(1.0, r - d))          # 1px anti-aliased rim
                if a <= 0:
                    continue
                h = (math.atan2(-dy, dx) / (2 * math.pi)) % 1.0
                cr, cg, cb = colorsys.hsv_to_rgb(h, min(1.0, d / r), 1.0)
                i = row + x * 4                        # premultiplied BGRA
                buf[i] = int(cb * 255 * a)
                buf[i + 1] = int(cg * 255 * a)
                buf[i + 2] = int(cr * 255 * a)
                buf[i + 3] = int(255 * a)
        return cairo.ImageSurface.create_for_data(buf, cairo.FORMAT_ARGB32, n, n, stride)

    def _draw(self, _area, cr, w, h):
        ox, oy = (w - WHEEL) / 2, (h - WHEEL) / 2
        cr.set_source_surface(self._surface, ox, oy)
        cr.paint_with_alpha(1.0 if self.get_sensitive() else 0.35)
        r = WHEEL / 2
        if self.v < 1.0:
            cr.arc(ox + r, oy + r, r, 0, 2 * math.pi)
            cr.set_source_rgba(0, 0, 0, 1.0 - self.v)
            cr.fill()
        if not self.get_sensitive():
            return
        ang = self.h * 2 * math.pi
        mx = ox + r + math.cos(ang) * self.s * r
        my = oy + r - math.sin(ang) * self.s * r
        cr.arc(mx, my, 7, 0, 2 * math.pi)
        cr.set_source_rgb(*colorsys.hsv_to_rgb(self.h, self.s, self.v))
        cr.fill_preserve()
        cr.set_line_width(2.5)
        cr.set_source_rgb(1, 1, 1)
        cr.stroke_preserve()
        cr.set_line_width(1)
        cr.set_source_rgb(0, 0, 0)
        cr.stroke()

    def _drag_update(self, g, dx, dy):
        ok, x, y = g.get_start_point()
        if ok:
            self._pick(x + dx, y + dy)

    def _pick(self, x, y):
        if not self.get_sensitive():
            return
        w, h = self.get_width(), self.get_height()
        r = WHEEL / 2
        dx, dy = x - w / 2, y - h / 2
        self.h = (math.atan2(-dy, dx) / (2 * math.pi)) % 1.0
        self.s = min(1.0, math.hypot(dx, dy) / r)
        self.queue_draw()
        self._on_change()

    def set_rgb(self, rgb):
        self.h, self.s, self.v = colorsys.rgb_to_hsv(*(c / 255 for c in rgb))
        self.queue_draw()

    def rgb(self):
        return tuple(int(round(c * 255)) for c in colorsys.hsv_to_rgb(self.h, self.s, self.v))


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

        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, hexpand=True)
        left.append(self._build_preview())
        left.append(self._build_effects())
        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=18,
                       margin_top=12, margin_bottom=14,
                       margin_start=18, margin_end=18)
        body.append(left)
        body.append(self._build_colors())
        # Wide window: colours beside the effects. Narrow (e.g. tiled): colours
        # underneath, and the page scrolls vertically.
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                      vexpand=True)
        scroller.set_child(body)
        toolbar.set_content(scroller)
        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 1000sp"))
        v = GObject.Value(Gtk.Orientation, Gtk.Orientation.VERTICAL)
        bp.add_setter(body, "orientation", v)
        self.add_breakpoint(bp)
        self.set_size_request(380, 400)

        # Colour panel state: the list being edited and which slot is selected.
        self._col_effect = None
        self._col_list = []
        self._col_sel = 0
        self._push_pending = None
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
        self.theme_row = Adw.ActionRow(title="Theme colours")
        self.pal_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4,
                               valign=Gtk.Align.CENTER)
        self.theme_row.add_suffix(self.pal_box)
        g.add(self.theme_row)

        # One row per connected keyboard: what it is, and in one line what it
        # cannot do, so nobody wonders why a white backlight is not showing red.
        self.boards_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.boards_box.add_css_class("boxed-list")
        g.add(self.boards_box)
        return g

    def _build_effects(self):
        g = self._group("Effects")
        flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE,
                           max_children_per_line=7, min_children_per_line=2,
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

    def _build_colors(self):
        g = self._group("Colours")
        g.add_css_class("colpanel")
        g.set_size_request(290, -1)
        g.set_hexpand(False)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=4)

        # Theme | Custom: which palette every effect uses.
        src = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, homogeneous=True)
        src.add_css_class("linked")
        self.src_theme = Gtk.ToggleButton(label="Theme",
                                          tooltip_text="Follow the Omarchy/Noctalia theme palette")
        self.src_custom = Gtk.ToggleButton(label="Custom", group=self.src_theme,
                                           tooltip_text="Use your own colours, set per effect")
        self.src_theme.connect("toggled", self._on_source, "theme")
        self.src_custom.connect("toggled", self._on_source, "custom")
        src.append(self.src_theme); src.append(self.src_custom)
        box.append(src)

        self.col_title = Gtk.Label(xalign=0)
        self.col_title.add_css_class("fxname")
        self.col_hint = Gtk.Label(xalign=0, wrap=True)
        self.col_hint.add_css_class("dim")
        box.append(self.col_title); box.append(self.col_hint)

        # One swatch per colour slot, plus add/remove.
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.swatch_box = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE,
                                      max_children_per_line=8, min_children_per_line=4,
                                      row_spacing=6, column_spacing=6, hexpand=True,
                                      homogeneous=True, halign=Gtk.Align.START)
        row.append(self.swatch_box)
        box.append(row)
        pm = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.btn_add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Add a colour")
        self.btn_del = Gtk.Button(icon_name="list-remove-symbolic", tooltip_text="Remove the selected colour")
        self.btn_reset = Gtk.Button(label="Reset", tooltip_text="Forget the picks for this effect")
        self.btn_add.connect("clicked", self._on_add)
        self.btn_del.connect("clicked", self._on_del)
        self.btn_reset.connect("clicked", self._on_reset)
        pm.append(self.btn_add); pm.append(self.btn_del)
        sp = Gtk.Box(hexpand=True); pm.append(sp)
        pm.append(self.btn_reset)
        box.append(pm)

        self.wheel = ColorWheel(self._on_wheel)
        box.append(self.wheel)

        vrow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        vl = Gtk.Label(label="Brightness"); vl.add_css_class("dim")
        self.val = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 0.01)
        self.val.set_draw_value(False); self.val.set_hexpand(True)
        self.val.connect("value-changed", self._on_value)
        vrow.append(vl); vrow.append(self.val)
        box.append(vrow)

        self.hex_entry = Gtk.Entry(max_length=7, width_chars=8, placeholder_text="#rrggbb")
        self.hex_entry.add_css_class("mono")
        self.hex_entry.connect("activate", self._on_hex)
        box.append(self.hex_entry)

        g.add(box)
        return g

    # ------------------------------------------------------------- state

    def sync_from_engine(self):
        """Repaint every widget from engine config. Guarded against feedback."""
        self._building = True
        cur = self.engine.cfg["current"]
        snap = self.engine.snapshot()

        mode = "daemon" if isinstance(self.engine, remote.RemoteEngine) else "local"
        src = "custom colours" if snap.get("color_source") == "custom" else kbd.theme_name()
        self.status.set_text(f"{src}   ·   {self.engine.kb.node}   ·   {mode}")

        fidelity = {e["name"]: e for e in snap.get("effects", [])}
        for name, btn in self.fx_buttons.items():
            if name == cur["effect"]:
                btn.add_css_class("card-sel")
            else:
                btn.remove_css_class("card-sel")
            e = fidelity.get(name, {})
            btn.set_sensitive(not e.get("greyed"))
            tip = e.get("desc", btn.get_tooltip_text() or "")
            limits = [f"{b}: {'not shown' if f == 'none' else 'reduced'}"
                      for b, f in (e.get("fidelity") or {}).items() if f != "full"]
            if limits:
                tip += "\n" + "\n".join(limits)
            btn.set_tooltip_text(tip)

        _clear(self.boards_box)
        for b in snap.get("boards", []):
            row = Adw.ActionRow(title=b["name"],
                                subtitle=("⚠ " + b["notice"]) if b.get("notice")
                                else b["caps"]["summary"])
            row.set_tooltip_text(f"{b['node']}  ·  {b['caps']['summary']}")
            self.boards_box.append(row)

        tn = kbd.theme_name()
        tn = "the desktop theme" if tn in ("", "unknown") else tn
        if snap.get("color_source") == "custom":
            self.theme_row.set_subtitle(f"Using your picked colours; {tn} is on standby.")
        else:
            self.theme_row.set_subtitle(f"Following {tn}. Switch themes and the keyboard follows.")

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

        self._sync_colors(snap)
        self._building = False

    # ------------------------------------------------------------- colours

    def _custom(self, snap=None):
        snap = snap or self.engine.snapshot()
        return snap.get("color_source") == "custom"

    def _sync_colors(self, snap, keep_edit=False):
        """Fill the colour panel for the current effect from the engine."""
        name = self.engine.cfg["current"]["effect"]
        custom = self._custom(snap)
        slots = (snap.get("color_slots") or {}).get(name, {"max": 8, "hint": ""})
        mx = slots["max"]

        self.src_theme.set_active(not custom)
        self.src_custom.set_active(custom)

        if not (keep_edit and name == self._col_effect):
            if custom:
                self._col_list = list((snap.get("custom_colors") or {}).get(name) or ["#ffffff"])
            else:
                self._col_list = list(snap.get("theme_colors") or [])[:max(1, mx)]
            if name != self._col_effect:
                self._col_sel = 0
            self._col_effect = name
        self._col_sel = min(self._col_sel, max(0, len(self._col_list) - 1))

        self.col_title.set_text(name.upper())
        if mx == 0:
            hint = slots["hint"]
        elif custom:
            hint = slots["hint"] + ("  ·  saved" if name in (snap.get("custom_saved") or []) else "")
        else:
            hint = slots["hint"] + ". Following the theme; switch to Custom to pick your own."
        self.col_hint.set_text(hint)

        editable = custom and mx > 0
        self.wheel.set_sensitive(editable)
        self.val.set_sensitive(editable)
        self.hex_entry.set_sensitive(editable)
        self.btn_add.set_sensitive(editable and len(self._col_list) < mx)
        self.btn_del.set_sensitive(editable and len(self._col_list) > 1)
        self.btn_reset.set_sensitive(editable and name in (snap.get("custom_saved") or []))
        self._rebuild_swatches(mx > 0)
        self._load_selected()

    def _rebuild_swatches(self, show=True):
        _clear(self.swatch_box)
        self._swatch_css = []
        if not show:
            return
        for i, hexc in enumerate(self._col_list):
            b = Gtk.Button(tooltip_text=hexc, halign=Gtk.Align.CENTER,
                           valign=Gtk.Align.CENTER)
            b.set_size_request(30, 30)
            b.add_css_class("swatch")
            if i == self._col_sel:
                b.add_css_class("swatch-sel")
            prov = Gtk.CssProvider()
            prov.load_from_data(f"button{{background:{hexc};}}".encode())
            b.get_style_context().add_provider(prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
            b.connect("clicked", self._on_swatch, i)
            self._swatch_css.append(prov)
            self.swatch_box.append(b)

    def _load_selected(self):
        """Point the wheel, slider and hex box at the selected slot."""
        if not self._col_list:
            return
        hexc = self._col_list[self._col_sel]
        self._building = True
        self.wheel.set_rgb(kbd.hex_rgb(hexc))
        self.val.set_value(self.wheel.v)
        self.hex_entry.set_text(hexc)
        self._building = False

    def _edited(self, rgb):
        """The selected slot changed colour: repaint it and queue a push."""
        hexc = rgb_hex(rgb)
        self._col_list[self._col_sel] = hexc
        if self._col_sel < len(self._swatch_css):
            self._swatch_css[self._col_sel].load_from_data(
                f"button{{background:{hexc};}}".encode())
        if not self.hex_entry.has_focus():
            self.hex_entry.set_text(hexc)
        self._queue_push()

    def _queue_push(self):
        if self._push_pending is None:
            self._push_pending = GLib.timeout_add(PUSH_DELAY_MS, self._push_colors)

    def _push_colors(self):
        self._push_pending = None
        if self._col_effect:
            self.engine.apply({"effect_colors": {self._col_effect: list(self._col_list)}},
                              save=True)
            snap = self.engine.snapshot()
            self._building = True
            self._sync_colors(snap, keep_edit=True)
            self._building = False
        return False

    # -- colour handlers ---------------------------------------------------

    def _on_source(self, btn, source):
        if self._building or not btn.get_active():
            return
        self.push({"color_source": source})

    def _on_swatch(self, _b, i):
        self._col_sel = i
        self._rebuild_swatches()
        self._load_selected()

    def _on_wheel(self):
        if self._building:
            return
        self._building = True
        self.val.set_value(self.wheel.v)
        self._building = False
        self._edited(self.wheel.rgb())

    def _on_value(self, scale):
        if self._building:
            return
        self.wheel.v = scale.get_value()
        self.wheel.queue_draw()
        self._edited(self.wheel.rgb())

    def _on_hex(self, entry):
        t = entry.get_text().strip()
        t = t if t.startswith("#") else "#" + t
        try:
            rgb = kbd.hex_rgb(t)
        except ValueError:
            return
        if len(t) != 7:
            return
        self.wheel.set_rgb(rgb)
        self._building = True
        self.val.set_value(self.wheel.v)
        self._building = False
        self._edited(rgb)

    def _on_add(self, _b):
        # Start the new slot from the selected colour, rotated a little in hue.
        h, s, v = colorsys.rgb_to_hsv(*(c / 255 for c in kbd.hex_rgb(self._col_list[self._col_sel])))
        new = rgb_hex(tuple(int(c * 255) for c in colorsys.hsv_to_rgb((h + 0.15) % 1, s, v)))
        self._col_list.append(new)
        self._col_sel = len(self._col_list) - 1
        self._push_colors()

    def _on_del(self, _b):
        if len(self._col_list) > 1:
            del self._col_list[self._col_sel]
            self._col_sel = max(0, self._col_sel - 1)
            self._push_colors()

    def _on_reset(self, _b):
        self.engine.apply({"effect_colors": {self._col_effect: []}})
        self._col_effect = None          # reload the defaults
        self.sync_from_engine()

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
