"""A stand-in for AmberTools' tleap, for tests.

Reads the script given with -f and writes the files its `saveamberparm` and
`savepdb` lines ask for: a topology with the atom count, an inpcrd with
coordinates and a box line, and a column-exact PDB with water oxygens.
"""

import re
import sys

N_SOLUTE = 300
N_WATER = 500


def main() -> int:
    with open(sys.argv[sys.argv.index("-f") + 1]) as fh:
        script = fh.read()
    salt = 0
    m = re.search(r"^addionsrand mol Na\+ (\d+) Cl- (\d+)", script, re.MULTILINE)
    if m:
        salt = int(m.group(1))
    natoms = N_SOLUTE + 3 * N_WATER + 2 * salt
    parm = re.search(r"^saveamberparm\s+\S+\s+(\S+)\s+(\S+)", script, re.MULTILINE)
    pdb = re.search(r"^savepdb\s+\S+\s+(\S+)", script, re.MULTILINE)
    with open(parm.group(1), "w") as fh:
        fh.write("%VERSION  VERSION_STAMP = V0001.000\n%FLAG POINTERS\n%FORMAT(10I8)\n")
        fh.write(f"{natoms:8d}{0:8d}\n")
    with open(parm.group(2), "w") as fh:
        fh.write("default_name\n")
        fh.write(f"{natoms:6d}\n")
        coords = [f"{(k % 37) * 1.1 + 2:12.7f}" for k in range(3 * natoms)]
        for k in range(0, len(coords), 6):
            fh.write("".join(coords[k : k + 6]) + "\n")
        fh.write("  40.0000000  40.0000000  40.0000000  90.0000000  90.0000000  90.0000000\n")
    with open(pdb.group(1), "w") as fh:
        serial = 1
        for _ in range(N_SOLUTE):
            fh.write(f"ATOM  {serial:5d}  C1  DEN     1    {0:8.3f}{0:8.3f}{0:8.3f}  1.00  0.00\n")
            serial += 1
        for w in range(N_WATER):
            for name in (" O  ", " H1 ", " H2 "):
                fh.write(
                    f"ATOM  {serial:5d} {name} WAT {w + 2:5d}    {0:8.3f}{0:8.3f}{0:8.3f}"
                    "  1.00  0.00\n"
                )
                serial += 1
        for _ in range(2 * salt):
            fh.write(
                f"ATOM  {serial:5d} Na+  Na+ {9000:5d}    {0:8.3f}{0:8.3f}{0:8.3f}  1.00  0.00\n"
            )
            serial += 1
        fh.write("END\n")
    print(f"Fake tleap: {natoms} atoms written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
