# Export one instrument for programmers

`falcon_export_instrument` parses one Falcon Program into an ordered hierarchy and its associations.
It writes six local files under `<FALCON_MCP_HOME>/reports/<export_id>.*`. Open the **HTML explorer** first:

| File | Contents |
| --- | --- |
| `.html` | Standalone interactive explorer: grouped overview, expandable tree, search, module parameters and local connections |
| `.md` | Numbered hierarchy, association table, per-node parameter values, errors and limitations |
| `.json` | Full ordered nodes, children, parent, selectors, parameters, connections and source identity |
| `.mmd` | Simplified composition diagram in Mermaid; collection wrappers and connection records are hidden |
| `.puml` | The same simplified composition in PlantUML |
| `.svg` | Legible, fixed-width overview: program-level modules, Layer cards and aux returns |

## Reading the visual export

The default view is an overview, not a diagram with every record and wire on it. Layers and aux returns
are separate groups; each card summarizes sources, effects, modulation and routing. Click a card or
tree item to inspect a module. **Modules / Parameters / Connections** show its children, parameter values
and direct incoming/outgoing connections. You can filter connections by modulation/routing and follow
resolved endpoints to another module. Unresolved external sources remain text rather than invented nodes.

The tree hides `Properties`, `Connections`, `SignalConnection`, script payloads and collection-only
wrappers by default. Enable **Show XML / data containers** to see the full raw hierarchy. Search includes
module names, paths and parameter names; repeated names show their owning scope. Tree branches and
long lists are loaded in small batches. The page embeds its data and needs no network connection,
renderer service or JavaScript library. Serialized scripts are displayed as text, never run.

## From a saved instrument

```python
falcon_export_instrument(source_path="F:/Instruments/Keys.uvip")
```

This reads an unprotected `.uvip` or `.uvim` XML file without starting Falcon or modifying the preset.
If the file has multiple Programs, the error lists their exact selectors. Select one explicitly:

```python
falcon_export_instrument(
    source_path="F:/Instruments/Collection.uvim",
    root_path="UVI4/Engine[1]/Synth[1]/Children[1]/Part[2]/Program[1]",
)
```

Use the selector actually returned for your file; layouts differ. XML collection wrappers are retained,
so module order and native selectors remain unambiguous. Attributes remain strings rather than guessed
units or types. Script/text payloads, when present, remain in JSON; they are never executed.
Protected banks and third-party formats such as Kontakt `.nki` cannot be parsed by this tool.

## From an open instrument

Bind the intended instance as usual, then use `falcon_scan_project(root="Program")` (or the exact
Program selector) to capture its public state. Pass the returned report ID:

```python
falcon_export_instrument(report_id="<report_id>")
```

A whole-project report with multiple Programs requires `root_path`, using the Program's exact `path`
or `selector` from `falcon_query_project`. Existing reports still work; new scans also preserve collection
indices on containment edges. The exporter reads only the saved report, so no live instance is needed
at export time. It inherits the scan's errors, completeness, structural changes and non-atomic status.

## Ordering and associations

Nodes are numbered in depth-first order, with each parent's children in serialized/observed order.
`parent`, `children`, `relation` and `index` preserve ownership and sibling position. This is a composition
hierarchy, not a claim about DSP execution order. A sample mapping's private internal zones may not be
visible in a public scan.

The UML sources show containment diamonds between meaningful modules; they deliberately omit the global
modulation/routing wire overlay. Full associations remain in JSON/Markdown and in the HTML inspector,
including destination parameters and settings (Ratio/Bypass where exposed). Check `resolved` on each association: symbolic MIDI
sources, expressions, ambiguous names and unknown routing destinations are not silently resolved.
File parsing resolves unique names in the nearest owning scope; other expressions stay symbolic.

`max_nodes` (default 10,000; maximum 50,000) limits the full selected hierarchy. Exceeding it fails
without exporting a partial hierarchy. XML input is bounded to 32 MiB, 50,000 elements and depth 128.
`diagram_max_nodes` (default 150; maximum 1,000) limits static/UML views only: JSON, Markdown and the
HTML tree retain the entire selection. UML uses a breadth-first budget so sibling Layers are included
before one Layer's deep internals. The SVG summarizes modules in cards rather than drawing every one.
Coverage reports distinguish hidden data records, semantic modules, UML cropping and associations
available in the inspector. `omitted_edges` counts source edges not drawn in the UML, including those
deliberately moved to the inspector; it does not imply missing JSON data.
`include_parameters=False` removes per-node parameter/text payloads while retaining association settings.

The files record a source SHA-256. A successful export proves parsing/export only; it does not prove
that samples exist, a plugin can load the instrument, or the instrument sounds correct.
