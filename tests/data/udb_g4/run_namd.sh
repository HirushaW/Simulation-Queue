#!/bin/bash
# UDB v1.0  PAMAM G4  Complete NAMD Pipeline
# Usage:          bash run_namd.sh
# Restart prod:   bash run_namd.sh restart
# Custom cores:   bash run_namd.sh 32
# Required in this folder:
#   dendrimer_pamam_gen4_min_gaff.mol2 (from UDB AMBER pipeline -- minimised geometry)
#   dendrimer_pamam_gen4.frcmod        (from UDB AMBER pipeline)
# Falls back to the unminimised dendrimer_pamam_gen4_typed.mol2 if _min_gaff.mol2 is absent.
# All other files are generated automatically.

NAMD_BIN="/home/hirusha/.local/bin/namd3"
NCORES="${1:-48}"
NAMD="$NAMD_BIN +p$NCORES"
CFG="udb_md.conf"
mkdir -p logs

#  Colour output helpers 
RED='[0;31m'; GRN='[0;32m'; YLW='[1;33m'
CYN='[0;36m'; WHT='[1;37m'; NC='[0m'
ok()   { echo -e "${GRN}  [OK]${NC} $1"; }
err()  { echo -e "${RED}  [ERROR]${NC} $1"; }
info() { echo -e "${CYN}  $1${NC}"; }
warn() { echo -e "${YLW}  [WARN]${NC} $1"; }

#  Settings live in udb_md.conf 
# Every .conf file in this folder starts with `source udb_md.conf`, so the
# pipeline cannot run without it.  This script reads the same file for the two
# things bash has to decide for itself: the GPU launch line and the banner.
if [ ! -f "$CFG" ]; then
    err "$CFG not found -- every .conf in this folder sources it"
    err "Regenerate this package from UDB, or copy udb_md.conf from another"
    err "pipeline of the same dendrimer type and edit the prefix."
    exit 1
fi
# Read one `set <key> <value>` line, stripping any trailing ;# comment.
cfg() { awk -v k="$1" '$1=="set" && $2==k { v=$3; sub(/;.*/,"",v); print v; exit }' "$CFG"; }

# GPU-resident dynamics: NAMD 3 wants ~1 PE per GPU; extra cores slow it down.
# Minimisation (01) always keeps the multi-core CPU launch ($NAMD).
# This is read at RUN time, so flipping `set gpu` in udb_md.conf is enough --
# no regeneration, and no way for a regeneration to silently drop GPU support.
UDB_GPU="$(cfg gpu)"
if [ "${UDB_GPU:-0}" = "1" ]; then
    NAMD_DYN="$NAMD_BIN +p1 +setcpuaffinity +devices 0"
    GPU_MODE="GPU-resident (+p1 +devices 0)"
else
    NAMD_DYN="$NAMD"
    GPU_MODE="CPU (+p$NCORES)"
fi

#  Restart mode 
RESTART_MODE=0
if [ "$1" = "restart" ]; then
    RESTART_MODE=1
    NCORES="${2:-48}"
    NAMD="$NAMD_BIN +p$NCORES"
    if [ "${UDB_GPU:-0}" = "1" ]; then
        NAMD_DYN="$NAMD_BIN +p1 +setcpuaffinity +devices 0"
    else
        NAMD_DYN="$NAMD"
    fi
fi

echo ""
echo -e "${WHT}======================================================${NC}"
echo -e "${WHT}  UDB v1.0  PAMAM G4  NAMD Pipeline${NC}"
echo -e "${WHT}  NAMD:    $NAMD_BIN${NC}"
echo -e "${WHT}  Cores:   $NCORES  |  Dynamics: $GPU_MODE${NC}"
echo -e "${WHT}  Settings: $CFG   T=$(cfg temperature) K${NC}"
echo -e "${WHT}  Start:   $(date)${NC}"
echo -e "${WHT}======================================================${NC}"

#  Validate binary 
if [ -f "$NAMD_BIN" ] && [ -x "$NAMD_BIN" ]; then
    :
