from zipfile import ZipFile

import pytest

from hoard_link.hub.piece_checks_layout import _web_root


def test_check_uses_only_the_public_files_when_the_zip_contains_project_source(tmp_path):
    package = tmp_path / "booking.zip"
    with ZipFile(package, "w") as archive:
        archive.writestr("server.py", "# editable source")
        archive.writestr("public/index.html", "<h1>Booking</h1>")
        archive.writestr("public/assets/booking.js", "/* page script */")
    with _web_root(package) as root:
        assert (root / "index.html").is_file()
        assert (root / "assets" / "booking.js").is_file()
        assert not (root.parent / "server.py").exists()
    assert not root.exists()


def test_check_rejects_parent_traversal_in_public_files(tmp_path):
    package = tmp_path / "booking.zip"
    with ZipFile(package, "w") as archive:
        archive.writestr("public/index.html", "<h1>Booking</h1>")
        archive.writestr("public/../outside.html", "outside")
    with pytest.raises(ValueError, match="invalid"):
        with _web_root(package):
            pytest.fail("The package was extracted")
