#!/bin/bash
# ===========================================================
#  UDB v1.0  PAMAM G4  -  all-in-one NAMD launcher
#
#  Runs dendrimer_pamam_gen4_full.namd, which does every stage in one go.
#  This is the portable alternative to the staged run_namd.sh;
#  both read the same udb_md.conf and the same tLeap output.
#
#    bash run_full.sh            fresh run
#    bash run_full.sh resume     continue from the last restart
#
#  To change temperature, stage lengths or GPU mode, edit
#  udb_md.conf -- not this script.
# ===========================================================
set -u

PRE="dendrimer_pamam_gen4"
SCRIPT="${PRE}_full.namd"
CFG="udb_md.conf"

RED=$'\033[0;31m'; GRN=$'\033[0;32m'; CYN=$'\033[0;36m'; YLW=$'\033[1;33m'; NC=$'\033[0m'
info() { echo "${CYN}  $*${NC}"; }
ok()   { echo "${GRN}  [OK]${NC} $*"; }
err()  { echo "${RED}  [ERROR]${NC} $*"; }
warn() { echo "${YLW}  [WARN]${NC} $*"; }

if [ ! -f "$CFG" ]; then
    err "$CFG not found - it holds every MD setting and both launchers need it"
    exit 1
fi

# Read one `set <key> <value>` from udb_md.conf, stripping any ;# comment.
cfg() {
    awk -v k="$1" '$1=="set" && $2==k { v=$3; sub(/;.*/,"",v); print v; exit }' "$CFG"
}

NAMD_BIN="${NAMD_BIN:-/home/hirusha/.local/bin/namd3}"
NCORES="${NCORES:-48}"
UDB_GPU="$(cfg gpu)"

# GPU-resident wants ~1 PE per GPU; extra cores make it slower, not faster.
if [ "${UDB_GPU:-0}" = "1" ]; then
    LAUNCH=("$NAMD_BIN" +p1 +setcpuaffinity +devices 0)
    MODE="GPU-resident (gpu=1 in $CFG)"
else
    LAUNCH=("$NAMD_BIN" +p"$NCORES")
    MODE="CPU, +p$NCORES (gpu=0 in $CFG)"
fi

echo ""
echo "======================================================"
echo "  UDB v1.0  PAMAM G4  -  all-in-one NAMD run"
echo "  NAMD:  $NAMD_BIN"
echo "  Mode:  $MODE"
echo "  Temp:  $(cfg temperature) K   |  Start: $(date)"
echo "======================================================"

if [ ! -x "$NAMD_BIN" ] && ! command -v "$NAMD_BIN" >/dev/null 2>&1; then
    err "NAMD not found at '$NAMD_BIN' - set NAMD_BIN=/path/to/namd3"
    exit 1
fi
if [ ! -f "$SCRIPT" ]; then
    err "$SCRIPT not found"
    exit 1
fi

mkdir -p logs

# -- tLeap solvation, if it has not been done yet ----------------------
# udb_solvate.sh is the single solvation entry point shared with run_namd.sh:
# it patches the box padding and the ion counts into 00_solvate.tleap from
# `set box_pad` / `set ion_conc` in udb_md.conf, then runs tLeap.  Editing the
# ionic strength there and deleting ${PRE}_solvated.* is all it takes to
# re-solvate at a different salt concentration.
info "[1/4] tLeap solvation (ion_conc = $(cfg ion_conc) M)..."
if [ ! -f udb_solvate.sh ]; then
    err "udb_solvate.sh not found - regenerate this package from UDB"
    exit 1
fi
bash udb_solvate.sh || exit 1

# -- Cell + PME from the real box, patched into udb_md.conf ------------
# Everything below is derived from the *inpcrd*, because that is the file
# NAMD loads via `ambercoor`.  This matters: tLeap's savepdb and
# saveamberparm do NOT write the same frame - the PDB is centred on the
# origin, the inpcrd is shifted to the box corner by exactly box/2.
# Mixing the two puts the restraint reference half a box away from the
# coordinates being restrained.
info "[2/4] Reading box and frame from ${PRE}_solvated.inpcrd..."
eval "$(python3 - <<PYEND
import math

