#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# The paint_color decoder (decode_paint / _split_children) is a Python port of
# TriangleSelector::deserialize and TriangleSelector::perform_split from
# PrusaSlicer (Prusa Research, AGPL-3.0) as extended in Bambu Studio (Bambu Lab, AGPL-3.0).
"""
bambu3mf_to_glb.py - Convert a Bambu Studio (or PrusaSlicer/OrcaSlicer) 3MF project
into a plain-colored binary glTF (.glb) file.

Bambu Studio stores multi-color "painting" as a per-triangle ``paint_color``
attribute. The attribute is a hex string that encodes a recursive subdivision
of the triangle (each sub-triangle can be split again along 1, 2 or 3 edges)
with a filament index on every leaf. This script re-implements that decoder
(TriangleSelector::deserialize + perform_split from libslic3r), rebuilds the
sub-triangles, and writes them into a GLB where every filament becomes one
flat-colored material.

Usage:
    python bambu3mf_to_glb.py "model.3mf"
    python bambu3mf_to_glb.py "model.3mf" -o out.glb --units mm --keep-position
"""

from __future__ import annotations

import argparse
import json
import os
import re
import struct
import sys
import time
import zipfile
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

import numpy as np

# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

DEFAULT_PALETTE = [  # used only when the project has no filament colour table
    "#00AE42", "#FFFFFF", "#F5222D", "#1890FF", "#FAAD14", "#722ED1", "#13C2C2", "#EB2F96",
    "#A0D911", "#FA541C", "#2F54EB", "#8C8C8C", "#000000", "#FADB14", "#52C41A", "#FF85C0",
]

_HEX_VAL = {c: i for i, c in enumerate("0123456789ABCDEF")}


def local_name(tag: str) -> str:
    """Strip an XML namespace from a tag or attribute name."""
    return tag.rpartition("}")[2]


def attr(elem: ET.Element, name: str, default: Optional[str] = None) -> Optional[str]:
    """Get an attribute regardless of namespace prefix."""
    if name in elem.attrib:
        return elem.attrib[name]
    for k, v in elem.attrib.items():
        if local_name(k) == name:
            return v
    return default


def parse_3mf_transform(text: Optional[str]) -> np.ndarray:
    """
    3MF stores a 4x3 matrix as 12 numbers 'm00 m01 m02 m10 m11 m12 m20 m21 m22 m30 m31 m32'
    applied to row vectors ([x y z 1] * M). Return an equivalent 4x4 column-vector matrix.
    """
    if not text:
        return np.eye(4)
    v = [float(x) for x in text.split()]
    if len(v) != 12:
        raise ValueError(f"Bad 3MF transform: {text!r}")
    m = np.eye(4)
    m[:3, 0] = v[0:3]
    m[:3, 1] = v[3:6]
    m[:3, 2] = v[6:9]
    m[:3, 3] = v[9:12]
    return m


def hex_to_rgb(hex_color: str) -> Tuple[float, float, float]:
    h = hex_color.strip().lstrip("#")
    if len(h) not in (6, 8):
        raise ValueError(f"Bad colour {hex_color!r}")
    return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]


def srgb_to_linear(c: float) -> float:
    """glTF baseColorFactor is linear; Bambu colours are sRGB."""
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #

@dataclass
class Component:
    path: str            # model file inside the zip that holds the referenced object
    object_id: str
    transform: np.ndarray


@dataclass
class ModelObject:
    id: str
    name: str = ""
    # mesh data
    vertices: List[Tuple[float, float, float]] = field(default_factory=list)
    triangles: List[Tuple[int, int, int]] = field(default_factory=list)
    paint: List[Optional[str]] = field(default_factory=list)   # per-triangle paint code or None
    # or assembly data
    components: List[Component] = field(default_factory=list)

    @property
    def has_mesh(self) -> bool:
        return bool(self.triangles)


@dataclass
class BuildItem:
    object_id: str
    transform: np.ndarray


# --------------------------------------------------------------------------- #
# 3MF reading
# --------------------------------------------------------------------------- #

def normalize_zip_path(path: str, base_dir: str = "") -> str:
    path = path.replace("\\", "/")
    if path.startswith("/"):
        return path[1:]
    return os.path.normpath(os.path.join(base_dir, path)).replace("\\", "/")


