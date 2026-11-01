---
read_when: When working on sound design, macros or release packaging.
---

# Instrument design and release guidance

Use these as design options and validation criteria, not mandatory layer counts or a fixed aesthetic.

## Performance identity first

Define the instrument's role and test phrase before adding layers or space. Extra layers, slow modulation and reverb can make Lead, Keys and Pad presets converge on the same playing response.

- Build a stable, clear center first. One or two core layers can carry identity; extra layers should have specific jobs: body, thickness, overtones, contact transient, air, resonance or spatial response.
- Lead needs deliberate legato, glide and release behavior. Keys needs clear onset and natural decay. Pad benefits from sustained development. Contact/strike transients may retrigger independently when the design requires it.
- Fade details by velocity and restrict complex overtones/contact noise in lower registers. Do not expose every layer on every keypress by default.
- Borrow ensemble relationships: complementary registers, attacks, durations, dynamics and positions with a consistent shared acoustic character. A layer-count requirement alone does not establish useful orchestration.

## Stable center, moving surroundings

Keep core position and articulation stable. Use smaller peripheral movement at differing rates. Treat short reflections, middle-distance responses and long tails as separate roles. Band-limit delay returns and reduce their prominence while the body speaks when appropriate.

The reported Candlewick Lead capture had stereo correlation near 0.968 and a close model match despite extensive space processing. This describes one capture; it is a reminder to measure and listen rather than infer width from module count. Check headphones, speakers and mono separately. Correlation alone cannot determine direction or comfort.

## Performance controls

An optional shared layout proven useful in the reported library:

| Control | MIDI CC | Intended role |
|---|---|---|
| Warmth knob | 21 | Tonal warmth/body balance. |
| Glow knob | 22 | Upper harmonic presence. |
| Motion knob | 23 | Movement amount. |
| Space knob | 24 | Spatial-return presence. |
| Body fader | 25 | Core contribution. |
| Air fader | 26 | Air/noise contribution. |
| Texture fader | 27 | Detail contribution. |
| Halo fader | 28 | Surrounding response. |

Preserve semantic consistency across presets while tailoring ranges to each instrument. Test meaningful endpoints: Motion=0 stops the movement governed by that macro; Space=0 closes the returns it promises to control. Preserve saved values when the panel opens. Display real layer/output meters plus key, velocity and held-note information when available; avoid decorative meters presented as signal measurements.

The reported delivery contained 16 independent presets, eight layers each, four knobs/four faders, real demos/MIDI and three representative model analyses. Treat these counts as project-specific evidence, not future requirements. A reported Keys match also favored slow attack; a target match and a facet ranking can disagree.

## File responsibilities and release checks

One independently playable Program per `.uvip` is a useful library unit. A Multi embeds Program states but also contains performance routing and Master state. Save both deliberately; do not infer that separate preset files were updated from a successful Multi save.

Suggested organization:

```text
Instrument Project/
  work/
    Sources/
    Scripts/
    Backups/
    Raw Captures/
  releases/
    Instrument Library v1.0.0/
      Presets/
        Leads/
        Keys/
        Pads/
      Previews/
      Demo MIDI/
      Validation/
      Start Playing.uvim
      Listen.html
      Read Me.md
      manifest.json
```

- Keep playable presets in Presets, audio in Previews. Preserve raw captures; preview processing can be limited to trim, short fades and audition-level normalization where appropriate.
- Use English names and UTF-8 scripts/configuration/docs. Name backups by date or milestone. Make a new version directory for the next release.
- Prefer relative internal resource references where supported. Retain and hash real dependencies. Inspect accumulated directories and stale script-path metadata; remove only references confirmed unused. Embedded portable scripts and local MCP launchers have different portability requirements.
- Record version, preset names, roles, macro/CC mappings, dependencies, hashes, test environment and validation results in the manifest. Do not mark native preview/UFS/indexing complete from file existence alone.
- Copy the release to a fresh location, load each Program natively, play an appropriate phrase, verify macros at useful endpoints, save/reopen and check dependencies. Separately test the browsing Multi and any DAW template.
- Label local MCP templates as machine-specific. Portable performances must not silently depend on a developer checkout, private runtime home or original working directory.

Keep validation evidence for instrument sound, performance routing, standalone Program persistence, preview playback and native browser association separately. No reliable UFS/index/native-preview pipeline was established in the reported project.
