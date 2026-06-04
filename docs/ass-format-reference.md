# The complete ASS subtitle format reference

**The Advanced SubStation Alpha (.ass) format is the most powerful text-based subtitle system in existence**, capable of producing everything from simple dialogue to broadcast-quality motion graphics, karaoke effects, and vector artwork. Originally an extension of the SSA v4 format, ASS (v4.00+) is the de facto standard for anime fansubbing and complex subtitle typesetting, rendered by VSFilter and libass across virtually all modern media players. This reference documents every section, field, override tag, drawing command, and scripting capability available in the format.

---

## File structure and section anatomy

ASS files are plain-text UTF-8 documents (with optional BOM) organized into INI-style sections. Each section begins with a bracketed header. Lines starting with `;` are comments. The sections **must appear in order** and each may occur only once.

| Section | Required | Purpose |
|---------|----------|---------|
| `[Script Info]` | Yes (must be first) | Global metadata and rendering parameters |
| `[V4+ Styles]` | Yes | Style definitions controlling subtitle appearance |
| `[Fonts]` | No | Embedded TrueType fonts (custom UUEncoded binary) |
| `[Graphics]` | No | Embedded image files (custom UUEncoded binary) |
| `[Events]` | Yes (must be last) | All timed subtitle events |

Aegisub adds custom sections like `[Aegisub Project Garbage]` and `[Aegisub Extradata]` for editor-specific metadata. The legacy SSA v4 format uses `[V4 Styles]` instead of `[V4+ Styles]` and has significant differences in field count, color format, and alignment system.

### [Script Info] fields

**Rendering-critical headers:**

| Field | Description | Values |
|-------|-------------|--------|
| `ScriptType` | Format version | `v4.00+` for ASS, `v4.00` for SSA |
| `PlayResX` | Script coordinate width | Positive integer (e.g., `1920`) |
| `PlayResY` | Script coordinate height | Positive integer (e.g., `1080`) |
| `LayoutResX` | Native display width (libass) | Positive integer |
| `LayoutResY` | Native display height (libass) | Positive integer |
| `WrapStyle` | Default line-wrapping | `0`=smart equal, `1`=end-of-line, `2`=no wrap, `3`=smart bottom-wider |
| `ScaledBorderAndShadow` | Scale border/shadow with resolution | `yes` or `no` (always use `yes`) |
| `Collisions` | Overlap handling | `Normal` or `Reverse` |
| `Timer` | Speed multiplier | `100.0000` = normal |
| `YCbCr Matrix` | Color space (libass) | `TV.601`, `TV.709`, `PC.601`, `PC.709`, `None` |

**Informational headers** include `Title`, `Original Script`, `Original Translation`, `Original Editing`, `Original Timing`, `Synch Point`, `Script Updated By`, and `Update Details`—none of which affect rendering.

### Color format: &HBBGGRR& and &HAABBGGRR&

