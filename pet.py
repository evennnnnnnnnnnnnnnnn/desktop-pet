#!/usr/bin/env python3
"""A desktop pet that wanders randomly across your screen.

Renders as a transparent Wayland layer-shell overlay covering one monitor. The
pet is drawn inside that overlay, so walking is just moving a point on a canvas
-- the compositor is never asked to reposition a window.

The overlay's input region is kept clipped to the pet's own opaque pixels, so
the pet reacts to the pointer while the rest of the screen stays click-through.
Hovering the pet stops it; right-clicking opens a menu.

The pet also doubles as an Orbh monitor. When an agent session anywhere on the
machine blocks on a human, the pet walks to the nearest corner, waves, and
shows a speech bubble naming the sessions; the right-click menu lists them and
attaches on click. See orbh.py. That watch is optional -- if Blacksmith is not
running, the pet just wanders.
"""

import argparse
import json
import math
import random
import shlex
from pathlib import Path

import cairo
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("GtkLayerShell", "0.1")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import (  # noqa: E402
    Gdk, GdkPixbuf, GLib, Gtk, GtkLayerShell, Pango, PangoCairo,
)

import orbh  # noqa: E402

TICK_MS = 16  # ~60fps
ALPHA_CUTOFF = 8  # pixels this transparent are not part of the pet
REGION_EPSILON = 3  # only resend the input region once it drifts this many px

CORNER_MARGIN = 24  # how far from the screen edge the pet parks to raise a flag
BUBBLE_PAD = 12
BUBBLE_RADIUS = 10
BUBBLE_GAP = 10  # between the bubble's tail and the pet's head
BUBBLE_MAX_WIDTH = 420
BUBBLE_MAX_ROWS = 5  # more than this and the rest collapse into a "+N more"
BUBBLE_EDGE_MARGIN = 8  # keeps the bubble off the screen edge in a corner

# Only an interactive session has a terminal to take over; anything else is
# opened read-only on its Page, which is where a headless session says what it
# is waiting for.
DEFAULT_ATTACH = "kitty -e flint orbh attach {id} -p {path}"
DEFAULT_INSPECT = "kitty --hold -e flint orbh page {id} -p {path}"


class Sprite:
    """A grid spritesheet: each animation is one row of frames."""

    def __init__(self, manifest_path):
        meta = json.loads(Path(manifest_path).read_text())
        sheet_path = Path(manifest_path).parent / meta["sprite"]
        self.sheet = GdkPixbuf.Pixbuf.new_from_file(str(sheet_path))
        self.w = meta["frame"]["width"]
        self.h = meta["frame"]["height"]
        self.anims = meta["animations"]
        self._cache = {}

    def frame(self, name, elapsed):
        """The frame of `name` to show `elapsed` seconds into the animation."""
        anim = self.anims[name]
        index = int(elapsed * anim["fps"]) % anim["frames"]
        key = (name, index)
        if key not in self._cache:
            self._cache[key] = GdkPixbuf.Pixbuf.new_subpixbuf(
                self.sheet, index * self.w, anim["row"] * self.h, self.w, self.h
            )
        return self._cache[key]

    def opaque_bounds(self, name="idle"):
        """Tightest rect around the non-transparent pixels of `name`'s first frame.

        Used as the pointer target so the transparent margin around the sprite
        stays click-through.
        """
        pixbuf = self.frame(name, 0.0)
        if not pixbuf.get_has_alpha():
            return 0, 0, pixbuf.get_width(), pixbuf.get_height()

        pixels = pixbuf.get_pixels()
        stride = pixbuf.get_rowstride()
        channels = pixbuf.get_n_channels()
        width, height = pixbuf.get_width(), pixbuf.get_height()

        min_x, min_y, max_x, max_y = width, height, -1, -1
        for y in range(height):
            row = y * stride
            for x in range(width):
                if pixels[row + x * channels + 3] > ALPHA_CUTOFF:
                    if x < min_x:
                        min_x = x
                    if x > max_x:
                        max_x = x
                    if y < min_y:
                        min_y = y
                    if y > max_y:
                        max_y = y
        if max_x < 0:
            return 0, 0, width, height
        return min_x, min_y, max_x - min_x + 1, max_y - min_y + 1