elif command -v "$NAMD_BIN" >/dev/null 2>&1; then
    NAMD_BIN="$(command -v "$NAMD_BIN")"
    NAMD="$NAMD_BIN +p$NCORES"
else
    err "NAMD binary not found or not executable: $NAMD_BIN"
    err "Edit NAMD_BIN= at the top of this script or add it to PATH."
    exit 1
fi

#  Validate input files 
# Solvate from the AMBER-minimised geometry. dendrimer_pamam_gen4_typed.mol2 is the raw
# assembly -- its bond lengths are far from equilibrium and tLeap will flood the
# log with bond/close-contact warnings -- so it is a fallback only.
MD_MOL2="dendrimer_pamam_gen4_min_gaff.mol2"
if [ ! -f "$MD_MOL2" ]; then
    if [ -f "dendrimer_pamam_gen4_typed.mol2" ]; then
            MD_MOL2="dendrimer_pamam_gen4_typed.mol2"
            warn "dendrimer_pamam_gen4_min_gaff.mol2 not found -- falling back to $MD_MOL2"
            warn "This is the UNMINIMISED assembly; expect tLeap warnings and a"
            warn "possible minimisation blow-up. Run the UDB AMBER pipeline's"
            warn "minimisation step to produce dendrimer_pamam_gen4_min_gaff.mol2."
            sed -i "s|loadmol2 dendrimer_pamam_gen4_min_gaff.mol2|loadmol2 dendrimer_pamam_gen4_typed.mol2|" 00_solvate.tleap
    else
            err "Required file missing: dendrimer_pamam_gen4_min_gaff.mol2 (or dendrimer_pamam_gen4_typed.mol2)"
            err "Run UDB AMBER Fragment BCC pipeline first."
            exit 1
    fi
fi
if [ ! -f "dendrimer_pamam_gen4.frcmod" ]; then
    err "Required file missing: dendrimer_pamam_gen4.frcmod"
    err "Run UDB AMBER Fragment BCC pipeline first."
    exit 1
fi
ok "Input files verified (solute: $MD_MOL2)"

#  Handle restart mode 
if [ $RESTART_MODE -eq 1 ]; then
    if [ ! -f "05_production.restart.coor" ]; then
            err "No production restart file found (05_production.restart.coor)"
            err "Cannot restart -- no previous production run detected."
            exit 1
    fi
    info "RESTART MODE: resuming production from last checkpoint..."
    # Read last timestep from xsc file
    LAST_TS=$(tail -1 05_production.restart.xsc | awk '{print $1}')
    # Matches the whole firsttimestep line, not just the FIRSTTIMESTEP
    # placeholder -- the placeholder is consumed by the first restart, so a
    # pattern anchored on it silently no-ops on every restart after that and
    # the run resumes from the wrong step number.
    sed -i "s/^firsttimestep .*/firsttimestep       $LAST_TS/"             05_production_restart.conf
    info "Resuming from timestep $LAST_TS"
    $NAMD_DYN 05_production_restart.conf >> logs/05_production.log
    [ $? -ne 0 ] && { err "Restart failed -- check logs/05_production.log"; exit 1; }
    ok "Production restart complete"
    echo ""
    echo -e "${WHT}======================================================"
    echo -e "  Restart complete!  $(date)${NC}"
    exit 0
fi

# STEP 0 -- tLeap solvation
# Delegated to udb_solvate.sh so the staged pipeline, run_full.sh and the UDB
# GUI all solvate identically.  That script reads `set box_pad` / `set ion_conc`
# from udb_md.conf, neutralises through tLeap itself (which picks the right
# counter-ion sign -- the old inline block always added Na+, wrong for a
# positively charged dendrimer) and sizes the salt from the real water count.
echo ""
info "[0/5] tLeap solvation (TIP3P, $(cfg box_pad) A padding, $(cfg ion_conc) M NaCl)..."
if [ ! -f udb_solvate.sh ]; then
    err "udb_solvate.sh not found -- regenerate this package from UDB"
    exit 1
fi
bash udb_solvate.sh || exit 1

