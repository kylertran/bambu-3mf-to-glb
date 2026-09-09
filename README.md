# bambu-3mf-converter

Convert a multi-color **Bambu Studio** `.3mf` project into a plain-colored **`.glb`**
that any glTF-capable engine or viewer can open (Blender, Unity, Unreal, three.js,
Windows 3D Viewer, https://gltf-viewer.donmccurdy.com, ...).

The paint you apply in Bambu Studio is not a texture or vertex color. Each mesh
triangle carries a `paint_color` hex string that encodes a recursive subdivision
of that triangle with a filament index on every leaf. This script re-implements
Bambu/PrusaSlicer's `TriangleSelector` decoder, rebuilds the sub-triangles, and
writes one flat glTF material per filament, so the result looks exactly like the
model in Bambu Studio.

## Requirements

* Python 3.9+
* `pip install -r requirements.txt` (numpy, plus tkinterdnd2 for drag-and-drop in the GUI)

## GUI

Double-click **`Run GUI.bat`** (or run `python bambu3mf_gui.py`).

* Drag a `.3mf` from File Explorer onto the window, or click **Browse...**.
* Pick the destination with **Save as...** (defaults to a `.glb` next to the input).
* Choose units / plate position, press **Convert**, then **Open output folder**.
* Drop several `.3mf` files at once to batch-convert them into one folder.
* You can also drop a `.3mf` onto `Run GUI.bat` itself to open it pre-loaded.

## Command line

```bash
python bambu3mf_to_glb.py "buff baby world.3mf"
```

This writes `buff baby world.glb` next to the input. Options:

| Flag | Meaning |
| --- | --- |
| `-o OUT.glb` | Output path (default: input name with `.glb`). |
| `--units m\|mm` | Output units. glTF convention is metres (default). Use `mm` to keep 1 unit = 1 mm. |
| `--keep-position` | Keep the model where it sits on the Bambu build plate. By default the model is centred on X/Z and rests on Y = 0. |
| `--palette "#RRGGBB,#RRGGBB,..."` | Override the project's filament colours (filament 1 first). |
| `-q` | Quiet. |

The script prints a per-filament triangle count so you can sanity-check the result.

## What it reads from the 3MF

| File inside the zip | Used for |
| --- | --- |
| `3D/3dmodel.model` and `3D/Objects/*.model` | Meshes, component hierarchy, build-plate transforms, `paint_color` per triangle. |
| `Metadata/model_settings.config` | Default filament ("extruder") and name of every object / part. Unpainted triangles get this colour. |
| `Metadata/project_settings.config` | `filament_colour` table (the AMS slot colours). |

## Output structure

* One glTF node per part, with the original part name and its Bambu transform.
* One mesh primitive per filament colour per part, sharing a single position buffer.
* Materials named `Filament N #RRGGBB`, metallic 0 / roughness 0.7, colours converted from sRGB to linear as glTF requires.
* No normals are written, so viewers compute flat normals (the same faceted look as the slicer).
* 3MF is Z-up millimetres; the root node converts to glTF's Y-up metres.

## Making it small for the web

The output keeps the print-resolution mesh (often a million+ triangles). For three.js or game engines,
decimate and compress it with [glTF-Transform](https://gltf-transform.dev):

```bash
npm install -g @gltf-transform/cli
gltf-transform optimize model.glb model_small.glb --simplify-ratio 0.05 --simplify-error 0.002 --compress false --no-palette
```

Use `--compress draco` or `--compress meshopt` for an even smaller file if your loader has the matching decoder.

## Notes

* The decoder was validated against the Bambu Studio source (`TriangleSelector::deserialize` / `perform_split`)
  and by checking that every sub-triangle set covers exactly the area of its parent.
* PrusaSlicer / OrcaSlicer projects use the same encoding (`slic3rpe:mmu_segmentation`) and should also work.
* Objects that have no paint and no colour table fall back to a built-in palette.

## Credits

* The `paint_color` decoding in `bambu3mf_to_glb.py` (`decode_paint` / `_split_children`) is a Python port of
  `TriangleSelector::deserialize` and `TriangleSelector::perform_split` from
  [PrusaSlicer](https://github.com/prusa3d/PrusaSlicer) (Prusa Research, AGPL-3.0), including the extended
  filament-state encoding added in [Bambu Studio](https://github.com/bambulab/BambuStudio) (Bambu Lab, AGPL-3.0).
* Drag-and-drop uses [tkinterdnd2](https://github.com/pmgagne/tkinterdnd2) (MIT), which bundles
  [TkDND](https://github.com/petasis/tkdnd) (BSD-style licence).
* Geometry handling uses [NumPy](https://numpy.org) (BSD-3-Clause).

Not affiliated with or endorsed by Bambu Lab or Prusa Research.

## Licence

[GNU Affero General Public License v3.0](LICENSE), the same licence as the PrusaSlicer and Bambu Studio
code the decoder is derived from. You are free to use, modify and redistribute this tool; if you distribute
a modified version (including as a network service) you must share your source under the same terms.
