"""Automated gate ensuring zero CJK characters remain in Microduck Studio."""
import os
import re
import unittest

STUDIO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {"deps", "vendor", "__pycache__", ".git", "state", "logs", ".zh-backup"}
CHECK_EXTS = {".py", ".js", ".mjs", ".cjs", ".html", ".css", ".md", ".json", ".sh", ".service", ".txt"}
CJK_RE = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")


def find_cjk_violations(root=STUDIO_ROOT):
    violations = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in sorted(dirnames) if d not in SKIP_DIRS]
        for fname in sorted(filenames):
            _, ext = os.path.splitext(fname)
            if ext.lower() not in CHECK_EXTS:
                continue
            path = os.path.join(dirpath, fname)
            rel = os.path.relpath(path, root)
            try:
                text = open(path, encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                if CJK_RE.search(line):
                    violations.append(f"{rel}:{lineno}: {line.strip()[:100]}")
                    if len([v for v in violations if v.startswith(rel + ":")]) >= 3:
                        break
    return violations


class NoCjkTest(unittest.TestCase):
    def test_studio_contains_no_cjk_characters(self):
        violations = find_cjk_violations()
        self.assertEqual(
            violations,
            [],
            "Found unexpected CJK characters in software/studio:\n" + "\n".join(violations[:40]),
        )


if __name__ == "__main__":
    unittest.main()
