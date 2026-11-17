import json
import os
import random
import socket
import struct
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import bencodepy

from modules import util

_ACTIVE_DL_STATES = {"downloading", "stalledDL", "metaDL"}
_STALLED_DL_STATES = {"stalledDL", "metaDL"}
_UDP_PROTOCOL_ID = 0x41727101980


class DownloadQueue:
    """Manage download queue priorities by scraping queued seeders and rotating slow/stalled downloads."""

    def __init__(self, qbit_manager, hashes: list[str] | None = None):
        self.qbt = qbit_manager
        self.hashes = hashes
        self.config = qbit_manager.config
        self.client = qbit_manager.client
        self.stats_promoted = 0
        self.stats_demoted = 0

        self.promote_min_seeds = self.config.settings.get("download_queue_promote_min_seeds", 10)
        self.demote_speed_kib = self.config.settings.get("download_queue_demote_speed_kib", 50)
        self.grace_minutes = self.config.settings.get("download_queue_grace_minutes", 10)
        self.scrape_trackers = self.config.settings.get("download_queue_scrape_trackers", True)
        self.scrape_ttl_minutes = self.config.settings.get("download_queue_scrape_ttl_minutes", 30)

        self.state_path = os.path.join(self.config.default_dir, "download_queue_state.json")
        self.manage_queue()

    def _load_state(self) -> dict:
        if not os.path.exists(self.state_path):
            return {"torrents": {}}
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and isinstance(data.get("torrents"), dict):
                return data
        except Exception as exc:
            util.logger.debug(f"Could not load download queue state ({self.state_path}): {exc}")
        return {"torrents": {}}

    def _save_state(self, state: dict) -> None:
        if self.config.dry_run:
            return
        tmp_path = f"{self.state_path}.tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(state, fh)
            os.replace(tmp_path, self.state_path)
        except Exception as exc:
            util.logger.debug(f"Could not save download queue state ({self.state_path}): {exc}")

    @staticmethod
    def _scrape_udp(url: str, info_hash_bytes: bytes, timeout: float = 2.5) -> int | None:
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname
        port = parsed.port or 80
        if not host:
            return None
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        try:
            tx_id = random.randint(0, 0x7FFFFFFF)
            sock.sendto(struct.pack("!QII", _UDP_PROTOCOL_ID, 0, tx_id), (host, port))
            data, _ = sock.recvfrom(1024)
            if len(data) < 16:
                return None
            action, resp_tx, conn_id = struct.unpack("!IIQ", data[:16])
            if action != 0 or resp_tx != tx_id:
                return None
            tx_id2 = random.randint(0, 0x7FFFFFFF)
            sock.sendto(struct.pack("!QII20s", conn_id, 2, tx_id2, info_hash_bytes), (host, port))
            data2, _ = sock.recvfrom(1024)
            if len(data2) < 20:
                return None
            action2, resp_tx2, seeds, _completed, _leeches = struct.unpack("!IIIII", data2[:20])
            if action2 != 2 or resp_tx2 != tx_id2:
                return None
            return int(seeds)
        except Exception:
            return None
        finally:
            sock.close()

    @staticmethod
    def _scrape_http(url: str, info_hash_bytes: bytes, timeout: float = 3.0) -> int | None:
        if "/announce" not in url:
            return None
        scrape_url = url.replace("/announce", "/scrape")
        sep = "&" if "?" in scrape_url else "?"
        full_url = f"{scrape_url}{sep}info_hash={urllib.parse.quote_from_bytes(info_hash_bytes)}"
        try:
            req = urllib.request.Request(full_url, headers={"User-Agent": "qBittorrent/5.3.0"})
            raw = urllib.request.urlopen(req, timeout=timeout).read()
            decoded = bencodepy.decode(raw)
            files = decoded.get(b"files", {}) if isinstance(decoded, dict) else {}
            entry = files.get(info_hash_bytes)
            if entry is None and files:
                entry = next(iter(files.values()))
            if isinstance(entry, dict) and b"complete" in entry:
                return int(entry[b"complete"])
            return None
        except Exception:
            return None

    def _scrape_torrent_seeds(self, torrent) -> int | None:
        try:
            info_hash_bytes = bytes.fromhex(torrent.hash)
        except ValueError:
            return None
        urls = [
            tr.url
            for tr in getattr(torrent, "trackers", [])
            if getattr(tr, "url", "").startswith(("udp://", "http://", "https://"))
        ]
        best_seeds: int | None = None
        for url in urls[:6]:
            res = self._scrape_udp(url, info_hash_bytes) if url.startswith("udp://") else self._scrape_http(url, info_hash_bytes)
            if res is not None and (best_seeds is None or res > best_seeds):
                best_seeds = res
        return best_seeds

    def _resolve_queued_seeders(self, queued_torrents: list, state_torrents: dict, now: float) -> dict[str, int]:
        seed_map: dict[str, int] = {}
        to_scrape = []
        ttl_seconds = self.scrape_ttl_minutes * 60

        for torrent in queued_torrents:
            num_complete = getattr(torrent, "num_complete", 0) or 0
            if num_complete > 0:
                seed_map[torrent.hash] = int(num_complete)
                continue
            cached = state_torrents.get(torrent.hash, {}).get("scrape")
            if cached and (now - cached.get("ts", 0)) < ttl_seconds:
                seed_map[torrent.hash] = int(cached["seeds"])
                continue
            if self.scrape_trackers:
                to_scrape.append(torrent)

        if to_scrape:
            with ThreadPoolExecutor(max_workers=12) as pool:
                scraped_results = list(pool.map(self._scrape_torrent_seeds, to_scrape))
            for torrent, seeds in zip(to_scrape, scraped_results):
                if seeds is not None:
                    seed_map[torrent.hash] = seeds
                    entry = state_torrents.setdefault(torrent.hash, {})
                    entry["scrape"] = {"seeds": seeds, "ts": now}

        return seed_map

    def _should_demote_active(self, torrent, active_since: float, now: float) -> bool:
        if (now - active_since) < (self.grace_minutes * 60):
            return False
        if torrent.state in _STALLED_DL_STATES:
            return True
        if self.demote_speed_kib > 0 and getattr(torrent, "dlspeed", 0) < (self.demote_speed_kib * 1024):
            return True
        return False

    def manage_queue(self, now: float | None = None) -> None:
        """Evaluate active downloads for demotion and promote well-seeded queued downloads."""
        util.logger.separator("Managing Download Queue", space=False, border=False)
        now = time.time() if now is None else now

        torrent_list = self.qbt.torrent_list
        if self.hashes:
            torrent_list = self.qbt.get_torrents({"torrent_hashes": self.hashes})

        state = self._load_state()
        state_torrents = state["torrents"]

        # Prune completed or removed torrents from persistent state on full runs
        if self.hashes is None:
            live_hashes = {t.hash for t in torrent_list}
            for stale_hash in list(state_torrents.keys()):
                if stale_hash not in live_hashes:
                    state_torrents.pop(stale_hash, None)

        active_torrents = [t for t in torrent_list if t.state in _ACTIVE_DL_STATES]
        queued_torrents = [t for t in torrent_list if t.state == "queuedDL"]

        # Reset active_since once a torrent leaves active downloading states
        active_hashes = {t.hash for t in active_torrents}
        for t_hash, entry in state_torrents.items():
            if t_hash not in active_hashes:
                entry.pop("active_since", None)

        # Track active window and identify slow/stalled active downloads to rotate
        demoted_hashes: set[str] = set()
        healthy_active = []
        for torrent in active_torrents:
            entry = state_torrents.setdefault(torrent.hash, {})
            if "active_since" not in entry:
                time_active = getattr(torrent, "time_active", 0) or 0
                grace_secs = self.grace_minutes * 60
                # Preserve short initial age for newly added torrents; reset full grace if already active longer
                initial_offset = time_active if 0 < time_active < grace_secs else 0
                entry["active_since"] = now - initial_offset

            if queued_torrents and self._should_demote_active(torrent, entry["active_since"], now):
                demoted_hashes.add(torrent.hash)
                self.stats_demoted += 1
                dl_kib = getattr(torrent, "dlspeed", 0) / 1024
                util.logger.print_line(
                    util.logger.insert_space(
                        f"Demoting slow/stalled download ({torrent.state}, {dl_kib:.1f} KiB/s): {torrent.name}",
                        3,
                    ),
                    self.config.loglevel,
                )
                if not self.config.dry_run:
                    torrent.bottom_priority()
                    entry.pop("active_since", None)
            else:
                healthy_active.append(torrent)

        # Resolve seeder counts for queued downloads and promote > threshold without disrupting healthy active downloads
        seed_map = self._resolve_queued_seeders(queued_torrents, state_torrents, now)

        non_qualifying = [
            t for t in queued_torrents if seed_map.get(t.hash, -1) <= self.promote_min_seeds and getattr(t, "priority", 0) > 0
        ]
        min_non_qualifying_prio = min((t.priority for t in non_qualifying), default=None)

        to_promote = []
        if min_non_qualifying_prio is not None:
            for torrent in queued_torrents:
                seeds = seed_map.get(torrent.hash, -1)
                prio = getattr(torrent, "priority", 0)
                if seeds > self.promote_min_seeds and (prio == 0 or prio > min_non_qualifying_prio):
                    to_promote.append((seeds, torrent))

        if to_promote:
            # Apply top_priority ascending by seeders so highest-seeder torrent lands ahead of lower ones
            to_promote.sort(key=lambda item: (item[0], getattr(item[1], "added_on", 0)))
            for seeds, torrent in to_promote:
                self.stats_promoted += 1
                util.logger.print_line(
                    util.logger.insert_space(f"Promoting well-seeded download ({seeds} seeds): {torrent.name}", 3),
                    self.config.loglevel,
                )
                if not self.config.dry_run:
                    torrent.top_priority()

            # Re-pin healthy active downloads at the very top so they are never interrupted mid-download
            if not self.config.dry_run:
                for torrent in sorted(healthy_active, key=lambda t: getattr(t, "priority", 0), reverse=True):
                    torrent.top_priority()

        # Sink confirmed 0-seeder queued torrents behind >0/unknown queued torrents
        positive_or_unknown = [t for t in queued_torrents if seed_map.get(t.hash, -1) != 0 and getattr(t, "priority", 0) > 0]
        max_non_zero_prio = max((t.priority for t in positive_or_unknown), default=None)
        if max_non_zero_prio is not None:
            for torrent in queued_torrents:
                if seed_map.get(torrent.hash, -1) == 0 and getattr(torrent, "priority", 0) < max_non_zero_prio:
                    self.stats_demoted += 1
                    util.logger.print_line(
                        util.logger.insert_space(f"Demoting 0-seeder queued download: {torrent.name}", 3),
                        self.config.loglevel,
                    )
                    if not self.config.dry_run:
                        torrent.bottom_priority()

        self._save_state(state)
