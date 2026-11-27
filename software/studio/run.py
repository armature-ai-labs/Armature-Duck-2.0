"""Studio entrypoint: python3 -I run.py [args]

Under -I (isolated mode), Python skips cwd and user site-packages, ensuring only pinned deps/ are used.
This script prepends deps/ and the project root to sys.path before launching studio.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "deps"), HERE]

from studio.__main__ import main  # noqa: E402

if __name__ == "__main__":
    main()
