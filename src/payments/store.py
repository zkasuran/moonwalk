"""SQLite-backed async payment record store."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import aiosqlite

from ..domain.models import (
    ChannelMeter,
    ChannelSettlement,
    MarketplaceService,
    PaymentRecord,
    PaymentStatus,
)

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS payment_records (
    payment_id       TEXT PRIMARY KEY,
    guild_id         TEXT NOT NULL,
    channel_id       TEXT NOT NULL,
    user_id          TEXT NOT NULL,
    command_name     TEXT NOT NULL,
    command_args     TEXT NOT NULL DEFAULT '{}',
    price_atomic     INTEGER NOT NULL DEFAULT 0,
    pay_to           TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'pending',
    tx_hash          TEXT NOT NULL DEFAULT '',
    payer_address    TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL,
    paid_at          TEXT,
    result           TEXT NOT NULL DEFAULT '',
    interaction_token TEXT NOT NULL DEFAULT '',
    application_id   TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS guild_commands (
    guild_id        TEXT NOT NULL,
    command_name    TEXT NOT NULL,
    price_atomic    INTEGER NOT NULL DEFAULT 10000,
    description     TEXT NOT NULL DEFAULT '',
    enabled         INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (guild_id, command_name)
);

CREATE TABLE IF NOT EXISTS marketplace_services (
    service_id      TEXT PRIMARY KEY,
    guild_id        TEXT NOT NULL,
    lister_id       TEXT NOT NULL,
    name            TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    url             TEXT NOT NULL,
    price_atomic    INTEGER NOT NULL DEFAULT 0,
    wallet          TEXT NOT NULL,
    verified        INTEGER NOT NULL DEFAULT 0,
    verified_by     TEXT NOT NULL DEFAULT '',
    enabled         INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL
);

-- Channel rail: the off-chain half of a nanopayment channel. One row per
-- subject, holding the running cumulative and the newest signed voucher, so a
-- restart mid-batch loses nothing and a redeem can be rebuilt from the store.
CREATE TABLE IF NOT EXISTS channel_meters (
    channel_id        TEXT NOT NULL,
    subject           TEXT NOT NULL,
    guild_id          TEXT NOT NULL DEFAULT '',
    user_id           TEXT NOT NULL DEFAULT '',
    cumulative_atomic INTEGER NOT NULL DEFAULT 0,
    settled_atomic    INTEGER NOT NULL DEFAULT 0,
    calls             INTEGER NOT NULL DEFAULT 0,
    voucher_json      TEXT NOT NULL DEFAULT '',
    updated_at        TEXT NOT NULL,
    PRIMARY KEY (channel_id, subject)
);

CREATE TABLE IF NOT EXISTS channel_settlements (
    settlement_id   TEXT PRIMARY KEY,
    channel_id      TEXT NOT NULL,
    tx_hash         TEXT NOT NULL,
    total_atomic    INTEGER NOT NULL DEFAULT 0,
    subject_count   INTEGER NOT NULL DEFAULT 0,
    calls           INTEGER NOT NULL DEFAULT 0,
    block_number    INTEGER NOT NULL DEFAULT 0,
    gas_fee_atomic  INTEGER NOT NULL DEFAULT 0,
    settled_at      TEXT NOT NULL
);
"""


