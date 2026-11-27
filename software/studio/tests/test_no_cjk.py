import os
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CJK_RE = re.compile(r"[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]")

EXCLUDED_DIRS = {"__pycache__", ".git", "node_modules", "_testlogs"}
EXCLUDED_FILES = {
    ROOT / "deps" / "idna" / "uts46data.py",
    ROOT / "deps" / "idna-3.10.dist-info" / "METADATA",
}
EXCLUDED_PREFIXES = (
    ROOT / "web" / "vendor",
)


class NoCjkTest(unittest.TestCase):
    def test_zero_cjk_in_studio_repo(self):
        hits = []
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS]
            dp = Path(dirpath)
            if any(dp == p or p in dp.parents for p in EXCLUDED_PREFIXES):
                continue
            for fn in sorted(filenames):
                p = dp / fn
                if p in EXCLUDED_FILES:
                    continue
                if p.suffix in {".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".bin", ".pyc", ".glb", ".gltf"}:
                    continue
                try:
                    text = p.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
                for lineno, line in enumerate(text.splitlines(), 1):
                    m = CJK_RE.search(line)
                    if m:
                        hits.append(f"{p.relative_to(ROOT)}:{lineno}: {line.strip()[:100]}")
                        if len(hits) >= 25:
                            break
                if len(hits) >= 25:
                    break
            if len(hits) >= 25:
                break
        self.assertEqual(hits, [], "Remaining CJK characters found:\n" + "\n".join(hits))


if __name__ == "__main__":
    unittest.main()
