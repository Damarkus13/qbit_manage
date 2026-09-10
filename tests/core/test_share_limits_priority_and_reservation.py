"""Tests for ShareLimits priority and reservation features:
1. prioritize_by_seed_leech_ratio: Dynamic swarm demand scoring and sorting.
2. reserve_upload_spots: Guaranteed upload availability via force_start management.
3. deprioritize_on_limit_reached: Bottom priority upon meeting limits when cleanup is False.
4. deprioritize_on_unmet_minimums: Archivist guard bottom priority when min_num_seeds is not met.
"""

from __future__ import annotations

from collections import OrderedDict


def _calls_of(torrent, name):
    return [c for c in torrent.calls if c[0] == name]


def _has_call(torrent, name):
    return any(c[0] == name for c in torrent.calls)


def test_prioritize_by_seed_leech_ratio_sorts_by_demand(share_limits_factory, torrent_factory, group_config_factory):
    """Torrents with higher leecher demand (num_incomplete / num_complete) must be processed first."""
    group_cfg = group_config_factory(
        priority=1.0,
        max_ratio=3.0,
        prioritize_by_seed_leech_ratio=True,
    )
    # t_high: 50 leechers, 2 seeds (demand score 25.0)
    t_high = torrent_factory(
        name="T_High",
        hash="a" * 40,
        tags="",
        num_complete=2,
        num_incomplete=50,
        num_seeds=0,
        num_leechs=0,
        ratio=0.1,
        added_on=100,
    )
    # t_mid: 10 leechers, 5 seeds (demand score 2.0)
    t_mid = torrent_factory(
        name="T_Mid",
        hash="b" * 40,
        tags="",
        num_complete=5,
        num_incomplete=10,
        num_seeds=0,
        num_leechs=0,
        ratio=0.1,
        added_on=200,
    )
    # t_zero: 0 leechers, 10 seeds (demand score 0.0)
    t_zero = torrent_factory(
        name="T_Zero",
        hash="c" * 40,
        tags="",
        num_complete=10,
        num_incomplete=0,
        num_seeds=0,
        num_leechs=0,
        ratio=0.1,
        added_on=300,
    )

    # Input in reverse order: zero, mid, high
    input_torrents = [t_zero, t_mid, t_high]
    group_cfg["torrents"] = list(input_torrents)
    sl = share_limits_factory(
        torrents=input_torrents,
        share_limits_config=OrderedDict([("public", group_cfg)]),
    )

    sl.update_share_limits_for_group("public", group_cfg, input_torrents)

    # All three torrents were updated; check that processing order matches demand
    assert sl.torrents_updated == ["T_High", "T_Mid", "T_Zero"]


def test_prioritize_by_seed_leech_ratio_tie_breaking(share_limits_factory, torrent_factory, group_config_factory):
    """When demand scores are equal, lower ratio (greater buffer need) is prioritized."""
    group_cfg = group_config_factory(
        priority=1.0,
        max_ratio=3.0,
        prioritize_by_seed_leech_ratio=True,
    )
    # Both have demand 10/1 = 10.0
    t_lower_ratio = torrent_factory(
        name="T_LowerRatio",
        hash="1" * 40,
        tags="",
        num_complete=1,
        num_incomplete=10,
        ratio=0.5,
        added_on=100,
    )
    t_higher_ratio = torrent_factory(
        name="T_HigherRatio",
        hash="2" * 40,
        tags="",
        num_complete=1,
        num_incomplete=10,
        ratio=2.5,
        added_on=100,
    )

    input_torrents = [t_higher_ratio, t_lower_ratio]
    group_cfg["torrents"] = list(input_torrents)
    sl = share_limits_factory(
        torrents=input_torrents,
        share_limits_config=OrderedDict([("public", group_cfg)]),
    )

    sl.update_share_limits_for_group("public", group_cfg, input_torrents)

    assert sl.torrents_updated == ["T_LowerRatio", "T_HigherRatio"]


def test_reserve_upload_spots_sets_force_start_below_limit(share_limits_factory, torrent_factory, group_config_factory):
    """When below ratio limit, reserve_upload_spots sets force_start=True to guarantee active seeding."""
    group_cfg = group_config_factory(
        priority=1.0,
        max_ratio=3.0,
        reserve_upload_spots=True,
        cleanup=False,
    )
    t = torrent_factory(
        hash="a" * 40,
        tags="",
        ratio=1.0,
        force_start=False,
    )
    group_cfg["torrents"] = [t]
    sl = share_limits_factory(
        torrents=[t],
        share_limits_config=OrderedDict([("private", group_cfg)]),
    )

    sl.update_share_limits_for_group("private", group_cfg, [t])

    assert _has_call(t, "set_force_start")
    calls = _calls_of(t, "set_force_start")
    assert calls[-1][1]["enable"] is True
    assert t.force_start is True


