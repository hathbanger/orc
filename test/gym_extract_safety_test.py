"""gym archives extract safely with or without tarfile extraction filters."""
import io
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fusion_gym as gym


def archive(*members):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for info, data in members:
            tar.addfile(info, io.BytesIO(data) if data is not None else None)
    buffer.seek(0)
    return tarfile.open(fileobj=buffer)


def file(name, data=b"x", mode=0o644):
    info = tarfile.TarInfo(name)
    info.size, info.mode = len(data), mode
    return info, data


def link(name, target, kind=tarfile.SYMTYPE):
    info = tarfile.TarInfo(name)
    info.type, info.linkname = kind, target
    return info, None


class SafeExtractTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "out"
        self.root.mkdir()

    def test_fallback_extracts_files_and_inside_links(self):
        with patch.object(gym, "tarfile", wraps=tarfile) as fake:
            del fake.data_filter
            gym.safe_extract(archive(file("pkg/a.py", b"print(1)", 0o4755), link("pkg/b.py", "a.py")), self.root)
        self.assertEqual((self.root / "pkg/a.py").read_text(), "print(1)")
        self.assertEqual((self.root / "pkg/a.py").stat().st_mode & 0o7777, 0o755)
        self.assertTrue((self.root / "pkg/b.py").is_symlink())

    def test_fallback_refuses_escapes(self):
        for member in (file("../evil.py"), file("/abs.py"), link("pkg/x", "../../etc/passwd"),
                       link("pkg/h", "/etc/passwd", tarfile.LNKTYPE)):
            with self.subTest(member=member[0].name), patch.object(gym, "tarfile", wraps=tarfile) as fake:
                del fake.data_filter
                with self.assertRaisesRegex(ValueError, "refusing"):
                    gym.safe_extract(archive(member), self.root)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_whichever_path_this_python_has_extracts(self):
        # The native data filter where tarfile has it, the fallback elsewhere
        # (macOS /usr/bin/python3 is 3.9.6).
        gym.safe_extract(archive(file("a.py", b"ok")), self.root)
        self.assertEqual((self.root / "a.py").read_text(), "ok")


if __name__ == "__main__":
    unittest.main()
