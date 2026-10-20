"""Minimal binary glTF (GLB) writer for flat-shaded meshes.

Vertex data is quantized (KHR_mesh_quantization), which three.js and Cesium
read natively: positions as 16-bit integers scaled back by the node transform,
normals as 8-bit, and 16-bit indices whenever a mesh is small enough.
"""
import json
import struct

import numpy as np

BYTE, SHORT, USHORT, UINT, FLOAT = 5120, 5122, 5123, 5125, 5126
ARRAY_BUFFER, ELEMENT_ARRAY_BUFFER = 34962, 34963
Q = 32767


def write_glb(path, meshes: list[dict], materials: list[dict]) -> int:
    """Write a GLB file. Returns its size in bytes.

    meshes: [{"name", "material": index, "arrays": {POSITION, NORMAL, indices, [_FEATURE_ID_0]}, "extras": {}}]
    materials: [{"name", "color": (r, g, b, a), "roughness"}]
    """
    blob = bytearray()
    views, accessors, gl_meshes, nodes = [], [], [], []

    def add_view(data: np.ndarray, target: int, stride: int | None = None) -> int:
        while len(blob) % 4:
            blob.append(0)
        view = {"buffer": 0, "byteOffset": len(blob), "byteLength": data.nbytes, "target": target}
        if stride:
            view["byteStride"] = stride
        views.append(view)
        blob.extend(data.tobytes())
        return len(views) - 1

    def add_accessor(view: int, ctype: int, count: int, kind: str, **extra) -> int:
        accessors.append({"bufferView": view, "componentType": ctype, "count": count, "type": kind, **extra})
        return len(accessors) - 1

    for m in meshes:
        a = m["arrays"]
        pos = a["POSITION"].astype(np.float64)
        lo, hi = pos.min(axis=0), pos.max(axis=0)
        center = (lo + hi) / 2
        scale = max(float((hi - lo).max()) / 2 / Q, 1e-6)
        qpos = np.round((pos - center) / scale).astype(np.int16)
        n = len(qpos)
        padded = np.zeros((n, 4), np.int16)  # 8-byte stride keeps attributes 4-byte aligned
        padded[:, :3] = qpos
        attrs = {"POSITION": add_accessor(add_view(padded, ARRAY_BUFFER, 8), SHORT, n, "VEC3",
                                          min=qpos.min(axis=0).tolist(), max=qpos.max(axis=0).tolist())}
        nrm = np.zeros((n, 4), np.int8)
        nrm[:, :3] = np.round(np.clip(a["NORMAL"], -1, 1) * 127).astype(np.int8)
        attrs["NORMAL"] = add_accessor(add_view(nrm, ARRAY_BUFFER, 4), BYTE, n, "VEC3", normalized=True)
        if "_FEATURE_ID_0" in a:
            fid = a["_FEATURE_ID_0"].astype(np.float32)
            attrs["_FEATURE_ID_0"] = add_accessor(add_view(fid, ARRAY_BUFFER), FLOAT, n, "SCALAR")
        idx = a["indices"]
        idx = idx.astype(np.uint16) if n <= 65535 else idx.astype(np.uint32)
        prim = {"attributes": attrs, "material": m["material"],
                "indices": add_accessor(add_view(idx, ELEMENT_ARRAY_BUFFER), USHORT if idx.dtype == np.uint16 else UINT,
                                        len(idx), "SCALAR")}
        gl_meshes.append({"name": m["name"], "primitives": [prim]})
        node = {"name": m["name"], "mesh": len(gl_meshes) - 1,
                "translation": center.tolist(), "scale": [scale, scale, scale]}
        if m.get("extras"):
            node["extras"] = m["extras"]
        nodes.append(node)

    doc = {
        "asset": {"version": "2.0", "generator": "chicago-interactive-map pipeline"},
        "extensionsUsed": ["KHR_mesh_quantization"],
        "extensionsRequired": ["KHR_mesh_quantization"],
        "scene": 0,
        "scenes": [{"nodes": list(range(len(nodes)))}],
        "nodes": nodes,
        "meshes": gl_meshes,
        "materials": [
            {"name": mat["name"],
             "pbrMetallicRoughness": {"baseColorFactor": list(mat["color"]), "metallicFactor": 0.0,
                                      "roughnessFactor": mat.get("roughness", 0.9)}}
            for mat in materials
        ],
        "accessors": accessors,
        "bufferViews": views,
        "buffers": [{"byteLength": 0}],
    }
    while len(blob) % 4:
        blob.append(0)
    doc["buffers"][0]["byteLength"] = len(blob)
    js = json.dumps(doc, separators=(",", ":")).encode()
    js += b" " * (-len(js) % 4)
    total = 12 + 8 + len(js) + 8 + len(blob)
    with open(path, "wb") as f:
        f.write(struct.pack("<III", 0x46546C67, 2, total))
        f.write(struct.pack("<II", len(js), 0x4E4F534A))
        f.write(js)
        f.write(struct.pack("<II", len(blob), 0x004E4942))
        f.write(blob)
    return total