def find_root_model(zf: zipfile.ZipFile) -> str:
    """Locate the start-part model via _rels/.rels, defaulting to 3D/3dmodel.model."""
    try:
        rels = ET.fromstring(zf.read("_rels/.rels"))
        for rel in rels:
            if local_name(rel.tag) != "Relationship":
                continue
            if (rel.get("Type") or "").endswith("/3dmodel"):
                return normalize_zip_path(rel.get("Target", ""))
    except KeyError:
        pass
    return "3D/3dmodel.model"


def parse_model_file(zf: zipfile.ZipFile, path: str) -> Tuple[Dict[str, ModelObject], List[BuildItem]]:
    """Stream-parse one *.model XML file. Returns (objects by id, build items)."""
    objects: Dict[str, ModelObject] = {}
    build: List[BuildItem] = []
    base_dir = os.path.dirname(path)

    current: Optional[ModelObject] = None
    with zf.open(path) as fh:
        for event, elem in ET.iterparse(fh, events=("start", "end")):
            tag = local_name(elem.tag)
            if event == "start":
                if tag == "object":
                    current = ModelObject(id=elem.get("id", ""), name=elem.get("name", "") or "")
                continue

            # ---- end events ----
            if tag == "vertex":
                current.vertices.append((float(elem.get("x")), float(elem.get("y")), float(elem.get("z"))))
                elem.clear()
            elif tag == "triangle":
                current.triangles.append((int(elem.get("v1")), int(elem.get("v2")), int(elem.get("v3"))))
                code = elem.get("paint_color")
                if code is None:
                    for k, v in elem.attrib.items():
                        if local_name(k) in ("paint_color", "mmu_segmentation"):
                            code = v
                            break
                current.paint.append(code)
                elem.clear()
            elif tag == "component":
                ref = attr(elem, "path")
                current.components.append(Component(
                    path=normalize_zip_path(ref, base_dir) if ref else path,
                    object_id=elem.get("objectid", ""),
                    transform=parse_3mf_transform(elem.get("transform")),
                ))
                elem.clear()
            elif tag == "object":
                objects[current.id] = current
                current = None
                elem.clear()
            elif tag == "item":
                build.append(BuildItem(object_id=elem.get("objectid", ""),
                                       transform=parse_3mf_transform(elem.get("transform"))))
                elem.clear()
            elif tag in ("vertices", "triangles", "mesh", "components", "resources", "build"):
                elem.clear()
    return objects, build


def read_filament_colours(zf: zipfile.ZipFile) -> List[str]:
    """Filament colour table from Metadata/project_settings.config (Bambu/Orca JSON)."""
    for name in ("Metadata/project_settings.config", "Metadata/Slic3r_PE.config"):
        if name not in zf.namelist():
            continue
        raw = zf.read(name).decode("utf-8", errors="replace")
        try:
            cfg = json.loads(raw)
            cols = cfg.get("filament_colour")
            if isinstance(cols, list) and cols:
                return [str(c) for c in cols]
        except json.JSONDecodeError:
            # PrusaSlicer style ini: "; extruder_colour = #FF0000;#00FF00"
            m = re.search(r"^\s*;?\s*(?:extruder_colour|filament_colour)\s*=\s*(.+)$", raw, re.M)
            if m:
                return [c for c in m.group(1).split(";") if c.strip()]
    return []


def read_extruder_overrides(zf: zipfile.ZipFile) -> Tuple[Dict[str, int], Dict[str, str]]:
    """
    Metadata/model_settings.config assigns a default filament ("extruder") to each
    object and to each part (component). Returns ({object_or_part_id: extruder}, {id: name}).
    """
    extruders: Dict[str, int] = {}
    names: Dict[str, str] = {}
    if "Metadata/model_settings.config" not in zf.namelist():
        return extruders, names
    root = ET.fromstring(zf.read("Metadata/model_settings.config"))
    for obj in root.iter():
        if local_name(obj.tag) not in ("object", "part"):
            continue
        oid = obj.get("id")
        if oid is None:
            continue
        for md in obj:
            if local_name(md.tag) != "metadata":
                continue
            if md.get("key") == "extruder":
                try:
                    extruders[oid] = int(md.get("value", "1"))
                except ValueError:
                    pass
            elif md.get("key") == "name":
                names[oid] = md.get("value", "")
    return extruders, names


