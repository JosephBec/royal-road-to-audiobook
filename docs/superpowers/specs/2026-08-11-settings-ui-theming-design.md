# Settings UI polish + Theme tab — design & plan

Date: 2026-08-11. Approved verbally in-session ("just write the spec, then plan it, and then build it").

## Goals

1. The settings modal stops changing height per tab — no more buttons jumping around.
2. A fourth settings tab, **Theme**: pick a base theme and an accent color.
3. Favorites: explicitly untouched (user decision).
4. Auth/internet exposure: separate project, comes after this.

## 1. Fixed-height settings modal

`#modal-settings .modal-content` becomes a flex column with `height: min(640px, 85vh)`
and `overflow: hidden`. Title and tab row pinned at top (`flex-shrink: 0`), the
`.modal-actions` row pinned at bottom, and the active `.settings-panel` scrolls
in between (`flex: 1 1 auto; min-height: 0; overflow-y: auto;
overscroll-behavior: contain; touch-action: pan-y` — the same containment the
Pronunciation modal's `.modal-wide > .settings-panel` already uses). Every tab is
the same height; switching tabs moves nothing but panel content.

`#voice-demo-list` loses its own `max-height: 50vh` scroll — the panel is the
scroller now; nested scroll regions on iOS are misery.

## 2. Theme tab

Appended after Voices: Playback | Export | Voices | Theme.

### Base themes (swatch cards)

Four `data-theme` values, each a token block in style.css (the app is already
fully tokenized on CSS custom properties):

- `dark` — current dark (default)
- `oled` — true-black backgrounds (OLED phones, battery)
- `light` — current light
- `warm` — sepia/paper reading tones

Swatches are rendered by JS from a `THEMES` registry (id, name, family,
preview colors) as tappable cards showing a mini preview (bg / card / text /
accent dot) with the active one ring-highlighted. The old "Default Theme"
select in the Playback tab is removed — the swatches replace it.

The top-bar 🌙 toggle survives: each theme belongs to a family (`dark`:
dark+oled, `light`: light+warm). Toggling switches to the last-used theme of
the *other* family (remembered in localStorage, defaults dark ↔ light).

### Accent color

A row of 8 preset dots (current purple first = "default") plus one
`<input type="color">` for arbitrary colors. Only `--accent` is stored;
`--accent-hover` and `--accent-dim` are derived in CSS with `color-mix()`
(hover lightens on dark-family themes, darkens on light-family — replacing
today's hand-picked literals). A custom accent is applied as an inline
`--accent` on `<html>`, which wins over the stylesheet default.

Picking the default purple dot clears the stored accent (PUT `accent: ""` →
NULL in DB) rather than storing the purple hex, so themes keep owning their
default.

### Persistence

Settings DB, like everything else (syncs across devices):

- `settings.theme` now accepts `dark|light|oled|warm` (router validation widened).
- New nullable `settings.accent` column (TEXT, `#rrggbb` or NULL = default);
  hand-rolled migration entry in `_migrate_schema()`. Router validates
  `^#[0-9a-fA-F]{6}$`, empty string clears.

### iOS status bar

New `<meta name="theme-color">` in index.html; JS keeps its `content` equal to
the computed `--bg-primary` whenever the theme changes, so the Safari chrome
matches instead of defaulting white.

## Implementation plan

1. **Backend**: `Settings.accent` column + migration entry; widen theme
   validation, add accent validation + response field in routers/settings.py.
   Tests: accent round-trip, `""` clears, bad hex rejected, `oled`/`warm`
   accepted, `sepia` still rejected.
2. **CSS**: `[data-theme="oled"]` and `[data-theme="warm"]` token blocks;
   accent hover/dim via `color-mix()`; fixed-height settings modal rules;
   swatch/accent-dot/stacked-row styles; drop `#voice-demo-list` max-height.
3. **HTML**: theme-color meta; Theme tab button + panel (empty containers,
   JS-rendered); remove Default Theme row from Playback.
4. **JS**: `THEMES`/`ACCENT_PRESETS` registries; `applyTheme` (family memory +
   meta sync), `applyAccent`, family-aware `toggleTheme`; `renderThemePanel()`
   + wiring in `openSettings`/`switchSettingsTab`; boot applies accent; remove
   old select listener.
5. Run full test suite, commit, restart server via tray-compatible child, and
   verify `/api/version`.

Out of scope: favorites changes, auth (next project), per-novel theming.
