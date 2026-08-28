"""core.schedule — generate the scheduler snippet for this machine.

Running the agent by hand proves it works. Running it on a schedule is what makes
it useful. Three ways, depending on the box:

``cron``     Linux and macOS. Simplest thing that works.
``launchd``  macOS, and the only option that survives a laptop sleeping and waking.
``systemd``  Linux servers. A ``.service`` plus a ``.timer``.

    from core.schedule import render
    print(render("cron", command="tools/run.py --once", cadence="*/15 * * * *"))

Cadence accepts a cron expression (``*/15 * * * *``) or a friendly name
(``every-15-min``, ``hourly``, ``nightly``, ``weekly``), so a hotel does not have
to remember cron syntax. ``tools/schedule.py`` in each repo wraps this with an
argparse CLI, and ``scheduler/`` holds ready-made example files.
"""

from __future__ import annotations

import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

from core.config import repo_root

#: friendly cadence names -> (cron expression, calendar interval for launchd)
CADENCES: dict[str, tuple[str, dict]] = {
    "every-5-min": ("*/5 * * * *", {"Minute": 5}),
    "every-10-min": ("*/10 * * * *", {"Minute": 10}),
    "every-15-min": ("*/15 * * * *", {"Minute": 15}),
    "every-30-min": ("*/30 * * * *", {"Minute": 30}),
    "hourly": ("0 * * * *", {"Minute": 0}),
    "every-4-hours": ("0 */4 * * *", {"Minute": 0}),
    "nightly": ("0 2 * * *", {"Hour": 2, "Minute": 0}),
    "morning": ("0 7 * * *", {"Hour": 7, "Minute": 0}),
    "weekly": ("0 3 * * 1", {"Weekday": 1, "Hour": 3, "Minute": 0}),
    "monthly": ("0 6 1 * *", {"Day": 1, "Hour": 6, "Minute": 0}),
}


class ScheduleError(ValueError):
    """Raised for an unusable cadence or target."""


@dataclass
class Plan:
    """A resolved schedule: what to run, how often, and where from."""

    command: str
    cadence: str
    cron: str
    label: str
    root: Path
    python: str

    @property
    def full_command(self) -> str:
        """The command with an absolute interpreter and an absolute working directory."""
        return f"cd {shlex.quote(str(self.root))} && {shlex.quote(self.python)} {self.command}"


def resolve_cadence(cadence: str) -> str:
    """Friendly name or cron expression in, cron expression out."""
    if cadence in CADENCES:
        return CADENCES[cadence][0]
    if re.fullmatch(r"[\d*/,\-]+(\s+[\d*/,\-A-Za-z]+){4}", cadence.strip()):
        return cadence.strip()
    raise ScheduleError(
        f"'{cadence}' is neither a cron expression nor one of: {', '.join(CADENCES)}")


def make_plan(command: str, cadence: str = "every-15-min", *, slug: str = "hotel-agent",
              root: Path | None = None, python: str | None = None) -> Plan:
    """Work out the absolute paths so the snippet works from cron's bare environment."""
    root = root or repo_root()
    venv_python = root / ".venv" / "bin" / "python"
    return Plan(command=command, cadence=cadence, cron=resolve_cadence(cadence),
                label=slug, root=root,
                python=python or (str(venv_python) if venv_python.exists()
                                  else sys.executable))


# --------------------------------------------------------------------------
# renderers
# --------------------------------------------------------------------------
def render_cron(plan: Plan) -> str:
    """A crontab line. Install with ``crontab -e``."""
    log = plan.root / "data" / "logs" / "cron.log"
    return "\n".join([
        f"# {plan.label}: {plan.command} ({plan.cadence})",
        "# Install with:  crontab -e     Check with:  crontab -l",
        "# cron runs with a bare environment, so the paths below are absolute.",
        f"{plan.cron} {plan.full_command} >> {log} 2>&1",
        "",
    ])


def _cron_to_launchd(cron: str) -> dict:
    """Best-effort map of a 5-field cron line to launchd's calendar dict.

    ``*/N * * * *`` -> every N minutes; ``M H * * *`` -> daily at H:M;
    ``M H * * D`` -> weekly (D 0-6, Sunday = 0). Anything else falls back to a
    daily 02:00 run rather than silently doing nothing.
    """
    try:
        minute, hour, _dom, _mon, dow = cron.split()
    except ValueError:
        return {"Hour": 2, "Minute": 0}
    if minute.startswith("*/") and hour == "*":
        return {"Minute": int(minute[2:])}
    if hour.startswith("*/") and minute.isdigit():
        return {"Minute": int(minute)}  # hourly-ish: launchd StartInterval of 60 min
    if minute.isdigit() and hour.isdigit():
        out = {"Hour": int(hour), "Minute": int(minute)}
        if dow.isdigit():
            out["Weekday"] = int(dow)
        if _dom.isdigit():
            out["Day"] = int(_dom)
        return out
    return {"Hour": 2, "Minute": 0}


