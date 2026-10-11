from simq import fmt


def test_size():
    assert fmt.size(None) == "–"
    assert fmt.size(512) == "512 B"
    assert fmt.size(1536) == "1.5 KB"
    assert fmt.size(16 * 1024**3) == "16.0 GB"
    assert fmt.size(3 * 1024**5) == "3.0 PB"


def test_rate_and_ghz():
    assert fmt.rate(None) == "–"
    assert fmt.rate(2 * 1024**2) == "2.0 MB/s"
    assert fmt.ghz(None) == "–"
    assert fmt.ghz(3420) == "3.42 GHz"


def test_duration():
    assert fmt.duration(None) == "–"
    assert fmt.duration(42) == "42 s"
    assert fmt.duration(125) == "2 min 05 s"
    assert fmt.duration(2 * 3600 + 5 * 60) == "2 h 05 min"
    assert fmt.duration(3 * 86400 + 4 * 3600 + 59) == "3 d 4 h"
