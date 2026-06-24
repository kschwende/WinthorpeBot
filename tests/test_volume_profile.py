"""Volume-at-price profile — POC + value area from OHLCV bars (pure, no network)."""

from collections import namedtuple

from winthorpe.levels.volume_profile import volume_profile

Bar = namedtuple("Bar", ["high", "low", "close", "volume"])


def test_poc_is_the_heaviest_price_typical():
    bars = [
        Bar(7440, 7438, 7439, 1000),   # typical 7439 — the heavy node
        Bar(7445, 7443, 7444, 300),
        Bar(7435, 7433, 7434, 300),
    ]
    vp = volume_profile(bars, bin_size=1.0, mode="typical")
    assert 7439 <= vp.poc <= 7440      # bin [7439,7440) → midpoint 7439.5
    assert vp.val <= vp.poc <= vp.vah  # POC inside its own value area


def test_value_area_is_contiguous_around_poc():
    bars = ([Bar(7440, 7440, 7440, 1000)]
            + [Bar(7440 + i, 7440 + i, 7440 + i, 50) for i in range(1, 6)]
            + [Bar(7440 - i, 7440 - i, 7440 - i, 50) for i in range(1, 6)])
    vp = volume_profile(bars, mode="typical")
    assert round(vp.poc) == 7440
    assert vp.val < vp.poc < vp.vah
    assert vp.total_volume == 1000 + 10 * 50


def test_uniform_spreads_one_wide_bar_across_its_range():
    vp = volume_profile([Bar(7450, 7430, 7440, 1000)], mode="uniform", bin_size=1.0)
    assert 7430 <= vp.poc <= 7450
    assert vp.vah - vp.val >= 10        # value area spans most of the wide bar


def test_empty_or_zero_volume_is_none():
    assert volume_profile([], mode="typical") is None
    assert volume_profile([Bar(7440, 7438, 7439, 0)]) is None


def test_typical_pins_the_heavy_node_not_a_wide_bar_smear():
    # Tight heavy node low; wide lighter bars high. 'typical' keeps the POC at the
    # heavy node instead of smearing it up toward the wide-bar range (the bug that
    # put yesterday's POC at the VAH on coarse bars with a uniform smear).
    bars = ([Bar(7440, 7439, 7439, 5000) for _ in range(5)]        # tight, heavy, low
            + [Bar(7470, 7450, 7460, 1500) for _ in range(6)])     # wide, high
    assert abs(volume_profile(bars, mode="typical").poc - 7439) <= 1.5