# --------------------------------------------------------------------------- #
# Paint-code decoding (port of libslic3r TriangleSelector)
# --------------------------------------------------------------------------- #

Vec = Tuple[float, float, float]


def _mid(a: Vec, b: Vec) -> Vec:
    return ((a[0] + b[0]) * 0.5, (a[1] + b[1]) * 0.5, (a[2] + b[2]) * 0.5)


def _split_children(tri: Tuple[Vec, Vec, Vec], split_sides: int, special: int) -> List[Tuple[Vec, Vec, Vec]]:
    """Mirror of TriangleSelector::perform_split: children in the same order as tr.children[]."""
    a = tri[special % 3]
    b = tri[(special + 1) % 3]
    c = tri[(special + 2) % 3]
    if split_sides == 1:
        m = _mid(c, b)
        return [(a, b, m), (m, c, a)]
    if split_sides == 2:
        mab = _mid(b, a)
        mca = _mid(a, c)
        return [(a, mab, mca), (mab, b, mca), (b, c, mca)]
    # split_sides == 3
    mab = _mid(b, a)
    mbc = _mid(c, b)
    mca = _mid(a, c)
    return [(a, mab, mca), (mab, b, mbc), (mbc, c, mca), (mab, mbc, mca)]


def decode_paint(code: str, tri: Tuple[Vec, Vec, Vec]) -> List[Tuple[Tuple[Vec, Vec, Vec], int]]:
    """
    Decode one paint_color string for the triangle `tri` (3 vertex positions).
    Returns a list of (sub_triangle, state). state 0 = unpainted (use the part's
    filament), state n>=1 = filament n.
    """
    # Nibbles are stored with the last hex digit first.
    nibbles = [_HEX_VAL[ch] for ch in reversed(code)]
    pos = 0
    n_nib = len(nibbles)

    def next_nibble() -> int:
        nonlocal pos
        if pos >= n_nib:
            raise ValueError("paint code truncated")
        v = nibbles[pos]
        pos += 1
        return v

    leaves: List[Tuple[Tuple[Vec, Vec, Vec], int]] = []
    stack: List[List] = []  # each entry: [children, processed_count]

    while True:
        c = next_nibble()
        split_sides = c & 0b11
        state = 0
        special = c >> 2
        if split_sides == 0:
            if (c & 0b1100) == 0b1100:
                nxt = next_nibble()
                num = 0
                while nxt == 0b1111:
                    num += 1
                    nxt = next_nibble()
                state = nxt + 15 * num + 3
            else:
                state = c >> 2

        if not stack:
            if split_sides:
                stack.append([_split_children(tri, split_sides, special), 0])
                continue
            leaves.append((tri, state))
            break

        last = stack[-1]
        children = last[0]
        child_idx = len(children) - last[1] - 1   # children are serialized in reverse order
        child = children[child_idx]
        if split_sides:
            stack.append([_split_children(child, split_sides, special), 0])
        else:
            leaves.append((child, state))
            last[1] += 1

        while stack and stack[-1][1] == len(stack[-1][0]):
            stack.pop()
            if stack:
                stack[-1][1] += 1
        if not stack:
            break
    return leaves


def decode_leaf_state(code: str) -> Optional[int]:
    """Fast path: if the code describes an unsplit triangle, return its state, else None."""
    c = _HEX_VAL[code[-1]]
    if c & 0b11:
        return None
    if (c & 0b1100) != 0b1100:
        return c >> 2
    # extended state, no splits possible after a leaf root
    return decode_paint(code, ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)))[0][1]


# --------------------------------------------------------------------------- #
# Mesh -> per-filament index buffers
# --------------------------------------------------------------------------- #

@dataclass
class ColoredMesh:
    positions: np.ndarray                     # (N,3) float32
    indices_by_filament: Dict[int, np.ndarray]  # filament number (1-based) -> (M,) uint32
    name: str = ""


