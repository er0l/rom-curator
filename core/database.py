"""SQLite persistence for ROM inventory."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import time


SCHEMA = """
CREATE TABLE IF NOT EXISTS roms (
    id INTEGER PRIMARY KEY,
    system TEXT,
    title TEXT,
    filename TEXT,
    extension TEXT,
    path TEXT UNIQUE,
    relative_path TEXT,
    size INTEGER,
    modified INTEGER,
    region TEXT,
    revision TEXT,
    disc TEXT,
    is_beta INTEGER,
    is_proto INTEGER,
    is_translation INTEGER,
    is_hack INTEGER,
    arcade_system TEXT,
    scan_key TEXT,
    created_at INTEGER,
    updated_at INTEGER
);

CREATE TABLE IF NOT EXISTS scan_state (
    path TEXT PRIMARY KEY,
    scan_key TEXT NOT NULL,
    last_seen INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_system ON roms(system);
CREATE INDEX IF NOT EXISTS idx_title ON roms(title);
CREATE INDEX IF NOT EXISTS idx_region ON roms(region);
CREATE INDEX IF NOT EXISTS idx_extension ON roms(extension);
CREATE INDEX IF NOT EXISTS idx_path ON roms(path);
CREATE INDEX IF NOT EXISTS idx_scan_key ON roms(scan_key);
CREATE INDEX IF NOT EXISTS idx_scan_state_last_seen ON scan_state(last_seen);

CREATE TABLE IF NOT EXISTS romm_roms (
    romm_id            INTEGER PRIMARY KEY,
    platform_slug      TEXT,
    canonical_system   TEXT,
    fs_name            TEXT,
    fs_stem            TEXT,
    name               TEXT,
    total_rating       REAL,
    aggregated_rating  REAL,
    igdb_id            TEXT,
    is_identified      INTEGER,
    genres             TEXT,
    themes             TEXT,
    game_modes         TEXT,
    player_count       INTEGER,
    year               INTEGER,
    hltb_main          REAL,
    hltb_main_extra    REAL,
    hltb_completionist REAL,
    sibling_count      INTEGER,
    has_cover          INTEGER,
    regions            TEXT,
    tags               TEXT,
    synced_at          INTEGER
);

CREATE INDEX IF NOT EXISTS idx_romm_fs_name ON romm_roms(fs_name);
CREATE INDEX IF NOT EXISTS idx_romm_canonical ON romm_roms(canonical_system);
CREATE INDEX IF NOT EXISTS idx_romm_platform ON romm_roms(platform_slug);

CREATE TABLE IF NOT EXISTS mame_machines (
    name            TEXT PRIMARY KEY,
    description     TEXT,
    year            TEXT,
    manufacturer    TEXT,
    sourcefile      TEXT,
    arcade_system   TEXT,
    cloneof         TEXT,
    isbios          INTEGER,
    isdevice        INTEGER,
    ismechanical    INTEGER,
    runnable        INTEGER,
    driver_status   TEXT,
    players         INTEGER,
    control_types   TEXT,
    display_type    TEXT,
    display_rotate  INTEGER,
    imported_at     INTEGER
);

CREATE INDEX IF NOT EXISTS idx_mame_arcade_system ON mame_machines(arcade_system);
CREATE INDEX IF NOT EXISTS idx_mame_cloneof       ON mame_machines(cloneof);
CREATE INDEX IF NOT EXISTS idx_mame_sourcefile    ON mame_machines(sourcefile);
"""


class InventoryDatabase:
    def __init__(self, database_path: str | Path) -> None:
        self.path = Path(database_path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self._configure_connection()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "InventoryDatabase":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def initialize(self) -> None:
        self.connection.executescript(SCHEMA)
        self._migrate()
        self.connection.commit()

    def _migrate(self) -> None:
        cols = {row[1] for row in self.connection.execute("PRAGMA table_info(roms)")}
        if "disc" not in cols:
            self.connection.execute("ALTER TABLE roms ADD COLUMN disc TEXT DEFAULT NULL")
        if "arcade_system" not in cols:
            self.connection.execute("ALTER TABLE roms ADD COLUMN arcade_system TEXT DEFAULT NULL")

        romm_cols = {row[1] for row in self.connection.execute("PRAGMA table_info(romm_roms)")}
        if "fs_stem" not in romm_cols:
            self.connection.execute("ALTER TABLE romm_roms ADD COLUMN fs_stem TEXT")
            # Populate fs_stem from fs_name: strip last extension (e.g. "Game.smc" → "Game",
            # "J. League (Japan).zip" → "J. League (Japan)").
            # SQLite lacks REVERSE, so detect the last dot by checking from the right.
            self.connection.execute("""
                UPDATE romm_roms SET fs_stem = CASE
                    WHEN SUBSTR(fs_name, -5, 1) = '.' THEN SUBSTR(fs_name, 1, LENGTH(fs_name) - 5)
                    WHEN SUBSTR(fs_name, -4, 1) = '.' THEN SUBSTR(fs_name, 1, LENGTH(fs_name) - 4)
                    WHEN SUBSTR(fs_name, -3, 1) = '.' THEN SUBSTR(fs_name, 1, LENGTH(fs_name) - 3)
                    WHEN SUBSTR(fs_name, -2, 1) = '.' THEN SUBSTR(fs_name, 1, LENGTH(fs_name) - 2)
                    ELSE fs_name
                END
            """)
            self.connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_romm_fs_stem ON romm_roms(canonical_system, fs_stem)"
            )

    def _configure_connection(self) -> None:
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.execute("PRAGMA temp_store=MEMORY")
        self.connection.execute("PRAGMA mmap_size=30000000000")

    def get_scan_keys(self) -> dict[str, str]:
        rows = self.connection.execute(
            """
            SELECT scan_state.path, scan_state.scan_key
            FROM scan_state
            INNER JOIN roms ON roms.path = scan_state.path
            """
        ).fetchall()
        return {row["path"]: row["scan_key"] for row in rows}

    def mark_seen(self, path: str, scan_key: str, scan_timestamp: int) -> None:
        self.connection.execute(
            """
            INSERT INTO scan_state (path, scan_key, last_seen)
            VALUES (?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                scan_key = excluded.scan_key,
                last_seen = excluded.last_seen
            """,
            (path, scan_key, scan_timestamp),
        )

    def upsert_rom(self, record: dict[str, object]) -> None:
        now = int(time.time())
        payload = {**record, "created_at": now, "updated_at": now}
        self.connection.execute(
            """
            INSERT INTO roms (
                system, title, filename, extension, path, relative_path, size,
                modified, region, revision, disc, is_beta, is_proto, is_translation,
                is_hack, scan_key, created_at, updated_at
            )
            VALUES (
                :system, :title, :filename, :extension, :path, :relative_path,
                :size, :modified, :region, :revision, :disc, :is_beta, :is_proto,
                :is_translation, :is_hack, :scan_key, :created_at, :updated_at
            )
            ON CONFLICT(path) DO UPDATE SET
                system = excluded.system,
                title = excluded.title,
                filename = excluded.filename,
                extension = excluded.extension,
                relative_path = excluded.relative_path,
                size = excluded.size,
                modified = excluded.modified,
                region = excluded.region,
                revision = excluded.revision,
                disc = excluded.disc,
                is_beta = excluded.is_beta,
                is_proto = excluded.is_proto,
                is_translation = excluded.is_translation,
                is_hack = excluded.is_hack,
                scan_key = excluded.scan_key,
                updated_at = excluded.updated_at
            """,
            payload,
        )

    def remove_stale(self, scan_timestamp: int) -> int:
        stale_from_state = self.fetch_scalar(
            "SELECT COUNT(*) FROM scan_state WHERE last_seen < ?",
            (scan_timestamp,),
        )
        orphaned_roms = self.fetch_scalar(
            """
            SELECT COUNT(*)
            FROM roms
            WHERE path NOT IN (SELECT path FROM scan_state)
            """
        )
        self.connection.execute(
            """
            DELETE FROM roms
            WHERE path IN (
                SELECT path
                FROM scan_state
                WHERE last_seen < ?
            )
            """,
            (scan_timestamp,),
        )
        self.connection.execute(
            """
            DELETE FROM roms
            WHERE path NOT IN (SELECT path FROM scan_state)
            """
        )
        self.connection.execute("DELETE FROM scan_state WHERE last_seen < ?", (scan_timestamp,))
        return stale_from_state + orphaned_roms

    def commit(self) -> None:
        self.connection.commit()

    def fetch_scalar(self, query: str, params: tuple[object, ...] = ()) -> int:
        row = self.connection.execute(query, params).fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def fetch_all(self, query: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
        return self.connection.execute(query, params).fetchall()

    def upsert_romm_rom(self, record: dict[str, object]) -> None:
        self.connection.execute(
            """
            INSERT INTO romm_roms (
                romm_id, platform_slug, canonical_system, fs_name, fs_stem, name,
                total_rating, aggregated_rating, igdb_id, is_identified,
                genres, themes, game_modes, player_count, year,
                hltb_main, hltb_main_extra, hltb_completionist,
                sibling_count, has_cover, regions, tags, synced_at
            )
            VALUES (
                :romm_id, :platform_slug, :canonical_system, :fs_name, :fs_stem, :name,
                :total_rating, :aggregated_rating, :igdb_id, :is_identified,
                :genres, :themes, :game_modes, :player_count, :year,
                :hltb_main, :hltb_main_extra, :hltb_completionist,
                :sibling_count, :has_cover, :regions, :tags, :synced_at
            )
            ON CONFLICT(romm_id) DO UPDATE SET
                platform_slug      = excluded.platform_slug,
                canonical_system   = excluded.canonical_system,
                fs_name            = excluded.fs_name,
                fs_stem            = excluded.fs_stem,
                name               = excluded.name,
                total_rating       = excluded.total_rating,
                aggregated_rating  = excluded.aggregated_rating,
                igdb_id            = excluded.igdb_id,
                is_identified      = excluded.is_identified,
                genres             = excluded.genres,
                themes             = excluded.themes,
                game_modes         = excluded.game_modes,
                player_count       = excluded.player_count,
                year               = excluded.year,
                hltb_main          = excluded.hltb_main,
                hltb_main_extra    = excluded.hltb_main_extra,
                hltb_completionist = excluded.hltb_completionist,
                sibling_count      = excluded.sibling_count,
                has_cover          = excluded.has_cover,
                regions            = excluded.regions,
                tags               = excluded.tags,
                synced_at          = excluded.synced_at
            """,
            record,
        )

    def clear_romm_roms(self) -> None:
        self.connection.execute("DELETE FROM romm_roms")
        self.connection.commit()

    def upsert_mame_machine(self, machine, imported_at: int) -> None:
        self.connection.execute(
            """
            INSERT INTO mame_machines (
                name, description, year, manufacturer, sourcefile, arcade_system,
                cloneof, isbios, isdevice, ismechanical, runnable, driver_status,
                players, control_types, display_type, display_rotate, imported_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                description    = excluded.description,
                year           = excluded.year,
                manufacturer   = excluded.manufacturer,
                sourcefile     = excluded.sourcefile,
                arcade_system  = excluded.arcade_system,
                cloneof        = excluded.cloneof,
                isbios         = excluded.isbios,
                isdevice       = excluded.isdevice,
                ismechanical   = excluded.ismechanical,
                runnable       = excluded.runnable,
                driver_status  = excluded.driver_status,
                players        = excluded.players,
                control_types  = excluded.control_types,
                display_type   = excluded.display_type,
                display_rotate = excluded.display_rotate,
                imported_at    = excluded.imported_at
            """,
            (
                machine.name,
                machine.description,
                machine.year,
                machine.manufacturer,
                machine.sourcefile,
                machine.arcade_system,
                machine.cloneof,
                int(machine.isbios),
                int(machine.isdevice),
                int(machine.ismechanical),
                int(machine.runnable),
                machine.driver_status,
                machine.players,
                ";".join(machine.control_types) if machine.control_types else None,
                machine.display_type,
                machine.display_rotate,
                imported_at,
            ),
        )

    def update_arcade_systems(self) -> int:
        """Classify ROMs in system='arcade' using mame_machines. Returns updated row count."""
        self.connection.execute(
            """
            UPDATE roms
            SET arcade_system = (
                SELECT mm.arcade_system
                FROM mame_machines mm
                WHERE mm.name = roms.title
            )
            WHERE roms.system = 'arcade'
              AND roms.arcade_system IS NULL
            """
        )
        return self.connection.execute(
            "SELECT COUNT(*) FROM roms WHERE system = 'arcade' AND arcade_system IS NOT NULL"
        ).fetchone()[0]

    def iter_roms_by_systems(self, systems: list[str]):
        """Yield ROM rows for the given systems.

        Also yields ROMs from system='arcade' whose arcade_system matches a
        requested system, enabling export routing of classified arcade ROMs to
        their correct sub-system folders (cps1, cps2, neogeo, etc.).
        """
        if not systems:
            return
        placeholders = ",".join("?" for _ in systems)
        query = f"""
            SELECT
                roms.*,
                rr.total_rating,
                rr.aggregated_rating,
                rr.is_identified,
                rr.genres,
                rr.game_modes,
                rr.player_count,
                rr.year        AS romm_year,
                rr.hltb_main,
                rr.hltb_main_extra,
                rr.hltb_completionist,
                rr.has_cover,
                rr.sibling_count,
                mm.cloneof        AS mame_cloneof,
                mm.driver_status,
                mm.display_rotate,
                mm.isbios         AS mame_isbios,
                mm.isdevice       AS mame_isdevice,
                mm.ismechanical   AS mame_ismechanical,
                mm.description    AS mame_description,
                mm.sourcefile     AS mame_sourcefile,
                mm.control_types  AS mame_control_types,
                mm.players        AS mame_players
            FROM roms
            LEFT JOIN romm_roms rr
                ON rr.canonical_system = roms.system
                AND (rr.fs_name = roms.filename
                     OR rr.fs_stem = CASE
                         WHEN SUBSTR(roms.filename, -5, 1) = '.' THEN SUBSTR(roms.filename, 1, LENGTH(roms.filename) - 5)
                         WHEN SUBSTR(roms.filename, -4, 1) = '.' THEN SUBSTR(roms.filename, 1, LENGTH(roms.filename) - 4)
                         WHEN SUBSTR(roms.filename, -3, 1) = '.' THEN SUBSTR(roms.filename, 1, LENGTH(roms.filename) - 3)
                         WHEN SUBSTR(roms.filename, -2, 1) = '.' THEN SUBSTR(roms.filename, 1, LENGTH(roms.filename) - 2)
                         ELSE roms.filename
                     END)
            LEFT JOIN mame_machines mm
                ON mm.name = roms.title
                AND roms.system = 'arcade'
            WHERE roms.system IN ({placeholders})
               OR (roms.system = 'arcade'
                   AND roms.arcade_system IS NOT NULL
                   AND roms.arcade_system IN ({placeholders}))
            ORDER BY roms.system, roms.title, roms.filename
        """
        params = tuple(systems) * 2
        seen_ids: set[int] = set()
        for row in self.connection.execute(query, params):
            row_id = row["id"]
            if row_id not in seen_ids:
                seen_ids.add(row_id)
                yield row
