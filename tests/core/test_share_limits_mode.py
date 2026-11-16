"""Tests for share_limits_mode validation and passthrough to set_share_limits()."""

from __future__ import annotations

from collections import OrderedDict

import pytest

from modules.config import SHARE_LIMITS_MODES
from modules.config import validate_share_limits_mode
from modules.util import Failed
from tests.conftest import calls_of


class TestShareLimitsModeValidation:
    """Tests for share_limits_mode defaults and enum validation."""

    def test_default_value_in_group_config_factory(self, group_config_factory):
        """When share_limits_mode is not specified, defaults to 'Default'."""
        config = group_config_factory()
        assert config["share_limits_mode"] == "Default"

    @pytest.mark.parametrize("raw", [None, ""])
    def test_validator_resolves_empty_to_default(self, raw):
        """None or empty string resolves to 'Default'."""
        assert validate_share_limits_mode(raw, group="test_group") == "Default"

    @pytest.mark.parametrize("mode", list(SHARE_LIMITS_MODES.keys()))
    def test_valid_mode_passes_validator(self, mode):
        """All SHARE_LIMITS_MODES keys are accepted verbatim."""
        assert validate_share_limits_mode(mode, group="test_group") == mode

    @pytest.mark.parametrize("bad_mode", ["BogusMode", 123, True])
    def test_invalid_mode_raises_failed(self, bad_mode):
        """Invalid or non-string share_limits_mode values raise Failed."""
        with pytest.raises(Failed, match="invalid share_limits_mode"):
            validate_share_limits_mode(bad_mode, group="test_group")


class TestShareLimitsModePassthrough:
    """Tests that share_limits_mode is passed at all set_share_limits() call sites."""

    @pytest.mark.parametrize("mode", ["Default", "MatchAny", "MatchAll"])
    def test_set_limits_passes_share_limits_mode(self, share_limits_factory, torrent_factory, mode):
        """set_limits() forwards share_limits_mode to torrent.set_share_limits()."""
        sl = share_limits_factory()
        t = torrent_factory(max_ratio=-1.0, max_seeding_time=-1)

        sl.set_limits(t, max_ratio=2.0, max_seeding_time=3600, share_limits_mode=mode)

        calls = calls_of(t, "set_share_limits")
        assert len(calls) == 1
        assert calls[0][1]["share_limits_mode"] == mode

    def test_cleanup_false_limit_reached_passes_default_mode(self, share_limits_factory, torrent_factory, group_config_factory):
        """Clearing share limits when cleanup=False passes share_limits_mode='Default'."""
        group_cfg = group_config_factory(
            priority=1.0,
            max_ratio=3.0,
            cleanup=False,
            deprioritize_on_limit_reached=True,
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

        calls = calls_of(t, "set_share_limits")
        assert calls
        assert calls[-1][1]["share_limits_mode"] == "Default"

    def test_unmet_minimums_reset_passes_default_mode(self, share_limits_factory, torrent_factory, group_config_factory):
        """Unmet minimum seed guard reset passes share_limits_mode='Default'."""
        group_cfg = group_config_factory(
            priority=1.0,
            max_ratio=3.0,
            min_num_seeds=2,
            cleanup=True,
        )
        t = torrent_factory(
            hash="b" * 40,
            tags="",
            num_complete=1,
            ratio=3.5,
        )
        group_cfg["torrents"] = [t]
        sl = share_limits_factory(
            torrents=[t],
            share_limits_config=OrderedDict([("public", group_cfg)]),
        )

        sl.update_share_limits_for_group("public", group_cfg, [t])

        calls = calls_of(t, "set_share_limits")
        assert calls
        assert calls[-1][1]["share_limits_mode"] == "Default"