class Pet:
    """Random-walk state machine: pick a target, walk to it, pause, repeat."""

    def __init__(self, bounds, speed, dwell):
        self.bounds = bounds  # (width, height) of the area it may roam
        self.speed = speed  # px per second
        self.dwell = dwell  # (min, max) seconds to pause between walks
        self.x, self.y = self._random_point()
        self.facing = "right"
        self.walking = False
        self.frozen = False
        self.summoned = False  # parked somewhere on purpose rather than wandering
        self.pause_left = random.uniform(*dwell)
        self.target = (self.x, self.y)
        self.anim_time = 0.0

    def _random_point(self):
        w, h = self.bounds
        return random.uniform(0, w), random.uniform(0, h)

    def summon(self, point):
        """Abandon the wander, walk to `point`, and stay there waving."""
        if self.summoned and self.target == point:
            return  # already on the way; don't restart the animation
        self.summoned = True
        self.target = point
        self.walking = True
        self.anim_time = 0.0

    def release(self):
        """Go back to wandering."""
        if not self.summoned:
            return
        self.summoned = False
        self.walking = False
        self.pause_left = random.uniform(*self.dwell)
        self.anim_time = 0.0

    @property
    def arrived(self):
        return self.summoned and not self.walking

    @property
    def animation(self):
        if self.frozen:
            return "idle"
        if self.walking:
            return f"walk-{self.facing}"
        return "wave" if self.summoned else "idle"

    def update(self, dt, frozen=False):
        if frozen != self.frozen:
            self.frozen = frozen  # restart the clock so the new animation starts at frame 0
            self.anim_time = 0.0
        self.anim_time += dt

        if frozen:
            return  # held still under the pointer; keeps its target for later

        if not self.walking:
            if self.summoned:
                return  # parked at the corner until whatever summoned it clears
            self.pause_left -= dt
            if self.pause_left <= 0:
                self.target = self._random_point()
                self.walking = True
                self.anim_time = 0.0
            return

        tx, ty = self.target
        dx, dy = tx - self.x, ty - self.y
        distance = math.hypot(dx, dy)
        step = self.speed * dt

        if distance <= step:
            self.x, self.y = tx, ty
            self.walking = False
            self.pause_left = random.uniform(*self.dwell)
            self.anim_time = 0.0
            return

        self.x += dx / distance * step
        self.y += dy / distance * step
        if abs(dx) > 1:
            self.facing = "right" if dx > 0 else "left"


def build_overlay(monitor, namespace):
    """A fullscreen transparent overlay pinned to `monitor`."""
    win = Gtk.Window()
    win.set_app_paintable(True)
    visual = win.get_screen().get_rgba_visual()
    if visual is None:
        raise SystemExit("Compositor does not offer an RGBA visual; cannot draw transparently.")
    win.set_visual(visual)

    GtkLayerShell.init_for_window(win)
    GtkLayerShell.set_namespace(win, namespace)
    GtkLayerShell.set_layer(win, GtkLayerShell.Layer.OVERLAY)
    GtkLayerShell.set_monitor(win, monitor)
    for edge in (GtkLayerShell.Edge.TOP, GtkLayerShell.Edge.BOTTOM,
                 GtkLayerShell.Edge.LEFT, GtkLayerShell.Edge.RIGHT):
        GtkLayerShell.set_anchor(win, edge, True)
    # -1 keeps the overlay from reserving space, so it never shifts other windows.
    GtkLayerShell.set_exclusive_zone(win, -1)
    # ON_DEMAND lets the right-click menu take the keyboard while it is open
    # without the idle overlay ever holding focus.
    GtkLayerShell.set_keyboard_mode(win, GtkLayerShell.KeyboardMode.ON_DEMAND)
    return win


def _rounded_rect(cr, x, y, width, height, radius):
    cr.new_sub_path()
    cr.arc(x + width - radius, y + radius, radius, -math.pi / 2, 0)
    cr.arc(x + width - radius, y + height - radius, radius, 0, math.pi / 2)
    cr.arc(x + radius, y + height - radius, radius, math.pi / 2, math.pi)
    cr.arc(x + radius, y + radius, radius, math.pi, 3 * math.pi / 2)
    cr.close_path()