#  Cell + PME: always re-derived from the actual solvated system 
# Unconditional on purpose.  This used to live inside the "tLeap just ran"
# branch, so re-running a directory that already had solvated files left the
# hardcoded cubic guess in 01_minimize.conf in place.
eval "$(python3 - <<'PYEND'
import math

pre = "dendrimer_pamam_gen4"

# cellOrigin must be the centre of the coordinates NAMD actually loads, which is
# the inpcrd (via ambercoor) -- NOT the solvated PDB.  tLeap writes the two with
# different conventions: the inpcrd is box-cornered (0..box, centre ~ box/2)
# while savepdb re-centres on the origin.  They differ by exactly half a box, so
# reading the PDB here puts the cell half a box away from the system.
with open(f"{pre}_solvated.inpcrd") as fh:
    raw = fh.read().splitlines()
nat = int(raw[1].split()[0])
vals = []
for line in raw[2:]:
    if len(vals) >= 3 * nat:
        break
    line = line.rstrip()
    for i in range(0, len(line), 12):
        s = line[i:i + 12].strip()
        if s:
            vals.append(float(s))
vals = vals[:3 * nat]
box_line = [l for l in raw if l.strip()][-1]
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

info "  Box:        ${BX} x ${BY} x ${BZ} A"
info "  Cell origin: ${CX}  ${CY}  ${CZ}"
info "  PME grid:   ${PX} x ${PY} x ${PZ}"

# Patched into udb_md.conf, in the AUTO-GENERATED block at the bottom.  Every
# .conf sources that file and refers to $cell_x / $pme_x, so one write here
# reaches all six stages and the all-in-one script.  Anything the user edited
# further up the file is untouched.
sed -i "s/^set cell_x .*/set cell_x              $BX/"  "$CFG"
sed -i "s/^set cell_y .*/set cell_y              $BY/"  "$CFG"
sed -i "s/^set cell_z .*/set cell_z              $BZ/"  "$CFG"
sed -i "s/^set cell_ox .*/set cell_ox             $CX/" "$CFG"
sed -i "s/^set cell_oy .*/set cell_oy             $CY/" "$CFG"
sed -i "s/^set cell_oz .*/set cell_oz             $CZ/" "$CFG"
sed -i "s/^set pme_x .*/set pme_x               $PX/"   "$CFG"
sed -i "s/^set pme_y .*/set pme_y               $PY/"   "$CFG"
sed -i "s/^set pme_z .*/set pme_z               $PZ/"   "$CFG"
if [ "$(cfg cell_x)" != "$BX" ] || [ "$(cfg pme_x)" != "$PX" ]; then
    err "Failed to write the cell into $CFG -- check its AUTO-GENERATED block"
    err "still contains 'set cell_x' / 'set pme_x' lines at column 1."
    exit 1
fi
ok "cellBasisVector + cellOrigin + PME grid written to $CFG from actual inpcrd"

#  Banner helpers -- report what udb_md.conf actually says right now 
# These read the live file rather than a number frozen in at generation time,
# so an edited stage length is reflected in what the pipeline prints.
udb_ps() { awk -v s="$(cfg "$1")" -v t="$(cfg timestep_fs)" 'BEGIN{printf "%.0f", s*t/1000}'; }
udb_ns() { awk -v s="$(cfg "$1")" -v t="$(cfg timestep_fs)" 'BEGIN{printf "%.3g", s*t/1e6}'; }

# A stage is skipped when its restart files exist.  If udb_md.conf was edited
# after those files were written, the edit will NOT be applied to that stage --
# say so instead of letting it pass silently.
stale_check() {
    local rst="$1"; local name="$2"
    [ -f "$rst" ] || return 0
    if [ "$CFG" -nt "$rst" ]; then
        warn "$CFG was edited after $name ran -- the new settings do NOT apply"
        warn "  to it. To redo this stage: rm ${rst%%.coor}.*"
    fi
}

