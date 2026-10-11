import pytest

from simq.tcl import Interp, TclError, join_list, split_list


@pytest.fixture
def tcl():
    return Interp()


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("expr {1 + 2 * 3}", "7"),
        ("expr {-2**2}", "4"),
        ("expr {2**3**2}", "512"),
        ("expr {7 / 2}", "3"),
        ("expr {-7 / 2}", "-4"),
        ("expr {7 / 2.0}", "3.5"),
        ("expr {10 % 3}", "1"),
        ("expr {int(ceil(double(300.0) / 2.0))}", "150"),
        ("expr {1 < 2 && 3 > 2}", "1"),
        ('expr {5 > 3 ? "a" : "b"}', "a"),
        ('expr {"abc" eq "abc"}', "1"),
        ("expr {1e-8 < 1}", "1"),
        ("expr {max(3, 9, 4)}", "9"),
        ("expr {round(2.5)}", "3"),
        ("expr 3 + 4", "7"),
    ],
)
def test_expr(tcl, script, expected):
    assert tcl.eval(script) == expected


def test_variables_procs_and_loops(tcl):
    assert tcl.eval("set x 5; set y [expr {$x * 2}]") == "10"
    assert tcl.eval("proc f {a {b 3}} {return [expr {$a + $b}]}; f 1") == "4"
    assert tcl.eval("f 1 1") == "2"
    assert tcl.eval("set s 0; for {set i 1} {$i <= 4} {incr i} {incr s $i}; set s") == "10"
    assert tcl.eval("set t 0; foreach v {1 2 3} {incr t $v}; set t") == "6"
    assert tcl.eval("set n 0; while {$n < 5} {incr n; if {$n == 3} break}; set n") == "3"
    assert tcl.eval("if {0} {set r a} elseif {1} {set r b} else {set r c}") == "b"
    assert tcl.eval('set name world; set g "hello $name"') == "hello world"
    assert tcl.eval("set u ${name}s") == "worlds"


def test_comments_and_continuations(tcl):
    script = "set a 1 ;# trailing comment\n# a full-line comment\nset b \\\n    2\nexpr {$a + $b}"
    assert tcl.eval(script) == "3"


def test_arrays_and_info(tcl):
    interp = Interp(env={"UDB_PHASE": "2"})
    assert interp.eval("info exists env(UDB_PHASE)") == "1"
    assert interp.eval("info exists env(OTHER)") == "0"
    assert interp.eval("set p $env(UDB_PHASE)") == "2"
    assert tcl.eval("set a(x) 4; set a(x)") == "4"


def test_global_in_proc(tcl):
    assert tcl.eval("set g 1; proc bump {} {global g; incr g}; bump; set g") == "2"


def test_errors(tcl):
    with pytest.raises(TclError):
        tcl.eval("set nope")
    with pytest.raises(TclError):
        tcl.eval("expr {1 / 0}")
    with pytest.raises(TclError):
        tcl.eval("no_such_command 1")
    assert tcl.eval("catch {error boom} msg") == "1"
    assert tcl.eval("set msg") == "boom"


def test_endless_loop_is_stopped():
    tcl = Interp()
    tcl.MAX_COMMANDS = 10_000
    with pytest.raises(TclError, match="too long"):
        tcl.eval("while {1} {set x 1}")


def test_unknown_handler_receives_namd_keywords():
    seen = []
    tcl = Interp(unknown=lambda it, name, args: seen.append((name, args)) or "")
    tcl.eval("set c 12.0\ncutoff $c\nPME yes")
    assert seen == [("cutoff", ["12.0"]), ("PME", ["yes"])]


def test_lists():
    assert split_list("a {b c} d") == ["a", "b c", "d"]
    assert join_list(["a", "b c", ""]) == "a {b c} {}"
    assert Interp().eval("llength {a {b c} d}") == "3"
    assert Interp().eval("lindex {a b c} end") == "c"


def test_source_relative_to_cwd(tmp_path):
    (tmp_path / "vals.tcl").write_text("set from_file 42\n")
    tcl = Interp(cwd=tmp_path)
    assert tcl.eval("source vals.tcl; set from_file") == "42"
