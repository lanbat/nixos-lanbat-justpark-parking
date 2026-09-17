from parking_guard.geometry import (
    bottom_center,
    coverage,
    normalize_box,
    parse_polygon,
    point_in_polygon,
    polygon_area,
)

UNIT_SQUARE = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))


def test_parse_polygon_accepts_frigate_string():
    assert parse_polygon("0,0,1,0,1,1,0,1") == UNIT_SQUARE


def test_parse_polygon_accepts_pairs_and_flat_lists():
    assert parse_polygon([[0, 0], [1, 0], [1, 1], [0, 1]]) == UNIT_SQUARE
    assert parse_polygon([0, 0, 1, 0, 1, 1, 0, 1]) == UNIT_SQUARE
    assert parse_polygon(None) == ()


def test_polygon_area_of_unit_square():
    assert polygon_area(UNIT_SQUARE) == 1.0


def test_point_in_polygon():
    assert point_in_polygon((0.5, 0.5), UNIT_SQUARE)
    assert not point_in_polygon((1.5, 0.5), UNIT_SQUARE)


def test_point_in_concave_polygon():
    # An L shape: the notch in the top-right quadrant is outside.
    l_shape = ((0.0, 0.0), (1.0, 0.0), (1.0, 0.5), (0.5, 0.5), (0.5, 1.0), (0.0, 1.0))
    assert point_in_polygon((0.25, 0.75), l_shape)
    assert not point_in_polygon((0.75, 0.75), l_shape)


def test_coverage_fully_inside_and_partially_inside():
    slot = ((0.0, 0.0), (0.5, 0.0), (0.5, 1.0), (0.0, 1.0))
    assert coverage((0.1, 0.1, 0.4, 0.4), slot) == 1.0
    assert coverage((0.4, 0.0, 0.6, 1.0), slot) == 0.5
    assert coverage((0.6, 0.0, 0.8, 1.0), slot) == 0.0


def test_coverage_ignores_degenerate_rect():
    assert coverage((0.5, 0.5, 0.5, 0.5), UNIT_SQUARE) == 0.0


def test_normalize_box_from_pixels():
    assert normalize_box([960, 540, 1920, 1080], 1920, 1080) == (0.5, 0.5, 1.0, 1.0)


def test_normalize_box_passes_through_normalized_values():
    assert normalize_box([0.1, 0.2, 0.3, 0.4], 1920, 1080) == (0.1, 0.2, 0.3, 0.4)


def test_normalize_box_clamps_and_orders_corners():
    assert normalize_box([2000, -10, 100, 2000], 1920, 1080) == (
        0.052083333333333336,
        0.0,
        1.0,
        1.0,
    )


def test_bottom_center_is_ground_contact_point():
    assert bottom_center((0.2, 0.1, 0.6, 0.9)) == (0.4, 0.9)