def draw_bubble(cr, markup, anchor_x, anchor_top, anchor_bottom, bounds):
    """A speech bubble tethered to the sprite spanning anchor_top..anchor_bottom.

    Sits above the sprite when there is room and flips below when there isn't,
    so the pet can raise it from any corner. The two anchors are what keep the
    flipped bubble clear of the pet: pointing both cases at the same edge would
    drop the bubble straight over it.
    """
    layout = PangoCairo.create_layout(cr)
    layout.set_font_description(Pango.FontDescription("Sans 10"))
    layout.set_width(BUBBLE_MAX_WIDTH * Pango.SCALE)
    layout.set_wrap(Pango.WrapMode.WORD_CHAR)
    layout.set_markup(markup, -1)
    text_w, text_h = (size / Pango.SCALE for size in layout.get_size())

    width = text_w + BUBBLE_PAD * 2
    height = text_h + BUBBLE_PAD * 2
    screen_w, screen_h = bounds

    above = anchor_top - BUBBLE_GAP - height >= BUBBLE_EDGE_MARGIN
    anchor_y = anchor_top if above else anchor_bottom
    y = anchor_y - BUBBLE_GAP - height if above else anchor_y + BUBBLE_GAP
    limit_x = max(BUBBLE_EDGE_MARGIN, screen_w - width - BUBBLE_EDGE_MARGIN)
    limit_y = max(BUBBLE_EDGE_MARGIN, screen_h - height - BUBBLE_EDGE_MARGIN)
    x = max(BUBBLE_EDGE_MARGIN, min(anchor_x - width / 2, limit_x))
    y = max(BUBBLE_EDGE_MARGIN, min(y, limit_y))

    cr.save()
    _rounded_rect(cr, x, y, width, height, BUBBLE_RADIUS)
    # The tail is part of the same path, so the fill and the outline meet
    # cleanly instead of showing a seam where the triangle joins the body.
    tail_y = y + height if above else y
    tail_dir = 1 if above else -1
    tip_x = max(x + BUBBLE_RADIUS + 12, min(anchor_x, x + width - BUBBLE_RADIUS - 12))
    cr.move_to(tip_x - 9, tail_y)
    cr.line_to(tip_x, tail_y + BUBBLE_GAP * tail_dir)
    cr.line_to(tip_x + 9, tail_y)
    cr.close_path()

    cr.set_source_rgba(0.09, 0.10, 0.13, 0.94)
    cr.fill_preserve()
    cr.set_source_rgba(0.98, 0.76, 0.22, 0.85)
    cr.set_line_width(1.5)
    cr.stroke()

    cr.set_source_rgba(0.95, 0.95, 0.96, 1.0)
    cr.move_to(x + BUBBLE_PAD, y + BUBBLE_PAD)
    PangoCairo.show_layout(cr, layout)
    cr.restore()


def session_marker(session):
    """● blocking on you, ◌ stopped and unadjudicated, ○ genuinely running."""
    if session.needs_input:
        return "●"
    return "◌" if session.stopped else "○"


def bubble_markup(sessions):
    """The bubble's contents: a headline, then one line per session.

    Leads with whatever is waiting on the human, falling back to a plain count
    of what is merely running. Sessions arrive sorted with the waiting ones
    first, so those are never the rows that get truncated.
    """
    waiting = [session for session in sessions if session.needs_input]
    if waiting:
        noun = "session needs" if len(waiting) == 1 else "sessions need"
        headline = f"{len(waiting)} {noun} you"
    else:
        noun = "session" if len(sessions) == 1 else "sessions"
        headline = f"{len(sessions)} live {noun}"

    lines = [f"<b>{headline}</b>"]
    for session in sessions[:BUBBLE_MAX_ROWS]:
        marker = session_marker(session)
        label = GLib.markup_escape_text(session.label)
        lines.append(f"{marker} <b>{label}</b>" if session.needs_input else f"{marker} {label}")
        if session.stopped:
            # Orbh still calls this "working"; say what it actually means so a
            # stopped agent doesn't read as a busy one.
            lines.append("<span alpha='60%'><i>    stopped, awaiting verdict</i></span>")
    remaining = len(sessions) - BUBBLE_MAX_ROWS
    if remaining > 0:
        lines.append(f"<i>+{remaining} more</i>")
    return "\n".join(lines)


