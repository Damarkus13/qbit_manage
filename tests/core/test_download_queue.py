"""Tests for DownloadQueue — seeder-based promotion and slow/stalled rotation."""

from __future__ import annotations

import json

from modules.core.download_queue import DownloadQueue
from tests.factories import FakeConfig
from tests.factories import FakeQbtManager
from tests.factories import FakeTorrent


def _make_cfg(tmp_path, **settings_overrides):
    settings = {
        "download_queue_promote_min_seeds": 10,
        "download_queue_demote_speed_kib": 50,
        "download_queue_grace_minutes": 10,
        "download_queue_scrape_trackers": True,
        "download_queue_scrape_ttl_minutes": 30,
    }
    settings.update(settings_overrides)
    cfg = FakeConfig(settings=settings)
    cfg.default_dir = str(tmp_path)
    return cfg


def _calls_of(torrent, name):
    return [c for c in torrent.calls if c[0] == name]


def test_slow_active_not_demoted_before_grace_period(tmp_path):
    active = FakeTorrent(
        name="Slow.Show.S01E01",
        hash="a" * 40,
        state="downloading",
        priority=1,
        dlspeed=10 * 1024,
        time_active=120,
    )
    queued = FakeTorrent(
        name="Waiting.Show.S01E01",
        hash="b" * 40,
        state="queuedDL",
        priority=2,
        num_complete=5,
    )
    cfg = _make_cfg(tmp_path, download_queue_scrape_trackers=False)
    qbt = FakeQbtManager(torrents=[active, queued], config=cfg)

    dq = DownloadQueue(qbt)
    assert dq.stats_demoted == 0
    assert not _calls_of(active, "bottom_priority")


def test_slow_and_stalled_active_demoted_after_grace_period(tmp_path):
    slow = FakeTorrent(
        name="Slow.Show.S01E01",
        hash="a" * 40,
        state="downloading",
        priority=1,
        dlspeed=10 * 1024,
        time_active=60,
    )
    stalled = FakeTorrent(
        name="Stalled.Show.S01E02",
        hash="b" * 40,
        state="stalledDL",
        priority=2,
        dlspeed=0,
        time_active=60,
    )
    queued = FakeTorrent(
        name="Waiting.Show.S01E03",
        hash="c" * 40,
        state="queuedDL",
        priority=3,
        num_complete=5,
    )
    cfg = _make_cfg(tmp_path, download_queue_scrape_trackers=False)
    qbt = FakeQbtManager(torrents=[slow, stalled, queued], config=cfg)

    dq_obj = object.__new__(DownloadQueue)
    dq_obj.qbt = qbt
    dq_obj.hashes = None
    dq_obj.config = cfg
    dq_obj.client = qbt.client
    dq_obj.stats_promoted = 0
    dq_obj.stats_demoted = 0
    dq_obj.promote_min_seeds = 10
    dq_obj.demote_speed_kib = 50
    dq_obj.grace_minutes = 10
    dq_obj.scrape_trackers = False
    dq_obj.scrape_ttl_minutes = 30
    dq_obj.state_path = str(tmp_path / "download_queue_state.json")

    dq_obj.manage_queue(now=1000.0)
    assert dq_obj.stats_demoted == 0

    dq_obj.manage_queue(now=1700.0)
    assert dq_obj.stats_demoted == 2
    assert len(_calls_of(slow, "bottom_priority")) == 1
    assert len(_calls_of(stalled, "bottom_priority")) == 1


def test_active_not_demoted_when_queue_is_empty(tmp_path):
    slow = FakeTorrent(
        name="Only.Show.S01E01",
        hash="a" * 40,
        state="downloading",
        priority=1,
        dlspeed=5 * 1024,
    )
    cfg = _make_cfg(tmp_path, download_queue_grace_minutes=0, download_queue_scrape_trackers=False)
    qbt = FakeQbtManager(torrents=[slow], config=cfg)

    dq = DownloadQueue(qbt)
    assert dq.stats_demoted == 0
    assert not _calls_of(slow, "bottom_priority")


