#!/usr/bin/env python3
"""Print a cron / launchd / systemd snippet that runs this agent on a schedule.

    make schedule                                   # cron, every 15 minutes, tools/run.py --once
    make schedule ARGS="--target launchd"           # macOS laptop
    make schedule ARGS="--target systemd --cadence hourly"
    make schedule ARGS="--command 'tools/run.py --confirmations' --cadence every-15-min"

Nothing is installed. The snippet is printed with absolute paths so it works from
a scheduler's bare environment; you paste it where the header line says.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.schedule import CADENCES, ScheduleError, render  # noqa: E402


def _agent_schedule() -> dict:
    """The ``schedule:`` block of config/agent.yaml, or {} if there is none yet."""
    try:
        from core.config import load_settings  # type: ignore
        block = load_settings().agent_get("schedule", {}) or {}
        return block if isinstance(block, dict) else {}
    except Exception:
        return {}


def _cadence_from_config(job: str | None) -> tuple[str | None, str | None]:
    """Return (job, cadence) from config, or (None, None). Values may be a cron
    string, a friendly name, or a mapping with ``cadence``/``cron``/``every``."""
    block = _agent_schedule()
    if not block:
        return None, None
    key = job or next(iter(block))
    value = block.get(key)
    if isinstance(value, dict):
        value = value.get("cadence") or value.get("cron") or value.get("every")
    return (key, str(value)) if value else (key, None)


def _default_slug() -> str:
    try:
        from core.config import load_settings  # type: ignore
        settings = load_settings()
        slug = getattr(getattr(settings, "agent", None), "slug", None)
        if slug:
            return str(slug)
    except Exception:  # config may not exist yet; the folder name is fine
        pass
    return ROOT.name


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--target", default="cron", choices=["cron", "crontab", "launchd", "systemd"],
                    help="which scheduler to print a snippet for (default: cron)")
    ap.add_argument("--cadence", default=None,
                    help=f"one of {', '.join(CADENCES)} or a 5-field cron expression "
                         "(default: the job's entry under schedule: in config/agent.yaml, "
                         "else every-15-min)")
    ap.add_argument("--job", default=None,
                    help="which schedule: entry of config/agent.yaml to use (default: the first)")
    ap.add_argument("--command", default="tools/run.py --once",
                    help="what to run, relative to the repo root (default: tools/run.py --once)")
    ap.add_argument("--slug", default=None, help="label for the job (default: the agent slug)")
    ap.add_argument("--all", action="store_true",
                    help="print one snippet per entry of schedule: in config/agent.yaml "
                         "(each entry may set command: and cadence:)")
    args = ap.parse_args(argv)
    if args.all:
        block = _agent_schedule()
        if not block:
            print("schedule: config/agent.yaml has no schedule: block yet", file=sys.stderr)
            return 2
        slug = args.slug or _default_slug()
        settings_obj = None
        try:
            from core.config import load_settings  # type: ignore
            settings_obj = load_settings()
        except Exception:
            pass
        for job, value in block.items():
            cad = value.get("cadence") or value.get("cron") or value.get("every") if isinstance(value, dict) else value
            # A job may take its hour/minute from another config value, so the
            # scheduler can never drift from the setting a hotel actually edits:
            #   dispute-digest: {command: tools/digest.py, hour_from: digest.hour}
            if isinstance(value, dict) and (value.get("hour_from") or value.get("at")):
                hour, minute = None, int(value.get("minute", 0) or 0)
                if value.get("at"):
                    hh, _, mm = str(value["at"]).partition(":")
                    hour, minute = int(hh), int(mm or 0)
                elif settings_obj is not None:
                    got = settings_obj.agent_get(str(value["hour_from"]), None)
                    hour = int(got) if got is not None else None
                if hour is not None:
                    cad = f"{minute} {hour} * * *"
            cmd = value.get("command") if isinstance(value, dict) else None
            cmd = cmd or (args.command if args.command != "tools/run.py --once" else f"tools/run.py --once --job {job}")
            try:
                print(f"# job: {job}  cadence: {cad}  (from config/agent.yaml schedule.{job})")
                print(render(args.target, command=cmd, cadence=str(cad or "every-15-min"),
                             slug=f"{slug}-{job}", root=ROOT))
            except ScheduleError as exc:
                print(f"schedule: {job}: {exc}", file=sys.stderr)
                return 2
        return 0
    cadence, source = args.cadence, "--cadence"
    if cadence is None:
        job, configured = _cadence_from_config(args.job)
        if configured:
            cadence, source = configured, f"config/agent.yaml schedule.{job}"
        else:
            cadence, source = "every-15-min", "built-in default (no schedule: block in config/agent.yaml)"
    try:
        print(f"# cadence: {cadence}  (from {source})")
        print(render(args.target, command=args.command, cadence=cadence,
                     slug=args.slug or _default_slug(), root=ROOT))
    except ScheduleError as exc:
        print(f"schedule: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
