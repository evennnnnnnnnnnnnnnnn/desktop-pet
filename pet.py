#!/usr/bin/env python3
"""A desktop pet that wanders randomly across your screen.

Renders as a transparent, click-through Wayland layer-shell overlay covering one
monitor. The pet is drawn inside that overlay, so walking is just moving a point
on a canvas -- the compositor is never asked to reposition a window.
"""

import argparse
import json
import math
import random
from pathlib import Path

import cairo
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("GtkLayerShell", "0.1")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, GtkLayerShell  # noqa: E402

TICK_MS = 16  # ~60fps


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


class Pet:
    """Random-walk state machine: pick a target, walk to it, pause, repeat."""

    def __init__(self, bounds, speed, dwell):
        self.bounds = bounds  # (width, height) of the area it may roam
        self.speed = speed  # px per second
        self.dwell = dwell  # (min, max) seconds to pause between walks
        self.x, self.y = self._random_point()
        self.facing = "right"
        self.walking = False
        self.pause_left = random.uniform(*dwell)
        self.target = (self.x, self.y)
        self.anim_time = 0.0

    def _random_point(self):
        w, h = self.bounds
        return random.uniform(0, w), random.uniform(0, h)

    @property
    def animation(self):
        return f"walk-{self.facing}" if self.walking else "idle"

    def update(self, dt):
        self.anim_time += dt

        if not self.walking:
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
    """A fullscreen, transparent, click-through overlay pinned to `monitor`."""
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
    return win


def make_click_through(win):
    """Empty input region: pointer events fall through to whatever is beneath."""
    win.input_shape_combine_region(cairo.Region())


def main():
    parser = argparse.ArgumentParser(description="A desktop pet that wanders your screen.")
    parser.add_argument("--sprite", default=str(Path(__file__).parent / "assets" / "pet.json"),
                        help="path to the sprite manifest JSON")
    parser.add_argument("--monitor", type=int, default=0, help="monitor index to live on")
    parser.add_argument("--scale", type=float, default=0.5, help="sprite scale factor")
    parser.add_argument("--speed", type=float, default=90.0, help="walking speed in px/sec")
    parser.add_argument("--dwell", type=float, nargs=2, default=(1.5, 6.0),
                        metavar=("MIN", "MAX"), help="seconds to pause between walks")
    parser.add_argument("--list-monitors", action="store_true", help="list monitors and exit")
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
    draw_w = sprite.w * args.scale
    draw_h = sprite.h * args.scale
    # Roam over the area where the whole sprite still fits on screen.
    pet = Pet(
        bounds=(max(1.0, geo.width - draw_w), max(1.0, geo.height - draw_h)),
        speed=args.speed,
        dwell=tuple(args.dwell),
    )

    win = build_overlay(monitor, "desktop-pet")

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
        return False

    win.connect("draw", on_draw)
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    make_click_through(win)

    def tick():
        pet.update(TICK_MS / 1000.0)
        win.queue_draw()
        return GLib.SOURCE_CONTINUE

    GLib.timeout_add(TICK_MS, tick)
    Gtk.main()


if __name__ == "__main__":
    main()