def build_menu(entries, sessions, parent, attach_command, on_closed):
    """The right-click menu: live sessions, the configured launchers, then Quit.

    Rebuilt on every click so the session list is never stale.
    """
    menu = Gtk.Menu()
    # On Wayland the menu is an xdg_popup, which needs its parent surface --
    # without this it silently fails to map on a layer-shell window.
    menu.attach_to_widget(parent, None)

    def launch(_item, command):
        try:
            GLib.spawn_command_line_async(command)
        except GLib.Error as err:
            print(f"could not launch {command!r}: {err.message}")

    for session in sessions:
        label = f"{session_marker(session)}  {GLib.markup_escape_text(session.label)}"
        if session.needs_input:
            label += "   <b>needs input</b>"
        elif session.stopped:
            label += "   <span alpha='60%'><i>stopped, awaiting verdict</i></span>"
        item = Gtk.MenuItem(label="")
        item.get_child().set_markup(label)
        item.set_tooltip_text(
            f"Attach to this session ({session.runtime})" if session.attachable
            else f"Show this session's Page ({session.mode or 'headless'} "
                 f"sessions have no terminal to attach to)"
        )
        item.connect("activate", launch, attach_command(session))
        menu.append(item)
    if sessions:
        menu.append(Gtk.SeparatorMenuItem())

    for entry in entries:
        item = Gtk.MenuItem(label=entry["label"])
        item.connect("activate", launch, entry["command"])
        menu.append(item)

    if entries:
        menu.append(Gtk.SeparatorMenuItem())
    quit_item = Gtk.MenuItem(label="Quit")
    quit_item.connect("activate", lambda _i: Gtk.main_quit())
    menu.append(quit_item)

    menu.show_all()
    menu.connect("deactivate", on_closed)
    return menu