def build_colored_mesh(obj: ModelObject, default_filament: int, name: str) -> ColoredMesh:
    verts = obj.vertices
    positions: List[Vec] = list(verts)
    extra_vertex_index: Dict[Vec, int] = {}
    idx_by_fil: Dict[int, List[int]] = {}
    leaf_cache: Dict[str, Optional[int]] = {}

    def vertex_index(p: Vec) -> int:
        i = extra_vertex_index.get(p)
        if i is None:
            i = len(positions)
            positions.append(p)
            extra_vertex_index[p] = i
        return i

    for (v1, v2, v3), code in zip(obj.triangles, obj.paint):
        if not code:
            idx_by_fil.setdefault(default_filament, []).extend((v1, v2, v3))
            continue
        st = leaf_cache.get(code, -1)
        if st == -1:
            st = decode_leaf_state(code)
            leaf_cache[code] = st
        if st is not None:
            fil = st if st > 0 else default_filament
            idx_by_fil.setdefault(fil, []).extend((v1, v2, v3))
            continue
        # Genuinely subdivided triangle.
        tri = (verts[v1], verts[v2], verts[v3])
        for (a, b, c), state in decode_paint(code, tri):
            fil = state if state > 0 else default_filament
            lst = idx_by_fil.setdefault(fil, [])
            lst.append(v1 if a is tri[0] else vertex_index(a))
            lst.append(v2 if b is tri[1] else vertex_index(b))
            lst.append(v3 if c is tri[2] else vertex_index(c))

    return ColoredMesh(
        positions=np.asarray(positions, dtype=np.float32),
        indices_by_filament={k: np.asarray(v, dtype=np.uint32) for k, v in idx_by_fil.items()},
        name=name,
    )


# --------------------------------------------------------------------------- #
# GLB writing
# --------------------------------------------------------------------------- #

class GlbBuilder:
    def __init__(self) -> None:
        self.bin = bytearray()
        self.buffer_views: List[dict] = []
        self.accessors: List[dict] = []
        self.materials: List[dict] = []
        self.meshes: List[dict] = []
        self.nodes: List[dict] = []
        self._material_by_filament: Dict[int, int] = {}

    def _add_view(self, data: bytes, target: int) -> int:
        while len(self.bin) % 4:
            self.bin += b"\0"
        self.buffer_views.append({"buffer": 0, "byteOffset": len(self.bin), "byteLength": len(data), "target": target})
        self.bin += data
        return len(self.buffer_views) - 1

    def add_positions(self, pos: np.ndarray) -> int:
        pos = np.ascontiguousarray(pos, dtype=np.float32)
        view = self._add_view(pos.tobytes(), 34962)
        self.accessors.append({
            "bufferView": view, "componentType": 5126, "count": int(len(pos)), "type": "VEC3",
            "min": [float(x) for x in pos.min(axis=0)], "max": [float(x) for x in pos.max(axis=0)],
        })
        return len(self.accessors) - 1

    def add_indices(self, idx: np.ndarray, vertex_count: int) -> int:
        if vertex_count <= 65535:
            arr, ctype = np.ascontiguousarray(idx, dtype=np.uint16), 5123
        else:
            arr, ctype = np.ascontiguousarray(idx, dtype=np.uint32), 5125
        view = self._add_view(arr.tobytes(), 34963)
        self.accessors.append({"bufferView": view, "componentType": ctype, "count": int(len(arr)), "type": "SCALAR"})
        return len(self.accessors) - 1

    def material_for(self, filament: int, hex_color: str) -> int:
        if filament in self._material_by_filament:
            return self._material_by_filament[filament]
        r, g, b = (srgb_to_linear(c) for c in hex_to_rgb(hex_color))
        self.materials.append({
            "name": f"Filament {filament} {hex_color.upper()}",
            "pbrMetallicRoughness": {"baseColorFactor": [r, g, b, 1.0], "metallicFactor": 0.0, "roughnessFactor": 0.7},
            "doubleSided": False,
        })
        self._material_by_filament[filament] = len(self.materials) - 1
        return self._material_by_filament[filament]

    def add_mesh(self, cm: ColoredMesh, palette: List[str], fallback_color: str) -> int:
        pos_acc = self.add_positions(cm.positions)
        prims = []
        for fil in sorted(cm.indices_by_filament):
            idx = cm.indices_by_filament[fil]
            if len(idx) == 0:
                continue
            color = palette[fil - 1] if 1 <= fil <= len(palette) else fallback_color
            prims.append({
                "attributes": {"POSITION": pos_acc},
                "indices": self.add_indices(idx, len(cm.positions)),
                "material": self.material_for(fil, color),
                "mode": 4,
            })
        self.meshes.append({"name": cm.name, "primitives": prims})
        return len(self.meshes) - 1

    def add_node(self, name: str, mesh: Optional[int] = None, matrix: Optional[np.ndarray] = None,
                 children: Optional[List[int]] = None) -> int:
        node: dict = {"name": name}
        if mesh is not None:
            node["mesh"] = mesh
        if matrix is not None and not np.allclose(matrix, np.eye(4)):
            node["matrix"] = [float(x) for x in np.asarray(matrix, dtype=np.float64).T.flatten()]  # column-major
        if children:
            node["children"] = children
        self.nodes.append(node)
        return len(self.nodes) - 1

    def write(self, path: str, root_nodes: List[int], generator: str) -> None:
        while len(self.bin) % 4:
            self.bin += b"\0"
        gltf = {
            "asset": {"version": "2.0", "generator": generator},
            "scene": 0,
            "scenes": [{"nodes": root_nodes}],
            "nodes": self.nodes,
            "meshes": self.meshes,
            "materials": self.materials,
            "accessors": self.accessors,
            "bufferViews": self.buffer_views,
            "buffers": [{"byteLength": len(self.bin)}],
        }
        js = json.dumps(gltf, separators=(",", ":")).encode("utf-8")
        while len(js) % 4:
            js += b" "
        total = 12 + 8 + len(js) + 8 + len(self.bin)
        with open(path, "wb") as f:
            f.write(struct.pack("<4sII", b"glTF", 2, total))
            f.write(struct.pack("<II", len(js), 0x4E4F534A))  # JSON
            f.write(js)
            f.write(struct.pack("<II", len(self.bin), 0x004E4942))  # BIN
            f.write(self.bin)