def test_reserve_upload_spots_releases_force_start_at_limit(share_limits_factory, torrent_factory, group_config_factory):
    """When ratio limit reached, reserve_upload_spots releases force_start (sets False)."""
    group_cfg = group_config_factory(
        priority=1.0,
        max_ratio=3.0,
        reserve_upload_spots=True,
        deprioritize_on_limit_reached=True,
        cleanup=False,
    )
    t = torrent_factory(
        hash="a" * 40,
        tags="~share_limit_1.0.private",
        ratio=3.5,
        force_start=True,
    )
    group_cfg["torrents"] = [t]
    sl = share_limits_factory(
        torrents=[t],
        share_limits_config=OrderedDict([("private", group_cfg)]),
    )

    sl.update_share_limits_for_group("private", group_cfg, [t])

    assert _has_call(t, "set_force_start")
    calls = _calls_of(t, "set_force_start")
    assert calls[-1][1]["enable"] is False
    assert t.force_start is False


def test_deprioritize_on_limit_reached_moves_to_bottom_priority(
    share_limits_factory, torrent_factory, group_config_factory
):
    """When limit reached and deprioritize_on_limit_reached is True, calls bottom_priority and sets no limit."""
    group_cfg = group_config_factory(
        priority=1.0,
        max_ratio=3.0,
        deprioritize_on_limit_reached=True,
        upload_speed_on_limit_reached=100,
        cleanup=False,
    )
    t = torrent_factory(
        hash="a" * 40,
        tags="~share_limit_1.0.private",
        ratio=3.5,
        ratio_limit=3.0,
    )
    group_cfg["torrents"] = [t]
    sl = share_limits_factory(
        torrents=[t],
        share_limits_config=OrderedDict([("private", group_cfg)]),
    )

    sl.update_share_limits_for_group("private", group_cfg, [t])

    assert _has_call(t, "bottom_priority")
    assert _has_call(t, "set_share_limits")
    share_limit_call = _calls_of(t, "set_share_limits")[-1][1]
    assert share_limit_call["ratio_limit"] == -1
    assert _has_call(t, "set_upload_limit")
    assert _calls_of(t, "set_upload_limit")[-1][1]["limit"] == 100 * 1024


def test_deprioritize_on_unmet_minimums_archivist_guard(share_limits_factory, torrent_factory, group_config_factory):
    """When rare swarm triggers min_num_seeds, demotes to bottom priority and clears force_start."""
    group_cfg = group_config_factory(
        priority=1.0,
        max_ratio=3.0,
        min_num_seeds=2,
        deprioritize_on_unmet_minimums=True,
        cleanup=True,
    )
    t = torrent_factory(
        hash="a" * 40,
        tags="",
        num_complete=1,  # rare swarm (1 seed <= 2)
        ratio=3.5,       # would otherwise be cleaned up
        force_start=True,
    )
    group_cfg["torrents"] = [t]
    sl = share_limits_factory(
        torrents=[t],
        share_limits_config=OrderedDict([("public", group_cfg)]),
    )

    sl.update_share_limits_for_group("public", group_cfg, [t])

    # Archivist guard added MinSeedsNotMet tag
    assert "MinSeedsNotMet" in t.tags
    # Demoted to bottom priority
    assert _has_call(t, "bottom_priority")
    # force_start cleared
    assert _has_call(t, "set_force_start")
    assert _calls_of(t, "set_force_start")[-1][1]["enable"] is False
    assert t.force_start is False
    # Not queued for deletion
    assert t.hash not in sl.tdel_dict


def test_config_parsing_of_new_flags(tmp_path):
    """Config.process_config_share_limits parses all four new boolean flags from data."""
    from modules.config import Config
    from modules.util import check

    dummy_cfg = tmp_path / "config.yml"
    dummy_cfg.write_text("")

    cfg = object.__new__(Config)
    cfg.config_path = str(dummy_cfg)
    cfg.data = {
        "share_limits": {
            "test_group": {
                "priority": 1,
                "cleanup": False,
                "prioritize_by_seed_leech_ratio": True,
                "reserve_upload_spots": True,
                "deprioritize_on_limit_reached": True,
                "deprioritize_on_unmet_minimums": True,
            }
        }
    }
    cfg.commands = {"share_limits": True}
    cfg.util = check(cfg)
    cfg.default_ignore_tags = []
    cfg.share_limits_custom_tags = []
    cfg.notify = lambda *args, **kwargs: None

    cfg.process_config_share_limits()

    parsed = cfg.share_limits["test_group"]
    assert parsed["prioritize_by_seed_leech_ratio"] is True
    assert parsed["reserve_upload_spots"] is True
    assert parsed["deprioritize_on_limit_reached"] is True
    assert parsed["deprioritize_on_unmet_minimums"] is True