def render_launchd(plan: Plan) -> str:
    """A launchd plist. macOS only, and the right answer on a laptop."""
    interval = CADENCES.get(plan.cadence, (None, {}))[1]
    if not interval:
        interval = _cron_to_launchd(plan.cron)
    if interval and "Hour" not in interval and "Weekday" not in interval:
        minutes = interval.get("Minute", 15)
        schedule = f"    <key>StartInterval</key>\n    <integer>{minutes * 60}</integer>"
    else:
        rows = "".join(f"\n      <key>{k}</key><integer>{v}</integer>"
                       for k, v in (interval or {"Hour": 2, "Minute": 0}).items())
        schedule = ("    <key>StartCalendarInterval</key>\n    <dict>"
                    f"{rows}\n    </dict>")
    logs = plan.root / "data" / "logs"
    program = ["/bin/sh", "-c", plan.full_command]
    args = "".join(f"\n      <string>{a}</string>" for a in program)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<!-- {plan.label}: {plan.command} ({plan.cadence})
     Save as ~/Library/LaunchAgents/ai.th1.{plan.label}.plist then:
       launchctl load  ~/Library/LaunchAgents/ai.th1.{plan.label}.plist
       launchctl list | grep {plan.label}
     Unload with launchctl unload <same path>. -->
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>ai.th1.{plan.label}</string>
    <key>ProgramArguments</key>
    <array>{args}
    </array>
{schedule}
    <key>WorkingDirectory</key>
    <string>{plan.root}</string>
    <key>StandardOutPath</key>
    <string>{logs / 'launchd.log'}</string>
    <key>StandardErrorPath</key>
    <string>{logs / 'launchd.err'}</string>
    <key>RunAtLoad</key>
    <false/>
  </dict>
</plist>
"""


def render_systemd(plan: Plan) -> str:
    """A ``.service`` and a ``.timer``, ready for ``/etc/systemd/system``."""
    service = f"""# {plan.label}.service — save to /etc/systemd/system/{plan.label}.service
[Unit]
Description={plan.label}: {plan.command}
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory={plan.root}
ExecStart=/bin/sh -c '{plan.full_command}'
# Run as the user that owns the repo, never root.
User=%i
# The agent writes only inside its own folder.
ProtectSystem=strict
ReadWritePaths={plan.root / 'data'}
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
"""
    timer = f"""# {plan.label}.timer — save to /etc/systemd/system/{plan.label}.timer
# Then:  sudo systemctl daemon-reload
#        sudo systemctl enable --now {plan.label}.timer
#        systemctl list-timers | grep {plan.label}
[Unit]
Description=Run {plan.label} ({plan.cadence})

[Timer]
OnCalendar={_cron_to_oncalendar(plan.cron)}
Persistent=true
RandomizedDelaySec=60

[Install]
WantedBy=timers.target
"""
    return service + "\n" + timer


def _cron_to_oncalendar(cron: str) -> str:
    """Translate the cron expressions we generate into systemd OnCalendar syntax."""
    minute, hour, _dom, _mon, dow = (cron.split() + ["*"] * 5)[:5]
    if minute.startswith("*/"):
        return f"*:0/{minute[2:]}"
    if hour == "*":
        return f"*:{int(minute):02d}"
    if hour.startswith("*/"):
        return f"*-*-* 0/{hour[2:]}:{int(minute):02d}:00"
    day = {"1": "Mon", "2": "Tue", "3": "Wed", "4": "Thu", "5": "Fri",
           "6": "Sat", "0": "Sun", "7": "Sun"}.get(dow, "")
    prefix = f"{day} " if day else ""
    dom = f"{int(_dom):02d}" if _dom.isdigit() else "*"
    return f"{prefix}*-*-{dom} {int(hour):02d}:{int(minute):02d}:00"


RENDERERS = {"cron": render_cron, "crontab": render_cron, "launchd": render_launchd,
             "systemd": render_systemd}


def render(target: str, *, command: str, cadence: str = "every-15-min",
           slug: str = "hotel-agent", root: Path | None = None) -> str:
    """Render the snippet for ``target`` (``cron`` | ``launchd`` | ``systemd``)."""
    renderer = RENDERERS.get(target.lower())
    if renderer is None:
        raise ScheduleError(
            f"unknown scheduler '{target}'. Use cron, launchd or systemd.")
    return renderer(make_plan(command, cadence, slug=slug, root=root))
