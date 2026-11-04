import io
import pstats
import re
import sys

b = io.StringIO()
s = pstats.Stats(sys.argv[1], stream=b)
s.sort_stats(sys.argv[2] if len(sys.argv) > 2 else "cumulative")
s.print_stats(int(sys.argv[3]) if len(sys.argv) > 3 else 50)
for line in b.getvalue().splitlines():
    line = re.sub(r"[A-Z]:\\[^ ]*?\\(site-packages|src|Lib|scratchpad)\\", r"\1/", line)
    if line.strip():
        print(line[:190])
