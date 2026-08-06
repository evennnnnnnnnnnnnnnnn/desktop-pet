# desktop-pet

A sprite that wanders randomly around your desktop.

Draws itself into a transparent **Wayland layer-shell overlay** covering one monitor.
Walking is just moving a point on that canvas, so the compositor is never asked to
reposition a window — which is what makes this work on compositors like Hyprland, Sway,
and river, where clients cannot place their own windows.

- **Hover it and it stops.** It holds its target and carries on once you move away.
- **Right-click it for a menu** of launchers, plus Quit.
- **It watches your agents.** If an [Orbh](#watching-orbh-sessions) session anywhere on the
  machine blocks on you, the pet walks to the nearest corner, waves, and says which one.
- Everything except the pet itself is click-through: the overlay's input region is kept
  clipped to the sprite's own opaque pixels, so only a sprite-sized box swallows clicks.

## Requirements

```bash
sudo pacman -S --needed gtk-layer-shell python-gobject python-cairo
```

(`gtk-layer-shell` is in the official `extra` repo.)

## Run

```bash
./pet.py                      # wander on monitor 0
./pet.py --list-monitors      # see what's available
./pet.py --monitor 1 --scale 0.7 --speed 140
```

| Flag | Default | Meaning |
|---|---|---|
| `--sprite` | `assets/pet.json` | sprite manifest to load |
| `--menu` | `assets/menu.json` | right-click menu entries |
| `--monitor` | `0` | which monitor the pet lives on |
| `--scale` | `0.5` | sprite scale factor |
| `--speed` | `90` | walking speed, px/sec |
| `--dwell MIN MAX` | `1.5 6.0` | seconds to pause between walks |
| `--no-orbh` | off | don't watch Orbh sessions |
| `--attach-command` | `kitty -e flint orbh attach {id} -p {path}` | run when an interactive session is picked |
| `--inspect-command` | `kitty --hold -e flint orbh page {id} -p {path}` | run for sessions with no terminal to attach to |

Stop it with `Ctrl-C`.

## How it wanders

A two-state loop: pick a uniformly random point on the monitor, walk to it in a straight
line at `--speed`, pause for a random interval in `--dwell`, repeat. The pet faces the
direction it is travelling.

## The right-click menu

`assets/menu.json` is a list of launchers. A `Quit` item is always appended.

```json
[
  { "label": "Terminal", "command": "kitty" },
  { "label": "Browser",  "command": "firefox" }
]
```

`command` is run directly, not through a shell, so it takes arguments but not pipes or
globs. Delete the file for a menu with nothing but `Quit`.

When any agent sessions are live, they are listed above the launchers — the ones waiting
on you first, marked `needs input`. Picking one attaches to it.

## Watching Orbh sessions

[Orbh](https://github.com/nuu-cognition) supervises agent sessions (Claude Code, Codex)
across your Flints. A session that blocks on a human sits at `needs-input` until you
answer it, and nothing on Linux tells you — Orbh's own desktop notification is macOS-only.
That is the gap the pet fills.

Discovery is two hops, because **Blacksmith is a port registry, not a session API**:

1. Read `~/.nuucognition/blacksmith/blacksmith.json` for Blacksmith's endpoint, then ask
   it for the `flint-server` processes it has spawned — one per open Flint.
2. Subscribe to each server's `/events/stream` and read its `/orbh/sessions`.

The pet holds an SSE connection per server and refetches when one reports a change, so it
reacts immediately rather than polling. Ports are ephemeral — Blacksmith reaps and
respawns servers — so it re-runs discovery every 30s and whenever a stream drops.

Only `working` and `needs-input` sessions are ever shown. `awaiting` is deliberately
excluded: it means dormant-but-auto-wakeable, which needs no human, and a Flint can hold
hundreds of finished sessions that can never want anything.

Everything here fails soft. If Blacksmith isn't running, or a server dies, or the schema
moves, the pet reports nothing and keeps wandering. Pass `--no-orbh` to switch it off.

Sessions the pet finds are only visible on this machine's loopback interface; the API has
no authentication, so it deliberately never sends one.

## Using a different sprite

`assets/pet.json` describes a grid spritesheet — one animation per row, frames left to
right:

```json
{
  "sprite": "pet.webp",
  "frame": { "width": 192, "height": 208 },
  "animations": {
    "idle":       { "row": 0, "frames": 6, "fps": 1.1 },
    "walk-right": { "row": 1, "frames": 8, "fps": 7.5 },
    "walk-left":  { "row": 2, "frames": 8, "fps": 7.5 },
    "wave":       { "row": 3, "frames": 4, "fps": 6 }
  }
}
```

Only those four animations are used — `wave` is what it plays while flagging a session
that needs you. Drop in any image format GdkPixbuf can read (PNG, WebP, JPEG) and adjust
`frame` and `animations` to match.

## Credits

The bundled sprite (`assets/pet.webp`) is the default pet spritesheet from
[OpenPets](https://github.com/alvinunreal/openpets), used under the MIT License.
Its row layout is OpenPets' "universal" pet format, so spritesheets from OpenPets pets
drop in unchanged.
