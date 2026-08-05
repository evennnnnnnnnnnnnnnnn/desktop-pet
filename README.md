# desktop-pet

A sprite that wanders randomly around your desktop.

Draws itself into a transparent, click-through **Wayland layer-shell overlay** covering
one monitor. Walking is just moving a point on that canvas, so the compositor is never
asked to reposition a window — which is what makes this work on compositors like
Hyprland, Sway, and river, where clients cannot place their own windows.

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
| `--monitor` | `0` | which monitor the pet lives on |
| `--scale` | `0.5` | sprite scale factor |
| `--speed` | `90` | walking speed, px/sec |
| `--dwell MIN MAX` | `1.5 6.0` | seconds to pause between walks |

Stop it with `Ctrl-C`.

## How it wanders

A two-state loop: pick a uniformly random point on the monitor, walk to it in a straight
line at `--speed`, pause for a random interval in `--dwell`, repeat. The pet faces the
direction it is travelling.

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
    "walk-left":  { "row": 2, "frames": 8, "fps": 7.5 }
  }
}
```

Only those three animations are used. Drop in any image format GdkPixbuf can read
(PNG, WebP, JPEG) and adjust `frame` and `animations` to match.

## Credits

The bundled sprite (`assets/pet.webp`) is the default pet spritesheet from
[OpenPets](https://github.com/alvinunreal/openpets), used under the MIT License.
Its row layout is OpenPets' "universal" pet format, so spritesheets from OpenPets pets
drop in unchanged.