# --------------------------------------------------------------------------- #
# Conversion driver
# --------------------------------------------------------------------------- #

def convert(input_path: str, output_path: str, units: str = "m", keep_position: bool = False,
            palette_override: Optional[List[str]] = None, verbose: bool = True,
            log_fn: Optional[Callable[[str], None]] = None) -> dict:
    """Convert one 3MF to a GLB. `log_fn` receives progress lines (defaults to print when verbose)."""
    t0 = time.time()
    if log_fn is not None:
        log = log_fn
    else:
        log = print if verbose else (lambda *a, **k: None)

    with zipfile.ZipFile(input_path) as zf:
        root_path = find_root_model(zf)
        palette = palette_override or read_filament_colours(zf) or DEFAULT_PALETTE
        extruders, part_names = read_extruder_overrides(zf)

        model_files: Dict[str, Dict[str, ModelObject]] = {}
        build: List[BuildItem] = []

        def load_model(path: str) -> Dict[str, ModelObject]:
            if path not in model_files:
                log(f"  parsing {path} ...")
                objs, items = parse_model_file(zf, path)
                model_files[path] = objs
                if items:
                    build.extend(items)
                # follow component references to other files
                for o in objs.values():
                    for comp in o.components:
                        if comp.path != path:
                            load_model(comp.path)
            return model_files[path]

        log(f"Reading {input_path}")
        load_model(root_path)

    log(f"  filament colours: {', '.join(palette)}")

    builder = GlbBuilder()
    mesh_cache: Dict[Tuple[str, str, int], int] = {}
    stats: Dict[int, int] = {}
    world_bounds_min = np.full(3, np.inf)
    world_bounds_max = np.full(3, -np.inf)
    item_nodes: List[int] = []
    fallback_color = "#808080"

    def mesh_node(path: str, obj: ModelObject, default_filament: int, world: np.ndarray, name: str) -> int:
        nonlocal world_bounds_min, world_bounds_max
        key = (path, obj.id, default_filament)
        if key not in mesh_cache:
            log(f"  building mesh '{name}' ({len(obj.triangles)} triangles, default filament {default_filament}) ...")
            cm = build_colored_mesh(obj, default_filament, name)
            mesh_cache[key] = builder.add_mesh(cm, palette, fallback_color)
            for fil, idx in cm.indices_by_filament.items():
                stats[fil] = stats.get(fil, 0) + len(idx) // 3
        mesh_idx = mesh_cache[key]
        # world-space bounds (for centering) from the position accessor
        pos_acc = builder.accessors[builder.meshes[mesh_idx]["primitives"][0]["attributes"]["POSITION"]]
        corners = np.array([[x, y, z, 1.0] for x in (pos_acc["min"][0], pos_acc["max"][0])
                            for y in (pos_acc["min"][1], pos_acc["max"][1])
                            for z in (pos_acc["min"][2], pos_acc["max"][2])])
        wc = (world @ corners.T).T[:, :3]
        world_bounds_min = np.minimum(world_bounds_min, wc.min(axis=0))
        world_bounds_max = np.maximum(world_bounds_max, wc.max(axis=0))
        return mesh_idx

    def emit_object(path: str, obj: ModelObject, world: np.ndarray, inherited_filament: int) -> Optional[int]:
        default_filament = extruders.get(obj.id, inherited_filament)
        name = part_names.get(obj.id) or obj.name or f"object_{obj.id}"
        if obj.has_mesh:
            return builder.add_node(name, mesh=mesh_node(path, obj, default_filament, world, name))
        if obj.components:
            children = []
            for comp in obj.components:
                target = model_files.get(comp.path, {}).get(comp.object_id)
                if target is None:
                    log(f"  warning: component {comp.object_id} in {comp.path} not found")
                    continue
                child = emit_object(comp.path, target, world @ comp.transform, default_filament)
                if child is not None:
                    builder.nodes[child]["matrix"] = [float(x) for x in comp.transform.T.flatten()]
                    children.append(child)
            return builder.add_node(name, children=children) if children else None
        return None

    if not build:
        # No build section: place every top-level mesh object once at identity.
        for path, objs in model_files.items():
            for obj in objs.values():
                build.append(BuildItem(object_id=obj.id, transform=np.eye(4)))

    root_objs = model_files[root_path]
    for item in build:
        obj = root_objs.get(item.object_id)
        if obj is None:
            log(f"  warning: build item references unknown object {item.object_id}")
            continue
        node = emit_object(root_path, obj, item.transform, 1)
        if node is not None:
            builder.nodes[node]["matrix"] = [float(x) for x in item.transform.T.flatten()]
            item_nodes.append(node)

    # Root node: 3MF is Z-up millimetres; glTF is Y-up metres.
    scale = 0.001 if units == "m" else 1.0
    z_up_to_y_up = np.array([[1, 0, 0, 0],
                             [0, 0, 1, 0],
                             [0, -1, 0, 0],
                             [0, 0, 0, 1]], dtype=np.float64)
    root = np.eye(4)
    if not keep_position and np.all(np.isfinite(world_bounds_min)):
        center = (world_bounds_min + world_bounds_max) / 2.0
        root[:3, 3] = [-center[0], -center[1], -world_bounds_min[2]]  # centre XY, rest on the ground
    root = np.diag([scale, scale, scale, 1.0]) @ z_up_to_y_up @ root
    root_node = builder.add_node("3MF Model", matrix=root, children=item_nodes)

    builder.write(output_path, [root_node], "bambu3mf_to_glb")

    size_mm = world_bounds_max - world_bounds_min
    log(f"Wrote {output_path} ({os.path.getsize(output_path) / 1e6:.1f} MB) in {time.time() - t0:.1f}s")
    log(f"  model size: {size_mm[0]:.1f} x {size_mm[1]:.1f} x {size_mm[2]:.1f} mm, output units: {units}")
    for fil in sorted(stats):
        color = palette[fil - 1] if 1 <= fil <= len(palette) else fallback_color
        log(f"  filament {fil:>2} {color}: {stats[fil]:>9} triangles")
    return {"stats": stats, "palette": palette, "output": output_path}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Convert a Bambu Studio 3MF project into a colored .glb")
    ap.add_argument("input", help="path to the .3mf project file")
    ap.add_argument("-o", "--output", help="output .glb path (default: next to the input)")
    ap.add_argument("--units", choices=("m", "mm"), default="m",
                    help="output units; glTF convention is metres (default), use mm to keep 1 unit = 1 mm")
    ap.add_argument("--keep-position", action="store_true",
                    help="keep the model where it sits on the Bambu Studio build plate instead of centring it at the origin")
    ap.add_argument("--palette", help="comma-separated #RRGGBB list overriding the project's filament colours")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args(argv)

    output = args.output or os.path.splitext(args.input)[0] + ".glb"
    palette = [c.strip() for c in args.palette.split(",")] if args.palette else None
    convert(args.input, output, units=args.units, keep_position=args.keep_position,
            palette_override=palette, verbose=not args.quiet)
    return 0


if __name__ == "__main__":
    sys.exit(main())
