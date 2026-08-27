"""Credit-usage daily-spend alert checker (cron script).

Reads the alert config written by the Credit Usage dashboard
(``<config_dir>/usage/credit_alert.json``) and today's per-turn credit rows
(``<config_dir>/usage/tokens/<YYYY-MM-DD>.jsonl``). If the alert is enabled and
today's cumulative credits have crossed ``threshold * ratio``, it sends ONE
dashboard notification per local day (deduped via a stamp file) so a
long-running day does not re-fire the banner every hour.

This file is the CANONICAL, git-tracked source of the checker. The gateway
route ``POST /api/apps/credit-usage/alert-schedule`` copies it into
``<config_dir>/crons/credit_usage_alert.py`` (cron scripts must live there) and
registers/removes the hourly Schedule job when the user toggles Enable in the
dashboard. Shipping it in the package makes it reboot-durable: a fresh machine
always has the script, and the job is (re)registered the next time the user
saves the alert config.

Notification delivery: ``ctx.notify`` is the unconditional, reliable path
(writes the dashboard bell on the fallback branch even with no Slack
configured); ``send_notification`` is attempted additionally for a
banner + sound but its failure is swallowed.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path


def _data_home() -> Path:
    # Mirror kiro_crew.config.paths.data_home default without importing the pkg,
    # because the cron sandbox runs this file in isolation.
    env = os.environ.get("KIROCREW_HOME")
    if env:
        return Path(env)
    return Path.home() / ".kiro" / "crew"


def _today_credits(tokens_dir: Path, today_iso: str) -> tuple[float, int]:
    total = 0.0
    turns = 0
    shard = tokens_dir / f"{today_iso}.jsonl"
    if not shard.exists():
        return 0.0, 0
    try:
        with shard.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(obj, dict) or obj.get("_type") != "tokens":
                    continue
                # The shard is named by the writer's local date, so every row in
                # it belongs to today's local day — no per-row tz math needed.
                try:
                    total += float(obj.get("credits", 0.0) or 0.0)
                except (TypeError, ValueError):
                    pass
                turns += 1
    except OSError:
        return 0.0, 0
    return total, turns


def run(ctx):
    home = _data_home()
    cfg_path = home / "usage" / "credit_alert.json"
    tokens_dir = home / "usage" / "tokens"
    stamp_path = home / "usage" / "credit_alert_last.json"

    # Read config; silent no-op when missing/disabled.
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(cfg, dict) or not cfg.get("enabled"):
        return
    try:
        threshold = float(cfg.get("threshold", 0.0) or 0.0)
        ratio = float(cfg.get("ratio", 0.8) or 0.8)
    except (TypeError, ValueError):
        return
    trigger = threshold * ratio
    if trigger <= 0:
        return

    today = datetime.now().astimezone().date().isoformat()
    credits, turns = _today_credits(tokens_dir, today)
    if credits < trigger:
        return

    # Dedupe: at most one alert per local day.
    try:
        last = json.loads(stamp_path.read_text(encoding="utf-8"))
        if isinstance(last, dict) and last.get("date") == today:
            return
    except (OSError, ValueError):
        pass

    msg = (
        f"\u26a0\ufe0f Credit usage alert: today's spend {credits:.1f} credits "
        f"crossed your {int(ratio * 100)}% trigger ({trigger:.0f} of {threshold:.0f}) "
        f"across {turns} turns."
    )
    # Unconditional reliable delivery (bell + Slack if configured).
    ctx.notify(msg)
    # Best-effort banner + sound; swallow governance/identity failures.
    try:
        ctx.call_tool(
            "kirocrew-core",
            "send_notification",
            {
                "title": "Credit usage alert",
                "body": msg,
                "priority": "critical",
                "group_key": "credit-usage-daily-alert",
            },
        )
    except Exception:  # noqa: BLE001 — banner is best-effort, notify already sent
        pass

    try:
        stamp_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        stamp_path.write_text(
            json.dumps(
                {
                    "date": today,
                    "credits": round(credits, 4),
                    "ts": datetime.now(timezone.utc).isoformat(),
                }
            ),
            encoding="utf-8",
        )
    except OSError:
        pass
