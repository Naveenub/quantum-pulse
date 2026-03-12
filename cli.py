#!/usr/bin/env python3
"""
QUANTUM-PULSE :: cli.py
=========================
Operator command-line interface using Typer + Rich.

Commands
────────
  qp seal       <file>         — seal a file into the vault
  qp unseal     <pulse-id>     — decrypt and write a pulse to stdout/file
  qp scan       <dir>          — scan a directory, seal all shards, print master ID
  qp rotate     <pulse-id>     — re-encrypt a single shard with current key
  qp list                      — list stored pulses
  qp info       <pulse-id>     — print PulseBlob metadata
  qp master     [ids...]       — build a MasterPulse from given pulse IDs
  qp keygen                    — generate a strong passphrase
  qp benchmark  [dir]          — run compression benchmark
  qp health                    — query /healthz and print report
  qp config                    — print active (redacted) configuration

Requires a running QUANTUM-PULSE server at QUANTUM_HOST:QUANTUM_PORT,
or can be used in --offline mode which runs the engine in-process.
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import uuid
from pathlib import Path

import msgpack as _mp
import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

app = typer.Typer(
    name="qp",
    help="QUANTUM-PULSE operator CLI",
    add_completion=True,
    rich_markup_mode="rich",
)
console = Console()

# ─────────────────────────────── helpers ──────────────────────────────────── #


def _get_engine(passphrase: str):
    """Create a QuantumEngine in-process (no server required for offline ops)."""
    sys.path.insert(0, str(Path(__file__).parent))
    from core.engine import QuantumEngine

    return QuantumEngine(passphrase=passphrase)


def _get_db(passphrase: str):
    sys.path.insert(0, str(Path(__file__).parent))
    from core.config import get_settings
    from core.db import PulseDB

    cfg = get_settings()
    return PulseDB(cfg.mongo_uri, cfg.mongo_db)


def _passphrase_prompt() -> str:
    return typer.prompt("Passphrase", hide_input=True)


# ─────────────────────────────── keygen ──────────────────────────────────── #


@app.command()
def keygen(
    words: int = typer.Option(6, "--words", "-w", help="Number of random segments"),
) -> None:
    """Generate a cryptographically strong passphrase."""
    from core.vault import QuantumVault

    phrase = QuantumVault.generate_passphrase(words)
    console.print(
        Panel(
            f"[bold green]{phrase}[/bold green]",
            title="Generated Passphrase",
            subtitle="Store this securely — it cannot be recovered",
        )
    )


# ─────────────────────────────── config ──────────────────────────────────── #


@app.command()
def config() -> None:
    """Print active (secrets-redacted) configuration."""
    from core.config import get_settings

    cfg = get_settings()
    table = Table(title="Active Configuration", show_header=True)
    table.add_column("Setting", style="cyan")
    table.add_column("Value", style="white")
    for k, v in cfg.display().items():
        table.add_row(k, str(v))
    console.print(table)


# ─────────────────────────────── seal ────────────────────────────────────── #


@app.command()
def seal(
    file_path: str = typer.Argument(..., help="File to seal"),
    passphrase: str = typer.Option("", "--passphrase", "-p"),
    tag: list[str] | None = typer.Option(None, "--tag", "-t", help="key=value tags"),
    output: str | None = typer.Option(None, "--output", "-o", help="Write pulse ID or .qp blob to file"),
    offline: bool = typer.Option(False, "--offline", help="Seal without MongoDB — saves blob to <file>.qp"),
) -> None:
    """Seal a file into the vault (in-process, no server required).

    With --offline: no MongoDB needed. Blob saved to <file>.qp for later unsealing.
    """
    if not passphrase:
        passphrase = _passphrase_prompt()

    path = Path(file_path)
    if not path.exists():
        console.print(f"[red]File not found: {file_path}[/red]")
        raise typer.Exit(1)

    tags = dict(t.split("=", 1) for t in (tag or []) if "=" in t)
    tags["filename"] = path.name

    async def _run():
        engine = _get_engine(passphrase)
        raw = path.read_bytes()
        payload = {"filename": path.name, "data": list(raw)}
        pulse_id = str(uuid.uuid4())

        with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console) as prog:
            t = prog.add_task("Sealing …", total=None)
            blob, meta = await engine.seal(payload, pulse_id=pulse_id, tags=tags)

            if offline:
                out_path = Path(output) if output else path.with_suffix(".qp")
                packed = _mp.packb({
                    "pulse_id": pulse_id,
                    "blob": base64.b64encode(blob).decode(),
                    "meta": meta.model_dump_json(),
                }, use_bin_type=True)
                out_path.write_bytes(packed)
                prog.update(t, description="Saved offline")
                console.print(
                    Panel(
                        f"[bold cyan]{pulse_id}[/bold cyan]\n"
                        f"Ratio: [green]{meta.stats.ratio:.2f}×[/green]  "
                        f"Saved: {out_path}  "
                        f"Size: {meta.stats.encrypted_bytes:,} B",
                        title="✅ Sealed (offline)",
                    )
                )
            else:
                db = _get_db(passphrase)
                await db.connect()
                prog.update(t, description="Saving to MongoDB …")
                backend = await db.save_pulse(pulse_id, blob, meta)
                console.print(
                    Panel(
                        f"[bold cyan]{pulse_id}[/bold cyan]\n"
                        f"Ratio: [green]{meta.stats.ratio:.2f}×[/green]  "
                        f"Backend: {backend}  "
                        f"Size: {meta.stats.encrypted_bytes:,} B",
                        title="✅ Sealed",
                    )
                )
                if output:
                    Path(output).write_text(pulse_id)

    asyncio.run(_run())


# ─────────────────────────────── unseal ──────────────────────────────────── #


@app.command()
def unseal(
    pulse_id: str = typer.Argument(..., help="Pulse ID to decrypt"),
    passphrase: str = typer.Option("", "--passphrase", "-p"),
    output: str | None = typer.Option(None, "--output", "-o", help="Output file path"),
) -> None:
    """Decrypt a pulse from the vault."""
    if not passphrase:
        passphrase = _passphrase_prompt()

    async def _run():
        engine = _get_engine(passphrase)
        db = _get_db(passphrase)
        await db.connect()

        with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console) as prog:
            prog.add_task("Decrypting …", total=None)
            blob, meta = await db.load_pulse(pulse_id)
            payload = await engine.unseal(blob, meta)

        if output:
            raw = (
                bytes(payload["data"])
                if isinstance(payload.get("data"), list)
                else json.dumps(payload).encode()
            )
            Path(output).write_bytes(raw)
            console.print(f"[green]Written to {output}[/green]")
        else:
            console.print_json(json.dumps(payload, default=str))

    asyncio.run(_run())


# ─────────────────────────────── list ────────────────────────────────────── #


@app.command(name="list")
def list_pulses(
    limit: int = typer.Option(20, "--limit", "-n"),
    parent: str | None = typer.Option(None, "--parent", "-p"),
) -> None:
    """List stored pulses."""

    async def _run():
        db = _get_db("")
        await db.connect()
        pulses = await db.list_pulses(parent_id=parent, limit=limit)

        table = Table(title=f"Pulses (showing {len(pulses)})")
        table.add_column("Pulse ID", style="cyan", no_wrap=True)
        table.add_column("Ratio", style="green", justify="right")
        table.add_column("Size", style="white", justify="right")
        table.add_column("Dict ID", style="dim")
        table.add_column("Created", style="dim")

        for p in pulses:
            stats = p.get("stats", {})
            table.add_row(
                p.get("pulse_id", "")[:16] + "…",
                f"{stats.get('ratio', 0):.2f}×",
                f"{stats.get('encrypted_bytes', 0):,} B",
                str(p.get("zstd_dict_id") or "-"),
                str(p.get("created_at", ""))[:19],
            )
        console.print(table)

    asyncio.run(_run())


# ─────────────────────────────── info ────────────────────────────────────── #


@app.command()
def info(
    pulse_id: str = typer.Argument(..., help="Pulse ID to inspect"),
) -> None:
    """Print full PulseBlob metadata for a pulse."""

    async def _run():
        db = _get_db("")
        await db.connect()
        _, meta = await db.load_pulse(pulse_id)
        console.print_json(meta.model_dump_json(indent=2))

    asyncio.run(_run())


# ─────────────────────────────── rotate ──────────────────────────────────── #


@app.command()
def rotate(
    pulse_id: str = typer.Argument(..., help="Pulse ID to rotate"),
    old_passphrase: str = typer.Option("", "--old", help="Old passphrase"),
    new_passphrase: str = typer.Option("", "--new", help="New passphrase"),
) -> None:
    """Atomically re-encrypt a single shard under a new passphrase."""
    if not old_passphrase:
        old_passphrase = typer.prompt("Old passphrase", hide_input=True)
    if not new_passphrase:
        new_passphrase = typer.prompt("New passphrase", hide_input=True)
        confirm = typer.prompt("Confirm new passphrase", hide_input=True)
        if new_passphrase != confirm:
            console.print("[red]Passphrases do not match[/red]")
            raise typer.Exit(1)

    async def _run():
        from core.vault import QuantumVault

        vault = QuantumVault(new_passphrase)
        db = _get_db(old_passphrase)
        await db.connect()
        blob, meta = await db.load_pulse(pulse_id)
        new_vk = await vault.unlock()
        new_blob, new_meta = await vault.rotate_shard(blob, meta, old_passphrase, new_vk)
        await db.update_pulse(pulse_id, new_blob, new_meta)
        console.print(f"[green]✅ Rotated[/green] {pulse_id[:16]}…")

    asyncio.run(_run())


# ─────────────────────────────── scan ────────────────────────────────────── #


@app.command()
def scan(
    root: str = typer.Argument(..., help="Directory to scan and seal"),
    passphrase: str = typer.Option("", "--passphrase", "-p"),
    depth: int = typer.Option(-1, "--depth", "-d"),
) -> None:
    """Scan a directory tree, shard by entropy, seal all manifests."""
    if not passphrase:
        passphrase = _passphrase_prompt()

    async def _run():
        from core.scanner import QuantumScanner

        engine = _get_engine(passphrase)
        db = _get_db(passphrase)
        await db.connect()
        scanner = QuantumScanner(root, max_depth=depth)
        master_id = str(uuid.uuid4())
        pairs = []

        # Bootstrap dict
        samples = await scanner.scan_samples(limit=200)
        if samples:
            await engine.bootstrap_dict(samples)

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TimeElapsedColumn(),
            console=console,
        ) as prog:
            t = prog.add_task("Scanning + sealing …", total=None)
            async for manifest in scanner.scan():
                pid = str(uuid.uuid4())
                blob, meta = await engine.seal(
                    manifest.model_dump(),
                    pulse_id=pid,
                    parent_id=master_id,
                    tags={"root": manifest.root_path},
                )
                await db.save_pulse(pid, blob, meta)
                pairs.append((blob, meta))
                prog.update(t, description=f"Sealed {len(pairs)} shards …")

        if pairs:
            from core.engine import QuantumEngine as QE

            master = QE.build_master_pulse(master_id, pairs)
            await db.save_master(master)
            console.print(
                Panel(
                    f"Master ID: [cyan]{master_id}[/cyan]\n"
                    f"Shards:    [green]{master.total_shards}[/green]\n"
                    f"Root hash: {master.merkle_root[:32]}…",
                    title="✅ Scan Complete",
                )
            )

    asyncio.run(_run())


# ─────────────────────────────── benchmark ───────────────────────────────── #


@app.command()
def benchmark(
    passphrase: str = typer.Option("", "--passphrase", "-p"),
    rows: int = typer.Option(200, "--rows", "-r"),
    shards: int = typer.Option(5, "--shards", "-s"),
) -> None:
    """Run a local compression + seal/unseal benchmark."""
    if not passphrase:
        passphrase = _passphrase_prompt()

    async def _run():
        import json as _json

        from core.engine import QuantumEngine

        engine = QuantumEngine(passphrase=passphrase)
        samples = [
            _json.dumps({"shard": i, "rows": list(range(rows))}).encode() for i in range(200)
        ]
        await engine.bootstrap_dict(samples)

        table = Table(title="Seal Benchmark")
        table.add_column("Shard")
        table.add_column("Original", justify="right")
        table.add_column("Encrypted", justify="right")
        table.add_column("Ratio", justify="right", style="green")
        table.add_column("ms", justify="right")

        for i in range(shards):
            payload = {"shard": i, "rows": [{"text": "x" * 100, "id": j} for j in range(rows)]}
            pid = str(uuid.uuid4())
            blob, meta = await engine.seal(payload, pulse_id=pid)
            table.add_row(
                str(i),
                f"{meta.stats.original_bytes:,}",
                f"{meta.stats.encrypted_bytes:,}",
                f"{meta.stats.ratio:.2f}×",
                f"{meta.stats.duration_ms:.0f}",
            )

        console.print(table)

    asyncio.run(_run())


# ─────────────────────────────── health ──────────────────────────────────── #


@app.command()
def health(
    host: str = typer.Option("http://127.0.0.1:8747", "--host"),
) -> None:
    """Query the running server's health endpoint."""
    try:
        import httpx

        resp = httpx.get(f"{host}/healthz/", timeout=5)
        data = resp.json()
        status = data.get("status", "?")
        color = "green" if status == "PASS" else "yellow" if status == "WARN" else "red"
        console.print(f"[{color}]Status: {status}[/{color}]  uptime={data.get('uptime_s')}s")

        table = Table()
        table.add_column("Check", style="cyan")
        table.add_column("Status", justify="center")
        table.add_column("Message")
        table.add_column("ms", justify="right")

        for c in data.get("checks", []):
            s = c["status"]
            color = "green" if s == "PASS" else "yellow" if s == "WARN" else "red"
            table.add_row(
                c["name"],
                f"[{color}]{s}[/{color}]",
                c.get("message", ""),
                f"{c.get('latency_ms', 0):.1f}",
            )
        console.print(table)
    except Exception as exc:
        console.print(f"[red]Health check failed: {exc}[/red]")
        raise typer.Exit(1) from exc


