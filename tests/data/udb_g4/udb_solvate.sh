#!/bin/bash
# ============================================================
# UDB v1.0  PAMAM G4  -- tLeap solvation driver
#
# Reads `set box_pad` and `set ion_conc` from udb_md.conf, patches them into
# 00_solvate.tleap, and runs tLeap.
#
# Why it runs tLeap twice when ion_conc > 0:
#   The number of Na+/Cl- pairs that gives a requested molarity depends on how
#   much water there is, and that is only known after tLeap has filled the box.
#   Pass 1 solvates and neutralises with no bulk salt; the waters it placed are
#   counted from the solvated PDB; pass 2 re-solvates with
#       N = round(ion_conc * n_water / 55.56)
#   pairs.  55.56 mol/L is the molarity of pure water, so N/n_water is the mole
#   ratio that reproduces the requested concentration in the solvent phase.
#   Sizing the salt from a pre-tLeap box volume instead (what UDB writes into
#   00_solvate.tleap as a starting estimate) ignores the volume the dendrimer
#   itself occupies and gets steadily worse with generation number.
#
# Skips everything when dendrimer_pamam_gen4_solvated.prmtop and .inpcrd already exist.
# To re-solvate at a different ionic strength:
#     edit `set ion_conc` in udb_md.conf
#     rm dendrimer_pamam_gen4_solvated.*
#     bash udb_solvate.sh
# ============================================================
set -u

PRE="dendrimer_pamam_gen4"
CFG="udb_md.conf"
TLEAP_IN="00_solvate.tleap"

RED='\033[0;31m'; GRN='\033[0;32m'; CYN='\033[0;36m'; YLW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "${GRN}  [OK]${NC} $1"; }
err()  { echo -e "${RED}  [ERROR]${NC} $1"; }
info() { echo -e "${CYN}  $1${NC}"; }
warn() { echo -e "${YLW}  [WARN]${NC} $1"; }

mkdir -p logs

cfg() { awk -v k="$1" '$1=="set" && $2==k { v=$3; sub(/;.*/,"",v); print v; exit }' "$CFG"; }

#  PSF, from the solvated prmtop 
# Runs on every invocation, including the "already solvated" short-circuit, so
# flipping `set make_psf 1` and re-running produces the PSF without re-solvating.
#
# The work itself lives in 06_make_psf.py rather than a heredoc here, so it can
# also be re-run on its own (`python3 06_make_psf.py`) and so it can write the
# CHARMM parameter file alongside the PSF -- the PSF keeps GAFF atom types
# (ca, n3, hn, ow, hw), which nothing downstream can interpret without them.
make_psf() {
    [ "$(cfg make_psf)" = "1" ] || return 0
    if [ ! -f 06_make_psf.py ]; then
        warn "make_psf is 1 but 06_make_psf.py is missing - regenerate from UDB"
        return 0
    fi
    if [ -f "${PRE}_solvated.psf" ] && [ "${PRE}_solvated.psf" -nt "${PRE}_solvated.prmtop" ]; then
        ok "PSF up to date: ${PRE}_solvated.psf"
        return 0
    fi
    info "Writing the solvated PSF (dendrimer + water + ions) with ParmEd..."
    python3 06_make_psf.py 2>&1 | tee -a logs/00_solvate.log
    if [ ! -f "${PRE}_solvated.psf" ]; then
        # Single quotes, not backticks: bash runs a backticked word inside a
        # double-quoted string, so "`amber on`" would try to execute `amber`
        # and leave the message with a hole in it.
        warn "PSF not written - see the ParmEd message above.  The MD itself does"
        warn "  not need it; every .conf runs from ${PRE}_solvated.prmtop with"
        warn "  'amber on'.  Set 'make_psf 0' in udb_md.conf to stop trying."
        return 0
    fi
    ok "PSF written"
    return 0
}

if [ -f "${PRE}_solvated.prmtop" ] && [ -f "${PRE}_solvated.inpcrd" ]; then
    ok "Solvated files found - skipping tLeap"
    make_psf
    exit 0
fi
if [ ! -f "$TLEAP_IN" ]; then
    err "$TLEAP_IN not found - regenerate this package from UDB"
    exit 1
