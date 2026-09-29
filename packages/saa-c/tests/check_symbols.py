#!/usr/bin/env python3
"""Fails if libsaaclient defines a global symbol outside saa-c's namespaces, so it
can be linked next to other SAA libraries or a host's own cJSON.

usage: check_symbols.py LIBRARY
"""
import re
import subprocess
import sys

ALLOWED = re.compile(r"^(saa_client_|saac_|saa_cJSON_|saa_b64_)")


def main():
    nm = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else "nm"     # a cross build passes its own
    out = subprocess.run([nm, "-g", sys.argv[1]], capture_output=True, text=True, check=True).stdout
    seen, bad = 0, []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 3:                    # archive member headers, undefined symbols
            continue
        _, kind, name = parts
        if kind in "Uuvw":
            continue
        if sys.platform == "darwin" and name.startswith("_"):
            name = name[1:]
        seen += 1
        if not ALLOWED.match(name):
            bad.append(f"{kind} {name}")
    if bad:
        print("globals outside saa_client_* / saac_* / saa_cJSON_* / saa_b64_*:")
        print("\n".join("  " + b for b in sorted(set(bad))))
        return 1
    print(f"{seen} global symbols, all namespaced")
    return 0


if __name__ == "__main__":
    sys.exit(main())
