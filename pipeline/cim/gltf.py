"""Minimal binary glTF (GLB) writer for flat-shaded meshes."""
import json
import struct

import numpy as np

FLOAT, UINT32 = 5126, 5125
ARRAY_BUFFER, ELEMENT_ARRAY_BUFFER = 34962, 34963


def write_glb(path, meshes: list[dict], materials: list[dict]) -> int:
    """Write a GLB file. Returns its size in bytes.

    meshes: [{"name", "material": index, "arrays": {POSITION, NORMAL, indices, ...}, "extras": {}}]
    materials: [{"name", "color": (r, g, b, a), "roughness"}]
    """
    blob = bytearray()
    views, accessors, gl_meshes, nodes = [], [], [], []

    def add_view(data: np.ndarray, target: int) -> int:
        while len(blob) % 4:
            blob.append(0)
        views.append({"buffer": 0, "byteOffset": len(blob), "byteLength": data.nbytes, "target": target})
        blob.extend(data.tobytes())
        return len(views) - 1

    def add_accessor(data: np.ndarray, ctype: int, kind: str, target: int, minmax=False) -> int:
        acc = {"bufferView": add_view(data, target), "componentType": ctype,
               "count": int(data.shape[0]), "type": kind}
        if minmax:
            acc["min"] = data.min(axis=0).tolist()
            acc["max"] = data.max(axis=0).tolist()
        accessors.append(acc)
        return len(accessors) - 1

    for m in meshes:
        a = m["arrays"]
        attrs = {
            "POSITION": add_accessor(a["POSITION"], FLOAT, "VEC3", ARRAY_BUFFER, minmax=True),
            "NORMAL": add_accessor(a["NORMAL"], FLOAT, "VEC3", ARRAY_BUFFER),
        }
        if "_FEATURE_ID_0" in a:
            attrs["_FEATURE_ID_0"] = add_accessor(a["_FEATURE_ID_0"], FLOAT, "SCALAR", ARRAY_BUFFER)
        prim = {"attributes": attrs, "material": m["material"],
                "indices": add_accessor(a["indices"], UINT32, "SCALAR", ELEMENT_ARRAY_BUFFER)}
        gl_meshes.append({"name": m["name"], "primitives": [prim]})
        node = {"name": m["name"], "mesh": len(gl_meshes) - 1}
        if m.get("extras"):
            node["extras"] = m["extras"]
        nodes.append(node)

    doc = {
        "asset": {"version": "2.0", "generator": "chicago-interactive-map pipeline"},
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
        "buffers": [{"byteLength": len(blob)}],
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