class PaymentStore:
    """Async SQLite store for payment records and guild configs."""

    def __init__(self, db_path: str) -> None:
        self._path = db_path

    async def init(self) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.executescript(_CREATE_SQL)
            # Migrate a pre-marketplace DB in place: older payment_records have
            # no pay_to column and ALTER ADD COLUMN is a no-op re-run guard.
            async with db.execute("PRAGMA table_info(payment_records)") as cursor:
                cols = {row[1] for row in await cursor.fetchall()}
            if "pay_to" not in cols:
                await db.execute(
                    "ALTER TABLE payment_records ADD COLUMN pay_to TEXT NOT NULL DEFAULT ''"
                )
            await db.commit()

    async def create_payment(self, record: PaymentRecord) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO payment_records (
                    payment_id, guild_id, channel_id, user_id,
                    command_name, command_args, price_atomic, pay_to,
                    status, tx_hash, payer_address, created_at, paid_at,
                    result, interaction_token, application_id
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    record.payment_id,
                    record.guild_id,
                    record.channel_id,
                    record.user_id,
                    record.command_name,
                    json.dumps(record.command_args),
                    record.price_atomic,
                    record.pay_to,
                    record.status.value,
                    record.tx_hash,
                    record.payer_address,
                    record.created_at.isoformat(),
                    record.paid_at.isoformat() if record.paid_at else None,
                    record.result,
                    record.interaction_token,
                    record.application_id,
                ),
            )
            await db.commit()

    async def get_payment(self, payment_id: str) -> PaymentRecord | None:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM payment_records WHERE payment_id = ?", (payment_id,)
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return None
        return _row_to_record(row)

    async def mark_paid(
        self,
        payment_id: str,
        tx_hash: str,
        payer_address: str,
        result: str = "",
    ) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                UPDATE payment_records
                SET status = ?, tx_hash = ?, payer_address = ?, paid_at = ?, result = ?
                WHERE payment_id = ?
                """,
                (
                    PaymentStatus.PAID.value,
                    tx_hash,
                    payer_address,
                    datetime.now(timezone.utc).isoformat(),
                    result,
                    payment_id,
                ),
            )
            await db.commit()

    async def recent_settlements(self, limit: int = 5) -> list[PaymentRecord]:
        """Return the most recent real settlements, newest first.

        A settlement is a PAID record with a non-empty on-chain tx hash, so a
        pending or failed attempt (or a paid row that never got a hash) never
        shows up on the public proof wall.
        """
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM payment_records WHERE status = ? AND tx_hash != ''"
                " ORDER BY paid_at DESC, created_at DESC LIMIT ?",
                (PaymentStatus.PAID.value, limit),
            ) as cursor:
                rows = await cursor.fetchall()
        return [_row_to_record(row) for row in rows]

    async def total_spent_atomic(self, user_id: str) -> int:
        """Sum of settled (paid) spend for a user, in USDC atomic units."""
        async with aiosqlite.connect(self._path) as db:
            async with db.execute(
                "SELECT COALESCE(SUM(price_atomic), 0) FROM payment_records"
                " WHERE user_id = ? AND status = ?",
                (user_id, PaymentStatus.PAID.value),
            ) as cursor:
                row = await cursor.fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    async def mark_failed(self, payment_id: str, reason: str = "") -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "UPDATE payment_records SET status = ?, result = ? WHERE payment_id = ?",
                (PaymentStatus.FAILED.value, reason, payment_id),
            )
            await db.commit()

    async def set_command_price(
        self,
        guild_id: str,
        command_name: str,
        price_atomic: int,
        description: str = "",
        enabled: bool = True,
    ) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO guild_commands
                    (guild_id, command_name, price_atomic, description, enabled)
                VALUES (?,?,?,?,?)
                ON CONFLICT(guild_id, command_name)
                DO UPDATE SET price_atomic=excluded.price_atomic,
                              description=excluded.description,
                              enabled=excluded.enabled
                """,
                (guild_id, command_name, price_atomic, description, 1 if enabled else 0),
            )
            await db.commit()

    async def get_command_price(self, guild_id: str, command_name: str) -> int:
        """Return price in atomic units for the command. Returns 0 if not configured."""
        async with aiosqlite.connect(self._path) as db:
            async with db.execute(
                "SELECT price_atomic FROM guild_commands"
                " WHERE guild_id=? AND command_name=? AND enabled=1",
                (guild_id, command_name),
            ) as cursor:
                row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def list_guild_commands(self, guild_id: str) -> list[tuple[str, int, str]]:
        """Return list of (command_name, price_atomic, description)."""
        async with aiosqlite.connect(self._path) as db:
            async with db.execute(
                "SELECT command_name, price_atomic, description"
                " FROM guild_commands WHERE guild_id=? AND enabled=1",
                (guild_id,),
            ) as cursor:
                rows = await cursor.fetchall()
        return [(r[0], r[1], r[2]) for r in rows]

    # ---- marketplace ------------------------------------------------------

    async def create_service(self, service: MarketplaceService) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO marketplace_services (
                    service_id, guild_id, lister_id, name, description,
                    url, price_atomic, wallet, verified, verified_by,
                    enabled, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    service.service_id,
                    service.guild_id,
                    service.lister_id,
                    service.name,
                    service.description,
                    service.url,
                    service.price_atomic,
                    service.wallet,
                    1 if service.verified else 0,
                    service.verified_by,
                    1 if service.enabled else 0,
                    service.created_at.isoformat(),
                ),
            )
            await db.commit()

    async def get_service(self, service_id: str) -> MarketplaceService | None:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM marketplace_services WHERE service_id = ?", (service_id,)
            ) as cursor:
                row = await cursor.fetchone()
        return _row_to_service(row) if row is not None else None

    async def get_service_by_name(self, guild_id: str, name: str) -> MarketplaceService | None:
        """Case-insensitive lookup, enabled listings only (any verify state)."""
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM marketplace_services"
                " WHERE guild_id = ? AND lower(name) = lower(?) AND enabled = 1",
                (guild_id, name),
            ) as cursor:
                row = await cursor.fetchone()
        return _row_to_service(row) if row is not None else None

    async def verify_service(self, service_id: str, admin_id: str) -> bool:
        """Mark a listing verified. Returns False if the id does not exist."""
        async with aiosqlite.connect(self._path) as db:
            cursor = await db.execute(
                "UPDATE marketplace_services SET verified = 1, verified_by = ?"
                " WHERE service_id = ?",
                (admin_id, service_id),
            )
            await db.commit()
        return cursor.rowcount > 0

    async def list_services(
        self, guild_id: str, verified_only: bool = True
    ) -> list[MarketplaceService]:
        """Listings for a guild, newest first. The agent only sees verified ones."""
        sql = "SELECT * FROM marketplace_services WHERE guild_id = ? AND enabled = 1"
        if verified_only:
            sql += " AND verified = 1"
        sql += " ORDER BY created_at DESC"
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, (guild_id,)) as cursor:
                rows = await cursor.fetchall()
        return [_row_to_service(row) for row in rows]

    # ---- channel rail -----------------------------------------------------

    async def bump_meter(
        self,
        channel_id: str,
        subject: str,
        guild_id: str,
        user_id: str,
        cumulative_atomic: int,
        voucher_json: str,
    ) -> ChannelMeter:
        """Move a subject's cumulative forward and keep the newest voucher.

        Cumulative only ever grows, so a replayed or out-of-order voucher can
        never lower what the service is owed.
        """
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO channel_meters (
                    channel_id, subject, guild_id, user_id,
                    cumulative_atomic, settled_atomic, calls, voucher_json, updated_at
                ) VALUES (?,?,?,?,?,0,1,?,?)
                ON CONFLICT(channel_id, subject) DO UPDATE SET
                    cumulative_atomic =
                        MAX(excluded.cumulative_atomic, channel_meters.cumulative_atomic),
                    calls = channel_meters.calls + 1,
                    voucher_json = excluded.voucher_json,
                    guild_id = excluded.guild_id,
                    user_id = excluded.user_id,
                    updated_at = excluded.updated_at
                """,
                (channel_id, subject, guild_id, user_id, cumulative_atomic, voucher_json, now),
            )
            await db.commit()
        meter = await self.get_meter(channel_id, subject)
        assert meter is not None  # just written
        return meter

    async def seed_meter(
        self,
        channel_id: str,
        subject: str,
        guild_id: str,
        user_id: str,
        cumulative_atomic: int,
        settled_atomic: int,
    ) -> None:
        """Adopt on-chain truth for a subject without counting a call.

        Used when the contract says more has been settled than this store knows,
        which is what a fresh service instance sees after losing its database. The
        chain is the floor, so the service can never re-collect what it already
        collected or issue a voucher the contract would reject as stale.
        """
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO channel_meters (
                    channel_id, subject, guild_id, user_id,
                    cumulative_atomic, settled_atomic, calls, voucher_json, updated_at
                ) VALUES (?,?,?,?,?,?,0,'',?)
                ON CONFLICT(channel_id, subject) DO UPDATE SET
                    cumulative_atomic =
                        MAX(excluded.cumulative_atomic, channel_meters.cumulative_atomic),
                    settled_atomic =
                        MAX(excluded.settled_atomic, channel_meters.settled_atomic),
                    updated_at = excluded.updated_at
                """,
                (channel_id, subject, guild_id, user_id, cumulative_atomic, settled_atomic, now),
            )
            await db.commit()

    async def get_meter(self, channel_id: str, subject: str) -> ChannelMeter | None:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM channel_meters WHERE channel_id = ? AND subject = ?",
                (channel_id, subject),
            ) as cursor:
                row = await cursor.fetchone()
        return _row_to_meter(row) if row is not None else None

    async def unsettled_meters(self, channel_id: str) -> list[ChannelMeter]:
        """Subjects with earned but uncollected spend, biggest first."""
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM channel_meters WHERE channel_id = ?"
                " AND cumulative_atomic > settled_atomic"
                " ORDER BY (cumulative_atomic - settled_atomic) DESC",
                (channel_id,),
            ) as cursor:
                rows = await cursor.fetchall()
        return [_row_to_meter(row) for row in rows]

    async def all_meters(self, channel_id: str) -> list[ChannelMeter]:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM channel_meters WHERE channel_id = ? ORDER BY cumulative_atomic DESC",
                (channel_id,),
            ) as cursor:
                rows = await cursor.fetchall()
        return [_row_to_meter(row) for row in rows]

    async def mark_meters_settled(self, channel_id: str, settled: dict[str, int]) -> None:
        """Record what the chain now agrees each subject has paid."""
        async with aiosqlite.connect(self._path) as db:
            for subject, cumulative in settled.items():
                await db.execute(
                    "UPDATE channel_meters SET settled_atomic = ?, updated_at = ?"
                    " WHERE channel_id = ? AND subject = ?",
                    (cumulative, datetime.now(timezone.utc).isoformat(), channel_id, subject),
                )
            await db.commit()

    async def record_settlement(self, settlement: ChannelSettlement) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO channel_settlements (
                    settlement_id, channel_id, tx_hash, total_atomic,
                    subject_count, calls, block_number, gas_fee_atomic, settled_at
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    settlement.settlement_id,
                    settlement.channel_id,
                    settlement.tx_hash,
                    settlement.total_atomic,
                    settlement.subject_count,
                    settlement.calls,
                    settlement.block_number,
                    settlement.gas_fee_atomic,
                    settlement.settled_at.isoformat(),
                ),
            )
            await db.commit()

    async def recent_channel_settlements(self, limit: int = 10) -> list[ChannelSettlement]:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM channel_settlements ORDER BY settled_at DESC LIMIT ?", (limit,)
            ) as cursor:
                rows = await cursor.fetchall()
        return [_row_to_settlement(row) for row in rows]


