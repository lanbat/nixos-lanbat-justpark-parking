from parking_guard.plates import normalize_plate, plates_match


def test_normalize_strips_spaces_and_uppercases():
    assert normalize_plate("ab12 cde") == "AB12CDE"


def test_normalize_applies_o_and_i_rules():
    assert normalize_plate("AB12CDE") == "AB12CDE"
    assert normalize_plate("AB12CDO") == "AB12CD0"


def test_plates_match_after_normalization():
    assert plates_match("AB12 CDE", "ab12cde")
    assert not plates_match("AB12CDE", "AB99CDE")