# A stage can exit 0 and still leave no .restart.* files behind: NAMD only
# writes them when the step counter hits a multiple of restartfreq.  The next
# stage then dies with "Unable to open extended system file", which points at
# the wrong stage entirely.  Catch it here, where the cause is obvious.
require_restart() {
    local tag="$1"; local name="$2"; local missing=""
    for ext in coor vel xsc; do
        [ -f "${tag}.restart.${ext}" ] || missing="$missing ${tag}.restart.${ext}"
    done
    [ -z "$missing" ] && return 0
    err "$name finished but wrote no restart files:$missing"
    err "restartfreq resolved longer than the stage. In $CFG, lower"
    err "  set restart_freq   (currently $(cfg restart_freq))"
    err "or raise that stage's step count. The next stage cannot start."
    exit 1
}


# STEP 1 -- Energy minimisation
echo ""
info "[1/5] Energy minimisation ($(cfg min_steps) steps)..."
if [ ! -f "01_minimize.restart.coor" ] || [ ! -f "01_minimize.restart.vel" ] || [ ! -f "01_minimize.restart.xsc" ]; then
    $NAMD 01_minimize.conf > logs/01_minimize.log 2>&1
    if [ $? -ne 0 ]; then
            err "Minimisation failed"
            tail -30 logs/01_minimize.log
            exit 1
    fi
    ok "Minimisation complete"
    require_restart 01_minimize "minimisation"
else
    ok "Skipping -- restart file found"
    stale_check 01_minimize.restart.coor "minimisation"
fi

# STEP 2 -- NVT heating 0 -> target
echo ""
info "[2/5] NVT heating  0 -> $(cfg temperature) K  ($(udb_ps heat_steps) ps)..."
if [ ! -f "02_heat.restart.coor" ] || [ ! -f "02_heat.restart.vel" ] || [ ! -f "02_heat.restart.xsc" ]; then
    $NAMD_DYN 02_heat.conf > logs/02_heat.log 2>&1
    if [ $? -ne 0 ]; then
            err "Heating failed"
            tail -30 logs/02_heat.log
            exit 1
    fi
    ok "Heating complete"
    require_restart 02_heat "heating"
else
    ok "Skipping -- restart file found"
    stale_check 02_heat.restart.coor "heating"
fi

# STEP 3 -- NVT equilibration
echo ""
info "[3/5] NVT equilibration at $(cfg temperature) K  ($(udb_ps nvt_steps) ps)..."
if [ ! -f "03_nvt_equil.restart.coor" ] || [ ! -f "03_nvt_equil.restart.vel" ] || [ ! -f "03_nvt_equil.restart.xsc" ]; then
    $NAMD_DYN 03_nvt_equil.conf > logs/03_nvt_equil.log 2>&1
    if [ $? -ne 0 ]; then
            err "NVT equilibration failed"
            tail -30 logs/03_nvt_equil.log
            exit 1
    fi
    ok "NVT equilibration complete"
    require_restart 03_nvt_equil "NVT equilibration"
else
    ok "Skipping -- restart file found"
    stale_check 03_nvt_equil.restart.coor "NVT equilibration"
fi


#  Stage-4 restraint reference 
# Built from 03_nvt_equil.restart.coor -- the coordinates stage 4 actually
# restarts from -- NOT from <pre>_solvated.pdb.  The solvated PDB is the t = 0
# structure; by the end of NVT the solute has moved and been PBC-wrapped, so
# restraining to it put atoms ~50 A from their reference and blew up RATTLE
# on the first step (BOUNDARY = 3.4e5 kcal/mol, pressure -46000 atm).
if ! grep -q "^consref" 04_npt_equil.conf 2>/dev/null; then
info "  Stage 4 is unrestrained -- no restraint reference needed"
else
python3 - <<'PYEND'
import os
import struct

pre = "dendrimer_pamam_gen4"
src = "03_nvt_equil.restart.coor"

pdb = open(f"{pre}_solvated.pdb").readlines()
idx = [i for i, l in enumerate(pdb) if l[:4] in ("ATOM", "HETA")]