def _row_to_meter(row: sqlite3.Row) -> ChannelMeter:
    return ChannelMeter(
        channel_id=row["channel_id"],
        subject=row["subject"],
        guild_id=row["guild_id"],
        user_id=row["user_id"],
        cumulative_atomic=int(row["cumulative_atomic"]),
        settled_atomic=int(row["settled_atomic"]),
        calls=int(row["calls"]),
        voucher_json=row["voucher_json"],
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _row_to_settlement(row: sqlite3.Row) -> ChannelSettlement:
    return ChannelSettlement(
        settlement_id=row["settlement_id"],
        channel_id=row["channel_id"],
        tx_hash=row["tx_hash"],
        total_atomic=int(row["total_atomic"]),
        subject_count=int(row["subject_count"]),
        calls=int(row["calls"]),
        block_number=int(row["block_number"]),
        gas_fee_atomic=int(row["gas_fee_atomic"]),
        settled_at=datetime.fromisoformat(row["settled_at"]),
    )


def _row_to_record(row: sqlite3.Row) -> PaymentRecord:
    return PaymentRecord(
        payment_id=row["payment_id"],
        guild_id=row["guild_id"],
        channel_id=row["channel_id"],
        user_id=row["user_id"],
        command_name=row["command_name"],
        command_args=json.loads(row["command_args"]),
        price_atomic=row["price_atomic"],
        pay_to=row["pay_to"],
        status=PaymentStatus(row["status"]),
        tx_hash=row["tx_hash"],
        payer_address=row["payer_address"],
        created_at=datetime.fromisoformat(row["created_at"]),
        paid_at=datetime.fromisoformat(row["paid_at"]) if row["paid_at"] else None,
        result=row["result"],
        interaction_token=row["interaction_token"],
        application_id=row["application_id"],
    )


def _row_to_service(row: sqlite3.Row) -> MarketplaceService:
    return MarketplaceService(
        service_id=row["service_id"],
        guild_id=row["guild_id"],
        lister_id=row["lister_id"],
        name=row["name"],
        description=row["description"],
        url=row["url"],
        price_atomic=row["price_atomic"],
        wallet=row["wallet"],
        verified=bool(row["verified"]),
        verified_by=row["verified_by"],
        enabled=bool(row["enabled"]),
        created_at=datetime.fromisoformat(row["created_at"]),
    )
