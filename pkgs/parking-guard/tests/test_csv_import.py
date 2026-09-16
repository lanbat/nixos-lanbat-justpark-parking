from pathlib import Path

from parking_guard.justpark.csv_import import import_csv


def test_import_csv(tmp_path):
    csv_path = tmp_path / "bookings.csv"
    csv_path.write_text(
        "plate,start,end,booking_ref\n"
        "AB12 CDE,1700000000,1700003600,ref-1\n",
        encoding="utf-8",
    )
    bookings = import_csv(csv_path)
    assert len(bookings) == 1
    assert bookings[0].plate == "AB12 CDE"
    assert bookings[0].booking_ref == "ref-1"