fi
if ! command -v tleap >/dev/null 2>&1; then
    err "tleap not found in PATH - source your AmberTools environment"
    exit 1
fi

CONC="$(cfg ion_conc)";  CONC="${CONC:-0}"
PAD="$(cfg box_pad)";    PAD="${PAD:-15.0}"

#  Box padding from udb_md.conf 
sed -i "s|^solvatebox mol TIP3PBOX .*# UDB_BOXPAD|solvatebox mol TIP3PBOX $PAD   # UDB_BOXPAD|" "$TLEAP_IN"
info "Solvating dendrimer_pamam_gen4:  padding $PAD A,  ion_conc $CONC M"

#  Pass 1: neutralise only 
set_salt() {
    # $1 = number of NaCl pairs.  0 comments the line out; tLeap treats a literal
    # `addionsrand mol Na+ 0 Cl- 0` as "neutralise", which would double-neutralise
    # after the two lines above have already done it.
    if [ "$1" -gt 0 ]; then
        sed -i "s|^#\? *addionsrand mol Na+ .* Cl- .*# UDB_NSALT|addionsrand mol Na+ $1 Cl- $1   # UDB_NSALT|" "$TLEAP_IN"
    else
        sed -i "s|^#\? *addionsrand mol Na+ .* Cl- .*# UDB_NSALT|# addionsrand mol Na+ 0 Cl- 0   # UDB_NSALT|" "$TLEAP_IN"
    fi
}

run_tleap() {
    rm -f "${PRE}_solvated.prmtop" "${PRE}_solvated.inpcrd" "${PRE}_solvated.pdb"
    tleap -f "$TLEAP_IN" > "logs/00_solvate.log" 2>&1
    if [ ! -f "${PRE}_solvated.prmtop" ] || [ ! -f "${PRE}_solvated.pdb" ]; then
        err "tLeap failed - see logs/00_solvate.log"
        tail -30 "logs/00_solvate.log"
        return 1
    fi
    return 0
}

count_waters() {
    # One line per water oxygen in the solvated PDB, matched on the fixed PDB
    # columns: atom name O in 13-16, residue name in 18-20.  grep exits 1 on
    # zero matches, which `set -u` would otherwise turn into an empty NWAT.
    local n
    n="$(grep -c '^ATOM.\{9\}O.\{3\}\(WAT\|HOH\|SOL\|TP3\)' "${PRE}_solvated.pdb" 2>/dev/null)" || n=0
    if [ "${n:-0}" -le 0 ]; then
        # Whitespace-split fallback, in case the PDB is not column-exact.
        n="$(awk '$1=="ATOM" && $3=="O" && ($4=="WAT"||$4=="HOH"||$4=="SOL"||$4=="TP3") {c++} END{print c+0}' "${PRE}_solvated.pdb")"
    fi
    echo "${n:-0}"
}

set_salt 0
info "  pass 1/2: solvate + neutralise..."
run_tleap || exit 1

NWAT="$(count_waters)"
ok "  waters placed: $NWAT"
if [ "${NWAT:-0}" -le 0 ]; then
    warn "could not count waters in ${PRE}_solvated.pdb - leaving the system"
    warn "neutralised with no bulk salt.  Check logs/00_solvate.log."
    make_psf
    exit 0
fi

NSALT="$(awk -v c="$CONC" -v n="$NWAT" 'BEGIN{printf "%d", (c*n/55.56)+0.5}')"
if [ "${NSALT:-0}" -le 0 ]; then
    ok "Solvation complete (neutralised, no bulk salt)"
    make_psf
    exit 0
fi

#  Pass 2: same box, with the salt the water count calls for 
ACTUAL="$(awk -v s="$NSALT" -v n="$NWAT" 'BEGIN{printf "%.4f", s*55.56/n}')"
info "  pass 2/2: adding $NSALT Na+/Cl- pairs  ->  $ACTUAL M over $NWAT waters"
set_salt "$NSALT"
run_tleap || exit 1
ok "Solvation complete ($NSALT NaCl pairs, $ACTUAL M)"
make_psf