# ─────────────────────────────── audit ───────────────────────────────────── #


@app.command()
def audit(
    limit: int = typer.Option(20, "--limit", "-n"),
    event: str | None = typer.Option(None, "--event", "-e", help="Filter by event type"),
) -> None:
    """Print recent audit log entries."""

    async def _run():
        from core.audit import audit_logger

        records = await audit_logger.query_recent(limit=limit, event_type=event)
        table = Table(title=f"Audit Log (last {len(records)} entries)")
        table.add_column("Timestamp", style="dim", no_wrap=True)
        table.add_column("Event", style="cyan")
        table.add_column("Outcome", justify="center")
        table.add_column("Identity", style="dim")
        table.add_column("Pulse ID", style="dim")

        for r in records:
            outcome = r.get("outcome", "?")
            color = "green" if outcome == "success" else "red"
            pid = r.get("pulse_id") or "-"
            table.add_row(
                str(r.get("timestamp", ""))[:19],
                r.get("event_type", ""),
                f"[{color}]{outcome}[/{color}]",
                r.get("identity", ""),
                pid[:16] + ("…" if len(pid) > 16 else ""),
            )
        console.print(table)

    asyncio.run(_run())


# ─────────────────────────────── entry point ─────────────────────────────── #

if __name__ == "__main__":
    app()