ASS uses a **BGR byte order** (the reverse of HTML's RGB), with an inverted alpha channel where **`&H00` = fully opaque** and **`&HFF` = fully transparent**.

In style definitions, colors use the full `&HAABBGGRR` format including alpha. In override tags, colors use `&HBBGGRR&` (no alpha) and alpha is set separately with `\alpha` or `\1a`–`\4a` tags using `&HAA&`. For example, pure red is `&H0000FF&` (zero blue, zero green, full red), while `&H80FFFFFF` is white at 50% transparency.

---

## All 23 style parameters in [V4+ Styles]

The `Format:` line must precede any `Style:` lines. Values are comma-delimited with no whitespace after commas.

```
Style: Default,Arial,48,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,2,1,2,10,10,30,1
```

| # | Parameter | Type | Description |
|---|-----------|------|-------------|
| 1 | **Name** | String | Unique, case-sensitive style identifier |
| 2 | **Fontname** | String | Font family name as installed in OS |
| 3 | **Fontsize** | Float | Height in points (≤511) |
| 4 | **PrimaryColour** | &HAABBGGRR | Main text fill color |
| 5 | **SecondaryColour** | &HAABBGGRR | Karaoke pre-highlight fill |
| 6 | **OutlineColour** | &HAABBGGRR | Border/outline color |
| 7 | **BackColour** | &HAABBGGRR | Shadow color (and opaque box background) |
| 8 | **Bold** | Boolean | `-1`=true, `0`=false |
| 9 | **Italic** | Boolean | `-1`=true, `0`=false |
| 10 | **Underline** | Boolean | `-1`=true, `0`=false |
| 11 | **StrikeOut** | Boolean | `-1`=true, `0`=false |
| 12 | **ScaleX** | Float | Horizontal scaling (100=normal) |
| 13 | **ScaleY** | Float | Vertical scaling (100=normal) |
| 14 | **Spacing** | Float | Extra letter spacing in pixels |
| 15 | **Angle** | Float | Z-axis rotation in degrees |
| 16 | **BorderStyle** | Integer | `1`=outline+shadow, `3`=opaque box |
| 17 | **Outline** | Float | Border thickness in pixels |
| 18 | **Shadow** | Float | Shadow offset distance in pixels |
| 19 | **Alignment** | Integer | Numpad positions 1–9 |
| 20 | **MarginL** | Integer | Left margin in script pixels |
| 21 | **MarginR** | Integer | Right margin in script pixels |
| 22 | **MarginV** | Integer | Vertical margin in script pixels |
| 23 | **Encoding** | Integer | Font charset (`1`=Default recommended) |

The **numpad alignment** system maps positions intuitively: `1`–`3` for bottom row (left/center/right), `4`–`6` for middle, `7`–`9` for top. Style booleans use `-1`/`0`, while override tag booleans use `1`/`0`.

---

## Events section and line types

The `[Events]` section contains all timed content. The time format is **`H:MM:SS.CC`** (centiseconds, not milliseconds).

```
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.50,0:00:04.20,Default,Speaker,0000,0000,0000,,Hello world!
```

**Event line types:**

| Type | Purpose |
|------|---------|
| `Dialogue:` | Rendered subtitle text displayed at specified times |
| `Comment:` | Same format but ignored during playback |
| `Picture:` | Displays an image file (legacy SSA) |
| `Sound:` | Plays a .wav file (legacy SSA) |
| `Movie:` | Plays an .avi file (legacy SSA) |
| `Command:` | Executes a program (legacy SSA) |

The **Layer** field (integer) controls z-ordering—higher values render on top. The **Effect** field supports three built-in renderer effects: `Banner;delay[;lefttoright;fadeawaywidth]` for horizontal scrolling, `Scroll up;y1;y2;delay[;fadeawayheight]` and `Scroll down;y1;y2;delay[;fadeawayheight]` for vertical scrolling. The **Text** field accepts override tags in `{}` blocks, with `\N` for hard line breaks, `\n` for soft breaks, and `\h` for hard (non-breaking) spaces.

---

## Every override tag documented

Override tags appear inside `{}` blocks in the Text field. They start with `\`, followed by a name and parameter. Omitting the parameter resets to the style default. Tags in the first group below are per-line (appear once); all others modify text following them.

### Text formatting tags

| Tag | Syntax | Description |
|-----|--------|-------------|
| **Bold** | `\b1` `\b0` `\b<weight>` | Toggle bold or set weight (100–900; 400=normal, 700=bold) |
| **Italic** | `\i1` `\i0` | Toggle italic |
| **Underline** | `\u1` `\u0` | Toggle underline |
| **Strikeout** | `\s1` `\s0` | Toggle strikethrough |
| **Font name** | `\fn<name>` | Set font face (e.g., `\fnArial`) |
| **Font size** | `\fs<size>` | Set font height in script pixels (integer) |
| **Font scale X** | `\fscx<percent>` | Horizontal scale (100=normal). Example: `\fscx150` = 50% wider |
| **Font scale Y** | `\fscy<percent>` | Vertical scale. Example: `\fscy50` = half height |
| **Letter spacing** | `\fsp<pixels>` | Inter-character spacing (can be negative/decimal) |
| **Font encoding** | `\fe<id>` | Override charset (0=ANSI, 1=Default, 128=Shift-JIS, etc.) |
| **Wrap style** | `\q<0-3>` | Line-breaking mode for this line |
| **Reset style** | `\r` or `\r<style>` | Cancel all overrides; optionally reset to a named style |

Example: `I am {\b1}not{\b0} amused.` renders "not" in bold. `{\fscx200\fscy200}` doubles text size via scaling rather than hinting.

### Rotation and shearing tags

| Tag | Syntax | Description |
|-----|--------|-------------|
| **Z-axis rotation** | `\frz<degrees>` or `\fr<degrees>` | 2D rotation counterclockwise. `\frz180` = upside-down |
| **X-axis rotation** | `\frx<degrees>` | Top tilts into screen (positive). Creates perspective depth |
| **Y-axis rotation** | `\fry<degrees>` | Left edge moves into screen (positive) |
| **X shearing** | `\fax<factor>` | Horizontal perspective distortion. Range: typically -2 to 2 |
| **Y shearing** | `\fay<factor>` | Vertical perspective distortion. Applied after rotation |

Rotations are performed around the origin point set by `\org`. The `\fr` tag is simply a shorthand for `\frz`. Example: `{\t(\frz3600)}` animates 10 full counterclockwise rotations. Combining `\frx30\fry-20\frz10` creates convincing 3D perspective.

### Color and transparency tags

| Tag | Syntax | Description |
|-----|--------|-------------|
| **Primary color** | `\c&HBBGGRR&` or `\1c&HBBGGRR&` | Main text fill color |
| **Secondary color** | `\2c&HBBGGRR&` | Karaoke pre-highlight color |
| **Outline color** | `\3c&HBBGGRR&` | Border color |
| **Shadow color** | `\4c&HBBGGRR&` | Shadow color |
| **All alpha** | `\alpha&HAA&` | Set transparency for all four components |
| **Primary alpha** | `\1a&HAA&` | Fill transparency |
| **Secondary alpha** | `\2a&HAA&` | Karaoke pre-highlight transparency |
| **Outline alpha** | `\3a&HAA&` | Border transparency |
| **Shadow alpha** | `\4a&HAA&` | Shadow transparency |

Alpha `&H00&` = fully visible, `&HFF&` = fully invisible. Example: `{\1a&HFF&}` makes the fill invisible, leaving only border and shadow—a common technique for outline-only text.

### Positioning and movement tags

| Tag | Syntax | Description |
|-----|--------|-------------|
| **Position** | `\pos(x,y)` | Static position in script coordinates. Anchor depends on `\an` |
| **Movement** | `\move(x1,y1,x2,y2)` | Constant-speed movement over line duration |
| **Timed movement** | `\move(x1,y1,x2,y2,t1,t2)` | Movement between t1 and t2 (ms from line start) |
| **Rotation origin** | `\org(x,y)` | Fixed origin for all rotations. Cannot be animated |
| **Alignment** | `\an<1-9>` | Numpad-style positioning (2=bottom-center default) |
| **Legacy alignment** | `\a<pos>` | SSA alignment (1–3=bottom, 5–7=top, 9–11=middle) |

Only one `\pos` or `\move` may appear per line. Before t1, text stays at (x1,y1); after t2, it stays at (x2,y2). The `\org` tag enables precise control over 3D rotations—placing it at a scene's vanishing point produces correct perspective. Placing it far off-screen (e.g., `\org(10000,0)`) combined with small `\frz` rotations can simulate curved motion paths.

### Karaoke tags

| Tag | Syntax | Effect |
|-----|--------|--------|
| `\k` | `\k<duration>` | Instant fill: secondary → primary color when syllable starts |
| `\K` or `\kf` | `\K<duration>` | Sweep fill: left-to-right color transition during syllable |
| `\ko` | `\ko<duration>` | Outline reveal: border hidden before, appears at highlight |
| `\kt` | `\kt<time>` | Set absolute syllable start time (ms from line start) |

Duration is in **centiseconds** (100 = 1 second). Example: `{\k45}Twink{\k38}le {\k42}twink{\k38}le` times each syllable. The `\kt` tag (v4++ addition) is the only tag to set absolute timing rather than duration.

### Fade and transparency tags

| Tag | Syntax | Description |
|-----|--------|-------------|
| **Simple fade** | `\fad(fadein,fadeout)` | Fade in/out in milliseconds. `\fad(1200,250)` |
| **Complex fade** | `\fade(a1,a2,a3,t1,t2,t3,t4)` | Five-stage alpha transition with 3 alpha values and 4 times |

The complex `\fade` uses **decimal** alpha values (0=visible, 255=invisible) and times in milliseconds. Timeline: alpha a1 → fade to a2 (t1–t2) → hold a2 (t2–t3) → fade to a3 (t3–t4) → hold a3. Example: `\fade(255,32,224,0,500,2000,2200)` starts invisible, fades to near-opaque, then fades to near-invisible.

### Border and shadow tags

| Tag | Syntax | Description |
|-----|--------|-------------|
| **Border** | `\bord<size>` | Outline width in pixels (can be decimal). `\bord0` disables |
| **X border** | `\xbord<size>` | Horizontal border only |
| **Y border** | `\ybord<size>` | Vertical border only |
| **Shadow** | `\shad<depth>` | Shadow offset (both axes). Cannot be negative |
| **X shadow** | `\xshad<depth>` | Horizontal shadow offset (can be negative) |
| **Y shadow** | `\yshad<depth>` | Vertical shadow offset (can be negative) |
| **Blur edges** | `\be<strength>` | Integer-based edge blur (iterated box blur) |
| **Gaussian blur** | `\blur<strength>` | Float-capable Gaussian edge blur. Preferred over `\be` |

Both `\be` and `\blur` affect the outermost visible edge—the border if one exists, otherwise the fill. High `\blur` values are CPU-intensive. Negative shadow offsets via `\xshad`/`\yshad` can position shadows above or to the left of text.

### Animation tag

```
\t(<style modifiers>)
\t(<accel>,<style modifiers>)
\t(<t1>,<t2>,<style modifiers>)
\t(<t1>,<t2>,<accel>,<style modifiers>)
```

The `\t` tag performs gradual animated transitions. Times are in milliseconds relative to line start. The **acceleration parameter** follows y = x^accel: `1` = linear, `<1` = fast start/slow end (ease-out), `>1` = slow start/fast end (ease-in).

**Animatable tags:**

| Category | Tags |
|----------|------|
| Font/Color | `\fs`, `\fsp`, `\c`, `\1c`, `\2c`, `\3c`, `\4c`, `\alpha`, `\1a`–`\4a` |
| Geometry | `\fscx`, `\fscy`, `\frx`, `\fry`, `\frz`, `\fr`, `\fax`, `\fay` |
| Effects | `\bord`, `\xbord`, `\ybord`, `\shad`, `\xshad`, `\yshad`, `\clip` (rect), `\iclip` (rect), `\be`, `\blur` |

Example: `{\1c&HFF0000&\t(\1c&H0000FF&)}` fades from blue to red. `{\an5\fscx0\fscy0\t(0,500,\fscx100\fscy100)}Boo!` zooms text from invisible to full size. Multiple `\t` tags can be chained: `{\t(0,1000,\1c&H00FF00&)\t(1000,2000,\1c&H0000FF&)}`.

### Clip and mask tags

| Tag | Syntax | Description |
|-----|--------|-------------|
| **Rectangular clip** | `\clip(x1,y1,x2,y2)` | Show only within rectangle |
| **Rectangular inverse clip** | `\iclip(x1,y1,x2,y2)` | Hide within rectangle |
| **Vector clip** | `\clip([scale,]<drawing commands>)` | Show only within vector shape |
| **Vector inverse clip** | `\iclip([scale,]<drawing commands>)` | Hide within vector shape |

Rectangular clips can be animated with `\t`; **vector clips cannot**—frame-by-frame lines are required for animated vector masks. Coordinates are in script resolution pixels relative to the video's top-left corner. Example: `\clip(1,m 50 0 b 100 0 100 100 50 100 b 0 100 0 0 50 0)` clips to a pseudo-circle.

### Drawing mode tags

| Tag | Syntax | Description |
|-----|--------|-------------|
| **Drawing mode** | `\p<scale>` | `\p0`=off, `\p1`=on (1:1), `\p2`=2× resolution, `\p4`=8× resolution |
| **Baseline offset** | `\pbo<offset>` | Y-offset applied to all drawing coordinates |

Scale formula: coordinates are divided by **2^(scale-1)**. Higher scales enable sub-pixel precision for smooth curves.

---

## Drawing commands for vector graphics

Drawing commands appear **outside** override blocks, between `{\p1}` and `{\p0}`, or inside `\clip()`. They use an invisible cursor model.

| Command | Syntax | Description |
|---------|--------|-------------|
| **Move** | `m x y` | Move cursor to (x,y). Auto-closes any open shape. **Required as first command** |
| **Move (no close)** | `n x y` | Move cursor without closing current shape |
| **Line** | `l x y` | Draw straight line to (x,y). Multiple points: `l x1 y1 x2 y2 x3 y3` |
| **Cubic Bézier** | `b x1 y1 x2 y2 x3 y3` | Curve from cursor to (x3,y3) with control points (x1,y1) and (x2,y2) |
| **B-spline** | `s x1 y1 x2 y2 x3 y3 [... xN yN]` | Cubic uniform B-spline through N points (min 3 pairs) |
| **Extend spline** | `p x y` | Add point to current B-spline |
| **Close spline** | `c` | Smoothly close current B-spline |

Drawings use **primary color** (`\1c`) for fill, **outline color** (`\3c`) for borders, and **shadow color** (`\4c`) for shadows. All styling tags (rotation, scaling, blur, etc.) apply to drawings. **Overlapping shapes XOR**, creating holes—useful for ring shapes or letter cutouts.

### Common shape examples

```
Square:      {\p1}m 0 0 l 100 0 100 100 0 100{\p0}
Triangle:    {\p1}m 50 0 l 100 100 0 100{\p0}
Diamond:     {\p1}m 50 0 l 100 50 50 100 0 50{\p0}
Circle (4 Bézier arcs):
  {\p1}m 50 0 b 77 0 100 23 100 50 b 100 77 77 100 50 100
  b 23 100 0 77 0 50 b 0 23 23 0 50 0{\p0}
Rounded square: {\p1}m 0 0 s 100 0 100 100 0 100 c{\p0}
```

---

## Creative effects achievable with ASS tags

### Multi-layer composition builds complex visuals

The **Layer** field enables stacking multiple dialogue lines at identical timing. Higher layers render on top. A common **triple-layer glow effect**:

- Layer 0: `{\pos(640,360)\3c&H000080&\bord10\blur3\shad0}Text` (outer glow)
- Layer 1: `{\pos(640,360)\3c&H0000FF&\bord4\shad0}Text` (inner border)
- Layer 2: `{\pos(640,360)\bord0\shad0}Text` (clean fill)

**Gradient text** is achieved by creating ~100 copies of the same line, each with a unique `\clip(x1,y1,x2,y2)` covering a 1–2px vertical strip and an interpolated `\c` value. Automation scripts like GradientEverything handle this automatically.

### Motion tracking and sign translation techniques

Professional typesetting matches on-screen signs using `\pos`, `\frz`, `\frx`, `\fry`, `\fax`, `\fay` to place translated text at exact positions with correct perspective. **Motion tracking** uses external tools (Mocha, Blender) to export tracking data, applied via Aegisub-Motion to generate frame-by-frame position/rotation/scale values.

Background masking creates colored rectangles with `\p1` drawing mode to cover original text before placing translations on top. The `\blur` tag softens masks to match video focus.

### The "shadow trick" and other advanced techniques

Making fill nearly invisible with `\1a&HFE&` or using `\ko0` leaves only the shadow visible. This enables high-blur effects without hard edges, `\t`-animatable motion, and precise control over glow appearance. **Frame-by-frame (FBF) animation** creates one subtitle line per video frame for effects that exceed `\t` capabilities, such as complex motion paths, particle effects, or per-frame color changes.

Karaoke effects combine `\k`/`\K`/`\kf` timing with `\t` animations for per-syllable scaling, color transitions, and movement. Multi-layer karaoke uses separate glow and fill layers with synchronized timing.

---

## Aegisub automation and Lua scripting

### Automation 4 API runs Lua 5.1 scripts

Aegisub's scripting system enables programmatic manipulation of ASS files. Scripts register as **macros** (menu items) or **export filters**:

```lua
script_name = "My Script"
function process(subtitles, selected_lines, active_line)
    for _, i in ipairs(selected_lines) do
        local line = subtitles[i]
        line.text = line.text:gsub("old", "new")
        subtitles[i] = line
    end
    aegisub.set_undo_point("My Script")
end
aegisub.register_macro(script_name, "Description", process)
```

The `subtitles` object provides array-like access to all lines: `subtitles[i]` returns a table with `class`, `text`, `start_time`, `end_time`, `style`, `actor`, `layer`, `effect`, and margin fields. Key APIs include `aegisub.dialog.display()` for UI dialogs, `aegisub.text_extents()` for text measurement, `aegisub.frame_from_ms()`/`aegisub.ms_from_frame()` for frame-time conversion, and `aegisub.progress.set()` for progress reporting.

Standard include modules: **karaskel.lua** (karaoke skeleton with text layout), **utils.lua** (table/string/color utilities), **unicode.lua** (UTF-8 operations), **re** (ICU regex), and **clipboard** (system clipboard access). Aegisub also supports **MoonScript** natively, a cleaner-syntax language that compiles to Lua.

### Karaoke Templater generates effects from marked lines

The built-in Karaoke Templater processes specially marked lines via the Effect field to auto-generate styled karaoke output:

| Effect field | Line type |
|-------------|-----------|
| `template line` / `template syl` / `template char` | Template definitions |
| `code once` / `code line` / `code syl` | Lua code execution |
| `fx` | Generated output (replaced on re-run) |
| `karaoke` or empty | Timed input lyrics |

**Inline variables** (`$start`, `$end`, `$dur`, `$x`, `$y`, `$width`, `$height`, `$left`, `$right`, `$top`, `$bottom`, `$mid`, `$kdur`, `$i`, etc.) are replaced before template execution. **Inline code blocks** (`!...!`) execute Lua and insert the return value: `!$end+200!` computes a value 200ms after syllable end.

**Modifiers** control behavior: `notext` suppresses appending syllable text, `noblank` skips whitespace syllables, `repeat N`/`loop N` executes N times (with `j` counter), `keeptags` preserves original tags, `fx name` filters by inline-fx group, and `fxgroup name` enables conditional template groups.

The `retime()` function adjusts output timing with modes like `"syl"`, `"presyl"`, `"line"`, `"start2syl"`, and `"sylpct"` for precise temporal control. Three major templaters exist: the stock Aegisub templater, KaraOK (extended utility library), and The0x539's Templater (with mixins and nested loops).

### Community scripts and external tools extend the format

**DependencyControl** serves as a package manager for Aegisub scripts with automatic updates and dependency resolution. Key community script collections include lyger's scripts (GradientEverything, Frame-by-frame Transform, Blur and Glow, Image to ASS), line0's ASSFoundation (core tag parsing library), arch1t3cht's Perspective tools, and petzku's Typewriter effect scripts.

External tools for programmatic ASS processing include **pysubs2** (Python library supporting read/write/convert of ASS, SRT, WebVTT, and more), **ASS.js** (JavaScript DOM-based renderer), **libass** (the portable C rendering library used by mpv, VLC, and FFmpeg), and **FFmpeg** itself for subtitle extraction, burning-in, and format conversion.

---

## VSFilter vs libass renderer differences

| Feature | VSFilter | libass |
|---------|----------|--------|
| Platform | Windows (DirectShow) | Cross-platform (mpv, VLC, FFmpeg) |
| Font matching | Windows GDI | Fontconfig / CoreText |
| `\be` rendering | Reference implementation | May differ visually |
| Color space | Implicit BT.601 | Respects `YCbCr Matrix` header |
| Complex clips | May crash with many clips | More stable |

Scripts should always set `ScaledBorderAndShadow: yes` and specify `YCbCr Matrix` for consistent cross-renderer results. VSFilter variants include xy-VSFilter (more stable), XySubFilter (renderer-agnostic), and VSFilterMod (with non-standard extensions). The `\kt` tag, added by v4++, is supported by both VSFilter and libass but remains the only v4++ tag with broad adoption.

## Conclusion

The ASS format's power lies in the interaction between its layered architecture, comprehensive override tags, vector drawing system, and scriptable automation pipeline. **Thirty-seven distinct override tags** cover every aspect of text appearance, positioning, animation, and clipping. The drawing command system provides a complete vector graphics language. The karaoke templater and Lua automation API transform these primitives into arbitrarily complex procedural effects. While no single tag is individually complex, their combinations—multi-layer composition, frame-by-frame generation, clip-based gradients, perspective-matched typesetting—enable effects that rival dedicated motion graphics software, all encoded in a human-readable text file that renders in real time.