def main():
    here = Path(__file__).parent
    parser = argparse.ArgumentParser(description="A desktop pet that wanders your screen.")
    parser.add_argument("--sprite", default=str(here / "assets" / "pet.json"),
                        help="path to the sprite manifest JSON")
    parser.add_argument("--menu", default=str(here / "assets" / "menu.json"),
                        help="path to the right-click menu JSON")
    parser.add_argument("--monitor", type=int, default=0, help="monitor index to live on")
    parser.add_argument("--scale", type=float, default=0.5, help="sprite scale factor")
    parser.add_argument("--speed", type=float, default=90.0, help="walking speed in px/sec")
    parser.add_argument("--dwell", type=float, nargs=2, default=(1.5, 6.0),
                        metavar=("MIN", "MAX"), help="seconds to pause between walks")
    parser.add_argument("--list-monitors", action="store_true", help="list monitors and exit")
    parser.add_argument("--no-orbh", action="store_true",
                        help="do not watch Orbh agent sessions")
    parser.add_argument("--attach-command", default=DEFAULT_ATTACH,
                        help="command run when an interactive session is picked from "
                             "the menu; {id}, {path} and {title} are substituted")
    parser.add_argument("--inspect-command", default=DEFAULT_INSPECT,
                        help="as --attach-command, but for sessions with no terminal "
                             "to attach to (headless and subagent sessions)")
    args = parser.parse_args()

    display = Gdk.Display.get_default()
    if display is None:
        raise SystemExit("No display available.")

    if args.list_monitors:
        for i in range(display.get_n_monitors()):
            geo = display.get_monitor(i).get_geometry()
            print(f"{i}: {display.get_monitor(i).get_model() or '?'} "
                  f"{geo.width}x{geo.height} at {geo.x},{geo.y}")
        return

    if not 0 <= args.monitor < display.get_n_monitors():
        raise SystemExit(f"No monitor {args.monitor} (found {display.get_n_monitors()}).")
    monitor = display.get_monitor(args.monitor)
    geo = monitor.get_geometry()

    sprite = Sprite(args.sprite)
    menu_path = Path(args.menu)
    entries = json.loads(menu_path.read_text()) if menu_path.exists() else []

    draw_w = sprite.w * args.scale
    draw_h = sprite.h * args.scale
    pet = Pet(
        bounds=(max(1.0, geo.width - draw_w), max(1.0, geo.height - draw_h)),
        speed=args.speed,
        dwell=tuple(args.dwell),
    )

    win = build_overlay(monitor, "desktop-pet")
    win.add_events(
        Gdk.EventMask.POINTER_MOTION_MASK
        | Gdk.EventMask.BUTTON_PRESS_MASK
        | Gdk.EventMask.ENTER_NOTIFY_MASK
        | Gdk.EventMask.LEAVE_NOTIFY_MASK
    )

    # The pet is held still while the pointer is on it or its menu is open.
    held = {"hover": False, "menu": False}
    # Every live session the Orbh watch reports, waiting ones first. Replaced
    # wholesale on each update; any entry at all sends the pet to a corner.
    watch = {"sessions": []}
    # The open menu has to outlive this scope or it is collected while mapped.
    menu_ref = {"menu": None}

    def on_menu_closed(_menu):
        held["menu"] = False

    def attach_command(session):
        template = args.attach_command if session.attachable else args.inspect_command
        return template.format(
            id=shlex.quote(session.id),
            path=shlex.quote(session.flint_path),
            title=shlex.quote(session.title),
        )

    win.connect("enter-notify-event", lambda *_: held.__setitem__("hover", True))
    win.connect("leave-notify-event", lambda *_: held.__setitem__("hover", False))

    def on_button_press(_widget, event):
        if event.button == Gdk.BUTTON_SECONDARY:
            held["menu"] = True
            # Rebuilt per click so the session list reflects the live roster.
            menu_ref["menu"] = build_menu(
                entries, watch["sessions"], win, attach_command, on_menu_closed
            )
            menu_ref["menu"].popup_at_pointer(event)
            return True
        return False

    win.connect("button-press-event", on_button_press)

    # Pointer target: the sprite's opaque pixels, in overlay coordinates.
    bx, by, bw, bh = sprite.opaque_bounds("idle")
    hit_w = max(1, int(bw * args.scale))
    hit_h = max(1, int(bh * args.scale))
    last_region = [None]

    def sync_input_region():
        rect = (int(pet.x + bx * args.scale), int(pet.y + by * args.scale), hit_w, hit_h)
        previous = last_region[0]
        if previous and abs(rect[0] - previous[0]) < REGION_EPSILON and \
                abs(rect[1] - previous[1]) < REGION_EPSILON:
            return
        last_region[0] = rect
        win.input_shape_combine_region(cairo.Region(cairo.RectangleInt(*rect)))

    def on_draw(_widget, cr):
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        cr.save()
        cr.translate(pet.x, pet.y)
        cr.scale(args.scale, args.scale)
        Gdk.cairo_set_source_pixbuf(cr, sprite.frame(pet.animation, pet.anim_time), 0, 0)
        cr.paint()
        cr.restore()

        # Raised only once the pet has actually reached its corner, so the
        # bubble doesn't drag across the screen behind it.
        if watch["sessions"] and pet.arrived:
            draw_bubble(
                cr,
                bubble_markup(watch["sessions"]),
                pet.x + (bx + bw / 2) * args.scale,
                pet.y + by * args.scale,
                pet.y + (by + bh) * args.scale,
                (geo.width, geo.height),
            )
        return False

    win.connect("draw", on_draw)
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    sync_input_region()

    def nearest_corner():
        """The corner of the roaming area closest to where the pet stands."""
        span_w, span_h = pet.bounds
        x = CORNER_MARGIN if pet.x < span_w / 2 else max(CORNER_MARGIN, span_w - CORNER_MARGIN)
        y = CORNER_MARGIN if pet.y < span_h / 2 else max(CORNER_MARGIN, span_h - CORNER_MARGIN)
        return x, y

    flagging = [False]

    def tick():
        # Only act on the edges, so the pet keeps walking to the corner it
        # first chose instead of re-picking the nearest one as it moves, and
        # stays put while sessions come and go underneath it.
        raised = bool(watch["sessions"])
        if raised and not flagging[0]:
            pet.summon(nearest_corner())
        elif not raised and flagging[0]:
            pet.release()
        flagging[0] = raised

        pet.update(TICK_MS / 1000.0, frozen=held["hover"] or held["menu"])
        sync_input_region()
        win.queue_draw()
        return GLib.SOURCE_CONTINUE

    monitor = None
    if not args.no_orbh:
        def apply_sessions(sessions):
            watch["sessions"] = sessions
            win.queue_draw()
            return GLib.SOURCE_REMOVE

        # The monitor reports from its own threads; hop to the GTK loop first.
        monitor = orbh.OrbhMonitor(
            lambda sessions: GLib.idle_add(apply_sessions, sessions)
        )
        monitor.start()

    GLib.timeout_add(TICK_MS, tick)
    try:
        Gtk.main()
    finally:
        if monitor:
            monitor.stop()


if __name__ == "__main__":
    main()
