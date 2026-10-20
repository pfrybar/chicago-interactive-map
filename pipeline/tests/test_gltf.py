import json
import struct

import numpy as np
from shapely.geometry import box

from cim.gltf import write_glb
from cim.mesh import MeshBuilder, add_extrusion


def test_glb_layout(tmp_path):
    mb = MeshBuilder()
    add_extrusion(mb, box(0, 0, 10, 10), 0, 5, 0)
    arr = mb.arrays()
    path = tmp_path / "t.glb"
    size = write_glb(path, [{"name": "buildings", "material": 0, "arrays": arr, "extras": {"features": [{"id": "x"}]}}],
                     [{"name": "building", "color": (1, 1, 1, 1)}])
    data = path.read_bytes()
    assert len(data) == size and len(data) % 4 == 0
    magic, version, total = struct.unpack("<III", data[:12])
    assert magic == 0x46546C67 and version == 2 and total == size
    jlen = struct.unpack("<I", data[12:16])[0]
    doc = json.loads(data[20:20 + jlen])
    assert doc["nodes"][0]["extras"]["features"][0]["id"] == "x"
    assert "KHR_mesh_quantization" in doc["extensionsRequired"]
    pos = doc["accessors"][doc["meshes"][0]["primitives"][0]["attributes"]["POSITION"]]
    assert pos["count"] == len(arr["POSITION"]) and pos["componentType"] == 5122
    # Dequantized by the node transform, positions come back within a millimeter.
    node = doc["nodes"][0]
    view = doc["bufferViews"][pos["bufferView"]]
    blob = data[20 + jlen + 8:]
    q = np.frombuffer(blob, np.int16, pos["count"] * 4, view["byteOffset"]).reshape(-1, 4)[:, :3]
    back = q * node["scale"][0] + np.asarray(node["translation"])
    assert np.abs(back - arr["POSITION"]).max() < 1e-3