pre = "$PRE"
lines = open(f"{pre}_solvated.inpcrd").read().splitlines()
natom = int(lines[1].split()[0])

vals = []
for line in lines[2:]:
    if len(vals) >= 3 * natom:
        break
    line = line.rstrip()
    for i in range(0, len(line), 12):
        s = line[i:i + 12].strip()
        if s:
            vals.append(float(s))
vals = vals[:3 * natom]

box_line = [l for l in lines if l.strip()][-1]
bx, by, bz = (float(v) for v in box_line.split()[:3])

lo = [min(vals[j::3]) for j in range(3)]
hi = [max(vals[j::3]) for j in range(3)]
cx, cy, cz = ((lo[j] + hi[j]) / 2.0 for j in range(3))

def pme(length):
    # smallest 2/3/5-smooth grid count giving <= ~1 A spacing
    n = math.ceil(length)
    while True:
        x = n
        for p in (2, 3, 5):
            while x % p == 0:
                x //= p
        if x == 1:
            return n
        n += 1

print(f"BX={bx:.4f}; BY={by:.4f}; BZ={bz:.4f}")
print(f"CX={cx:.4f}; CY={cy:.4f}; CZ={cz:.4f}")
print(f"PX={pme(bx)}; PY={pme(by)}; PZ={pme(bz)}")
PYEND
)"

sed -i "s/^set cell_x .*/set cell_x              $BX/"  "$CFG"
sed -i "s/^set cell_y .*/set cell_y              $BY/"  "$CFG"
sed -i "s/^set cell_z .*/set cell_z              $BZ/"  "$CFG"
sed -i "s/^set cell_ox .*/set cell_ox             $CX/" "$CFG"
sed -i "s/^set cell_oy .*/set cell_oy             $CY/" "$CFG"
sed -i "s/^set cell_oz .*/set cell_oz             $CZ/" "$CFG"
sed -i "s/^set pme_x .*/set pme_x               $PX/"   "$CFG"
sed -i "s/^set pme_y .*/set pme_y               $PY/"   "$CFG"
sed -i "s/^set pme_z .*/set pme_z               $PZ/"   "$CFG"
ok "Box ${BX} x ${BY} x ${BZ} A   origin ${CX} ${CY} ${CZ}   PME ${PX}x${PY}x${PZ}"

# -- Restraint reference -----------------------------------------------
# Atom names/residues come from the solvated PDB; the COORDINATES come
# from whatever the upcoming phase will actually load.  Phase 1 loads the
# inpcrd; phase 2 loads the phase-1 restart.  Mixing frames is exactly the
# bug that made the staged pipeline blow up in RATTLE.
build_restraint() {
    UDB_REF_SRC="$1" python3 - <<'PYEND'
import os
import struct

pre = os.environ["UDB_PRE"]
src = os.environ["UDB_REF_SRC"]

pdb = open(f"{pre}_solvated.pdb").readlines()
idx = [i for i, l in enumerate(pdb) if l[:4] in ("ATOM", "HETA")]

if src == "inpcrd":
    lines = open(f"{pre}_solvated.inpcrd").read().splitlines()
    natom = int(lines[1].split()[0])
    vals = []
    for line in lines[2:]:
        if len(vals) >= 3 * natom:
            break
        line = line.rstrip()
        for i in range(0, len(line), 12):
            s = line[i:i + 12].strip()
            if s:
                vals.append(float(s))
    vals = vals[:3 * natom]
    xyz = [tuple(vals[3 * i:3 * i + 3]) for i in range(natom)]
    label = f"{pre}_solvated.inpcrd"
else:
    with open(src, "rb") as fh:
        natom = struct.unpack("i", fh.read(4))[0]
        flat = struct.unpack(f"{3 * natom}d", fh.read(24 * natom))
    xyz = [tuple(flat[3 * i:3 * i + 3]) for i in range(natom)]
    label = src

if natom != len(idx):
    raise SystemExit(f"  [ERROR] atom count mismatch: pdb {len(idx)}, {label} {natom}")

WATER = {"WAT", "HOH", "TIP", "TP3", "SOL", "T3P"}
IONS = {"NA", "CL", "K", "MG", "CA", "ZN", "BR", "LI", "RB", "CS", "F", "I"}
kept = {}
for k, i in enumerate(idx):
    l = pdb[i]
    name = l[12:16].strip()
    resn = l[17:20].strip()
    sym = name.lstrip("0123456789")
    is_water = resn.upper() in WATER
    is_ion = (sym.rstrip("+-0123456789").upper() in IONS
              or resn.rstrip("+-0123456789").upper() in IONS)
    is_h = sym[:1].upper() == "H"
    restrain = not (is_water or is_ion or is_h)
    coord = "%8.3f%8.3f%8.3f" % xyz[k]
    pdb[i] = l[:30] + coord + l[54:60] + ("  1.00" if restrain else "  0.00") + l[66:]
    if restrain:
        kept[resn] = kept.get(resn, 0) + 1

open(f"{pre}_restrained.pdb", "w").writelines(pdb)
total = sum(kept.values())
print(f"  reference = {label}   "
      + f"{total} restrained heavy atoms - "
      + ", ".join(f"{r}:{c}" for r, c in sorted(kept.items())))
if total == 0:
    raise SystemExit("  [ERROR] nothing flagged for restraint - check residue naming")
PYEND
    return $?
}
export UDB_PRE="$PRE"

