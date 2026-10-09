"""Real descendant processes used to verify command cleanup."""
import os
import subprocess
import sys
import time
from pathlib import Path

root = Path(sys.argv[1])
mode = sys.argv[2]
role = sys.argv[3]
(root / f"{role}.pid").write_text(str(os.getpid()))
if role == "grandchild":
    print("grandchild ready", flush=True)
    time.sleep(4)
    (root / "late-marker").write_text("orphan survived")
else:
    child = "child" if role == "parent" else "grandchild"
    subprocess.Popen(
        [sys.executable, __file__, str(root), mode, child],
        stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr,
    )
    deadline = time.monotonic() + 5
    while not (root / "grandchild.pid").exists():
        if time.monotonic() > deadline:
            raise RuntimeError("descendants did not start")
        time.sleep(0.01)
    if mode == "orphan":
        sys.exit(0)
time.sleep(30)