xyz = None
if os.path.exists(src):
    with open(src, "rb") as fh:
        n = struct.unpack("i", fh.read(4))[0]
        flat = struct.unpack(f"{3 * n}d", fh.read(24 * n))
    if n != len(idx):
        raise SystemExit(
            f"  [ERROR] atom count mismatch: {pre}_solvated.pdb has {len(idx)}, "
            f"{src} has {n} -- refusing to write a bogus restraint reference"
        )
    xyz = [flat[3 * i:3 * i + 3] for i in range(n)]
else:
    print(f"  [WARN] {src} not found -- falling back to t=0 solvated coordinates")

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

    coord = l[30:54]
    if xyz is not None:
        coord = "%8.3f%8.3f%8.3f" % xyz[k]
    b = "  1.00" if restrain else "  0.00"
    pdb[i] = l[:30] + coord + l[54:60] + b + l[66:]

    if restrain:
        kept[resn] = kept.get(resn, 0) + 1

with open(f"{pre}_restrained.pdb", "w") as fh:
    fh.writelines(pdb)

total = sum(kept.values())
detail = ", ".join(f"{r}:{c}" for r, c in sorted(kept.items()))
print(f"  Restraint PDB: {pre}_restrained.pdb  ({total} restrained heavy atoms -- {detail})")
if total == 0:
    raise SystemExit("  [ERROR] no atoms flagged for restraint -- check residue naming")
PYEND
if [ $? -ne 0 ]; then
    err "Restraint PDB generation failed"
    exit 1
fi
ok "Restraint PDB built from 03_nvt_equil.restart.coor"
fi

# STEP 4 -- NPT equilibration 10 ns
echo ""
if grep -q "^consref" 04_npt_equil.conf 2>/dev/null && [ "$(cfg use_restraints)" = "1" ]; then
    info "[4/5] NPT equilibration  $(cfg temperature) K / $(cfg pressure) bar  ($(udb_ns npt_restrained) ns restrained + $(udb_ns npt_free) ns free)..."
    warn "      04_npt_equil.dcd will contain restrained AND free frames in one file"
else
    info "[4/5] NPT equilibration  $(cfg temperature) K / $(cfg pressure) bar  ($(udb_ns npt_free) ns, unrestrained)..."
fi
if [ ! -f "04_npt_equil.restart.coor" ] || [ ! -f "04_npt_equil.restart.vel" ] || [ ! -f "04_npt_equil.restart.xsc" ]; then
    $NAMD_DYN 04_npt_equil.conf > logs/04_npt_equil.log 2>&1
    if [ $? -ne 0 ]; then
            err "NPT equilibration failed"
            tail -30 logs/04_npt_equil.log
            exit 1
    fi
    ok "NPT equilibration complete"
    require_restart 04_npt_equil "NPT equilibration"
else
    ok "Skipping -- restart file found"
    stale_check 04_npt_equil.restart.coor "NPT equilibration"
fi

# STEP 5 -- NPT production
echo ""
info "[5/5] NPT production  $(cfg temperature) K  ($(udb_ns prod_steps) ns)..."
info "      Estimated trajectory: ~54.0 GB at the generated dcdfreq"
$NAMD_DYN 05_production.conf > logs/05_production.log 2>&1
if [ $? -ne 0 ]; then
    err "Production failed"
    tail -30 logs/05_production.log
    exit 1
fi
ok "Production complete"

echo ""
info "Running VMD analysis..."
if command -v vmd &>/dev/null; then
    vmd -dispdev none -e analysis.tcl > logs/analysis.log 2>&1
    ok "Analysis complete"
    info "  Output files:"
    for f in dendrimer_pamam_gen4_Rg.dat dendrimer_pamam_gen4_delta.dat dendrimer_pamam_gen4_RMSD.dat                  dendrimer_pamam_gen4_end2end.dat dendrimer_pamam_gen4_rdensity.dat; do
            [ -f "$f" ] && echo "    $f"
    done
else
    warn "VMD not found -- skipping analysis"
    warn "Run manually: vmd -e analysis.tcl"
fi

echo ""
echo -e "${WHT}======================================================"
echo    "  Pipeline complete!  $(date)"
echo    "  To restart if killed:  bash run_namd.sh restart"
echo -e "======================================================${NC}"