def test_promotes_high_seeder_queued_ahead_of_low_seed_without_disrupting_healthy_active(tmp_path, monkeypatch):
    healthy = FakeTorrent(
        name="Healthy.Active",
        hash="1" * 40,
        state="downloading",
        priority=1,
        dlspeed=500 * 1024,
        num_complete=100,
    )
    low_seed = FakeTorrent(
        name="Low.Seed.Queued",
        hash="2" * 40,
        state="queuedDL",
        priority=2,
        num_complete=3,
    )
    fast_mid = FakeTorrent(
        name="Medium.High.Seed",
        hash="3" * 40,
        state="queuedDL",
        priority=3,
        num_complete=0,
    )
    fast_top = FakeTorrent(
        name="Highest.Seed",
        hash="4" * 40,
        state="queuedDL",
        priority=4,
        num_complete=0,
    )
    dead = FakeTorrent(
        name="Dead.Seed",
        hash="5" * 40,
        state="queuedDL",
        priority=2,
        num_complete=0,
    )
    low_seed.priority = 5

    scraped = {
        "3" * 40: 50,
        "4" * 40: 200,
        "5" * 40: 0,
    }
    monkeypatch.setattr(DownloadQueue, "_scrape_torrent_seeds", lambda self, t: scraped.get(t.hash))

    cfg = _make_cfg(tmp_path)
    qbt = FakeQbtManager(torrents=[healthy, dead, fast_mid, fast_top, low_seed], config=cfg)

    dq = DownloadQueue(qbt)
    assert dq.stats_promoted == 2
    assert dq.stats_demoted == 1
    assert len(_calls_of(fast_mid, "top_priority")) == 1
    assert len(_calls_of(fast_top, "top_priority")) == 1
    assert len(_calls_of(healthy, "top_priority")) == 1
    assert len(_calls_of(dead, "bottom_priority")) == 1
    assert not _calls_of(low_seed, "top_priority")


def test_idempotent_when_queue_already_ordered(tmp_path):
    healthy = FakeTorrent(
        name="Healthy.Active",
        hash="1" * 40,
        state="downloading",
        priority=1,
        dlspeed=500 * 1024,
        num_complete=100,
    )
    fast_queued = FakeTorrent(
        name="Fast.Queued",
        hash="2" * 40,
        state="queuedDL",
        priority=2,
        num_complete=50,
    )
    low_queued = FakeTorrent(
        name="Low.Queued",
        hash="3" * 40,
        state="queuedDL",
        priority=3,
        num_complete=4,
    )
    cfg = _make_cfg(tmp_path, download_queue_scrape_trackers=False)
    qbt = FakeQbtManager(torrents=[healthy, fast_queued, low_queued], config=cfg)

    dq = DownloadQueue(qbt)
    assert dq.stats_promoted == 0
    assert dq.stats_demoted == 0
    for tor in (healthy, fast_queued, low_queued):
        assert not tor.calls


def test_scrape_cache_reused_within_ttl(tmp_path, monkeypatch):
    low = FakeTorrent(name="Low.Queued", hash="1" * 40, state="queuedDL", priority=1, num_complete=2)
    cached_fast = FakeTorrent(name="Cached.Fast", hash="2" * 40, state="queuedDL", priority=2, num_complete=0)

    state_file = tmp_path / "download_queue_state.json"
    import time

    now = time.time()
    state_file.write_text(json.dumps({"torrents": {cached_fast.hash: {"scrape": {"seeds": 75, "ts": now - 60}}}}))

    scrape_calls = []
    monkeypatch.setattr(DownloadQueue, "_scrape_torrent_seeds", lambda self, t: scrape_calls.append(t.hash) or 0)

    cfg = _make_cfg(tmp_path)
    qbt = FakeQbtManager(torrents=[low, cached_fast], config=cfg)

    dq = DownloadQueue(qbt)
    assert scrape_calls == []
    assert dq.stats_promoted == 1
    assert len(_calls_of(cached_fast, "top_priority")) == 1


def test_dry_run_makes_no_modifications(tmp_path, monkeypatch):
    low = FakeTorrent(name="Low.Queued", hash="1" * 40, state="queuedDL", priority=1, num_complete=2)
    fast = FakeTorrent(name="Fast.Queued", hash="2" * 40, state="queuedDL", priority=2, num_complete=0)
    monkeypatch.setattr(DownloadQueue, "_scrape_torrent_seeds", lambda self, t: 80)

    cfg = _make_cfg(tmp_path)
    cfg.dry_run = True
    cfg.loglevel = "DRYRUN"
    qbt = FakeQbtManager(torrents=[low, fast], config=cfg)

    dq = DownloadQueue(qbt)
    assert dq.stats_promoted == 1
    assert not fast.calls
    assert not (tmp_path / "download_queue_state.json").exists()