# With `set use_restraints 0` in udb_md.conf the .namd script never issues a
# `constraints` command, so the reference PDB is dead weight - skip it.
UDB_RESTR="$(cfg use_restraints)"
maybe_build_restraint() {
    if [ "${UDB_RESTR:-0}" = "1" ]; then
        build_restraint "$1" || return 1
    else
        info "  use_restraints = 0 - stage 4 runs free, no reference PDB needed"
    fi
    return 0
}

run_phase() {
    # separate `local` statements: bash declares every name in one `local`
    # before assigning, so `local a=1 b=$a` leaves b unbound under `set -u`
    local ph="$1"
    local log="logs/${PRE}_full_p${ph}.log"
    info "  running phase $ph ..."
    UDB_PHASE="$ph" "${LAUNCH[@]}" "$SCRIPT" 2>&1 | tee "$log"
    local rc=${PIPESTATUS[0]}
    if [ "$rc" -ne 0 ]; then
        err "phase $ph failed (status $rc) - see $log"
        exit "$rc"
    fi
    ok "phase $ph complete"
}

# -- Run ---------------------------------------------------------------
if [ "${1:-}" = "resume" ]; then
    # phase comes from UDB_PHASE in run_phase, so only `resume` needs editing
    sed -i "s/^set resume .*/set resume              1                   ;# phase 2 only: 1 = continue from/" "$SCRIPT"
    info "[4/4] Resuming production from ${PRE}_full_p2.restart.*"
    if [ ! -f "${PRE}_restrained.pdb" ]; then
        maybe_build_restraint "${PRE}_full_p1.coor" || exit 1
    fi
    run_phase 2
else
    sed -i "s/^set resume .*/set resume              0                   ;# phase 2 only: 1 = continue from/" "$SCRIPT"

    info "[3/4] Phase 1 - minimise, heat, NVT (barostat off)"
    maybe_build_restraint inpcrd || exit 1
    run_phase 1

    if [ ! -f "${PRE}_full_p1.coor" ]; then
        err "phase 1 produced no output coordinates"
        exit 1
    fi

    info "[4/4] Phase 2 - NPT equilibration + production (barostat on)"
    # rebuild against the phase-1 end state, not the t=0 structure
    maybe_build_restraint "${PRE}_full_p1.coor" || exit 1
    run_phase 2
fi

ok "Finished: ${PRE}_full_p2.dcd  /  ${PRE}_full_p2.restart.*"
echo "  End: $(date)"
