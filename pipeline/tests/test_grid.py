from shapely.geometry import box

from cim.grid import Key, index, leaves_covering


def test_tiles_around_the_origin_share_one_root():
    # Tiles on all four sides of State & Madison must meet in a single quadtree.
    keys = {Key(0, *index(x, y)) for x in (-10, 10) for y in (-10, 10)}
    roots = {k.ancestor(7) for k in keys}
    assert len(roots) == 1


def test_children_cover_parent():
    k = Key(3, 9, 12)
    kids = k.children()
    assert len(kids) == 4 and all(c.parent() == k for c in kids)
    assert {leaf.ancestor(3) for leaf in k.leaves()} == {k}
    x0, y0, x1, y1 = k.bounds
    assert x1 - x0 == 4000


def test_leaves_covering_includes_margin():
    # A small square in the middle of the 500 m tile that starts at the origin.
    core = leaves_covering(box(200, 200, 300, 300))
    grown = leaves_covering(box(200, 200, 300, 300), margin_tiles=1)
    assert len(core) == 1 and len(grown) == 9
    assert all(k.level == 0 for k in grown)
