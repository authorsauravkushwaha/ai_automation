"""Communication Center — email drafting, templates, reminders, broadcasts.

All local-first: drafts are created as .eml/.txt files in allowed roots, not sent without explicit SMTP config + consent.
Templates and reminders are stored in the local DB.
"""

from __future__ import annotations

import re
import time
import uuid
from email.message import EmailMessage
from typing import Any

from hinata.automation.app_control import AutomationError
from hinata.database.database import Database

TEMPLATES: dict[str, str] = {
    "job_application": "Subject: Application for {position} at {company}\n\nDear Hiring Manager,\n\nI am writing to apply for the {position} role at {company}. {intro}\n\nKey highlights:\n{highlights}\n\nI have attached my resume and would love to discuss how I can contribute to {company}.\n\nWarm regards,\n{full_name}\n{email} | {phone}",
    "follow_up": "Subject: Following up — {subject}\n\nHi {name},\n\nJust following up on my previous email about {topic}. {body}\n\nLooking forward to your reply.\n\nBest,\n{full_name}",
    "meeting_request": "Subject: Meeting request: {topic}\n\nHi {name},\n\nI would like to schedule a meeting to discuss {topic} on {proposed_time}. {agenda}\n\nPlease let me know what works for you.\n\nBest regards,\n{full_name}",
    "thank_you": "Subject: Thank you — {subject}\n\nHi {name},\n\nThank you for {reason}. I really appreciated {detail}.\n\nLooking forward to staying in touch.\n\nWarmly,\n{full_name}",
    "cold_outreach": "Subject: Quick idea for {company}\n\nHi {name},\n\nI noticed {observation} at {company}. I help {audience} achieve {outcome} via {method}.\n\nWould you be open to a 15-min call next week?\n\nBest,\n{full_name}\n{email}",
}


class CommunicationCenter:
    def __init__(self, database: Database | None = None, files=None, vault=None) -> None:
        self.db = database
        self.files = files
        self.vault = vault
        if self.db:
            self._ensure_table()

    def _ensure_table(self) -> None:
        assert self.db
        with self.db.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS email_drafts (
                    draft_id TEXT PRIMARY KEY,
                    to_addr TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    body TEXT NOT NULL,
                    template TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'draft',
                    file_path TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                CREATE TABLE IF NOT EXISTS reminders (
                    reminder_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    message TEXT NOT NULL,
                    remind_at_epoch REAL NOT NULL,
                    repeat_interval REAL NOT NULL DEFAULT 0,
                    channel TEXT NOT NULL DEFAULT 'local',
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
            """)

    def draft_email(
        self,
        to: str,
        subject: str = "",
        body: str = "",
        *,
        template: str = "",
        template_vars: dict[str, str] | None = None,
        profile_name: str = "personal",
        save_path: str = "",
    ) -> dict[str, Any]:
        vars_filled = dict(template_vars or {})
        # Auto-fill from identity vault if available
        if self.vault or (self.db and template):
            try:
                from hinata.core.identity_vault import IdentityVault
                iv = IdentityVault(self.db) if self.db else None
                prof = iv.get_profile(profile_name) if iv else None
                if not prof:
                    prof = iv.get_default() if iv else None
                if prof:
                    for k, v in prof.fields.items():
                        vars_filled.setdefault(k, v)
                    # Ensure full_name alias
                    if "full_name" in prof.fields:
                        vars_filled.setdefault("full_name", prof.fields["full_name"])
            except Exception:
                pass

        if template:
            tpl = TEMPLATES.get(template.lower())
            if not tpl:
                raise AutomationError(f"Unknown template '{template}'. Available: {', '.join(TEMPLATES.keys())}")
            try:
                rendered = tpl.format(**vars_filled)
            except KeyError as exc:
                raise AutomationError(f"Template '{template}' needs variable {exc}; provided: {list(vars_filled.keys())}") from exc
            # First line is Subject, rest is body
            if "\n" in rendered:
                first_line, rest = rendered.split("\n", 1)
                if first_line.lower().startswith("subject:"):
                    subject = first_line[len("Subject:"):].strip() if not subject else subject
                    body = rest.strip() if not body else body + "\n\n" + rest.strip()
                else:
                    body = rendered
            else:
                body = rendered
            if not subject and "subject" in vars_filled:
                subject = vars_filled["subject"]

        if not to or not to.strip():
            raise AutomationError("Recipient 'to' address is required")
        if not subject and not body:
            raise AutomationError("Either subject or body is required")
        if len(subject) > 500 or len(body) > 20000:
            raise AutomationError("Subject/body exceeds limits")

        draft_id = uuid.uuid4().hex[:12]
        # Create .eml draft file if files manager available
        file_path = ""
        if self.files and save_path:
            dst = self.files.resolve(save_path, must_exist=False)
            if dst.exists():
                raise AutomationError(f"Draft path exists: {dst}")
            dst.parent.mkdir(parents=True, exist_ok=True)
            msg = EmailMessage()
            msg["From"] = vars_filled.get("email", "me@local.hinata")
            msg["To"] = to
            msg["Subject"] = subject or "(no subject)"
            msg.set_content(body)
            dst.write_text(msg.as_string(), encoding="utf-8")
            file_path = str(dst)
        elif self.files and not save_path:
            # Don't auto-pick path; leave file_path empty but store draft in DB
            pass

        if self.db:
            self.db.execute(
                "INSERT INTO email_drafts(draft_id, to_addr, subject, body, template, file_path) VALUES(?,?,?,?,?,?)",
                (draft_id, to, subject, body, template, file_path),
            )
        return {"draft_id": draft_id, "to": to, "subject": subject, "body_preview": body[:400], "template": template, "file_path": file_path}

    def list_drafts(self, limit: int = 20) -> dict[str, Any]:
        if not self.db:
            return {"drafts": []}
        rows = self.db.fetch_all("SELECT * FROM email_drafts ORDER BY created_at DESC LIMIT ?", (limit,))
        return {"drafts": rows, "count": len(rows)}

    def get_template(self, name: str) -> dict[str, Any]:
        tpl = TEMPLATES.get(name.lower())
        if not tpl:
            return {"found": False, "available": list(TEMPLATES.keys())}
        vars_needed = re.findall(r"\{(\w+)\}", tpl)
        return {"found": True, "template": name, "content": tpl, "variables": vars_needed}

    def create_reminder(self, title: str, message: str, when_epoch: float | None = None, when_str: str = "", repeat_seconds: int = 0, channel: str = "local") -> dict[str, Any]:
        if not title.strip():
            raise AutomationError("Reminder title is required")
        if when_epoch is None:
            # Parse simple human time: "in 2 hours", "tomorrow 3pm", "2026-12-01 10:00"
            epoch = self._parse_when(when_str) if when_str else time.time() + 3600
        else:
            epoch = float(when_epoch)
        if epoch < time.time() - 60:
            raise AutomationError("Reminder time is in the past")
        rid = uuid.uuid4().hex[:12]
        if self.db:
            self.db.execute(
                "INSERT INTO reminders(reminder_id, title, message, remind_at_epoch, repeat_interval, channel) VALUES(?,?,?,?,?,?)",
                (rid, title.strip()[:160], message.strip()[:1000], epoch, float(repeat_seconds), channel),
            )
        return {"reminder_id": rid, "title": title, "remind_at": epoch, "remind_at_human": time.ctime(epoch), "message": message[:200]}

    _WEEKDAYS = {
        "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1,
        "wednesday": 2, "wed": 2, "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
        "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
    }

    @staticmethod
    def _clock(hr: int, mn: int, ap: str) -> tuple[int, int] | None:
        """Normalise a 12/24-hour clock pair; ``None`` when out of range."""
        if ap == "pm" and hr < 12:
            hr += 12
        if ap == "am" and hr == 12:
            hr = 0
        if not (0 <= hr <= 23 and 0 <= mn <= 59):
            return None
        return hr, mn

    def _parse_when(self, s: str) -> float:
        """Parse a human time expression into a local epoch timestamp.

        Supported (case-insensitive):

        * ``in 2 hours`` / ``in 30 minutes`` / ``in 3 days`` / ``in 1 week``
        * ``tomorrow 9am`` / ``tomorrow`` / ``today 18:30`` / ``tonight``
        * ``next monday 10am`` / ``monday 14:00`` (next occurrence)
        * ``friday 17:00``
        * ``2026-12-01 10:00`` / ``2026-12-01`` / ``01/12/2026 10:00``
        """
        s = s.strip().lower()
        now = time.time()
        # in X hours/minutes/days
        m = re.match(r"in\s+(\d+)\s*(minutes?|mins?|hours?|hrs?|days?|weeks?)\s*$", s)
        if m:
            n = int(m.group(1))
            unit = m.group(2)
            mult = 60 if "min" in unit else 3600 if "hour" in unit or "hr" in unit else 86400 if "day" in unit else 604800
            return now + n * mult

        # tomorrow [time] | tomorrow | today [time] | tonight
        m = re.match(
            r"(tomorrow|today|tonight)\s*(?:at\s*)?(?:(\d{1,2})(?::(\d{2}))?\s*(am|pm)?)?\s*$",
            s,
        )
        if m:
            word = m.group(1)
            hr = int(m.group(2)) if m.group(2) else None
            mn = int(m.group(3) or 0)
            ap = m.group(4)
            if word == "tomorrow" or hr is not None:
                base = time.localtime(now + 86400) if word == "tomorrow" else time.localtime(now)
                if hr is None:
                    hr, mn = 9, 0  # default morning start
                clock = self._clock(hr, mn, ap or "")
                if clock is None:
                    raise AutomationError(f"Invalid time in '{s}'")
                epoch = time.mktime((base.tm_year, base.tm_mon, base.tm_mday, clock[0], clock[1], 0, 0, 0, -1))
                # "today 9am" said in the afternoon means *tomorrow* 9am -- the
                # next time that clock reading actually occurs. Returning the
                # past instant made create_reminder fail with a confusing
                # "time is in the past" error for a perfectly reasonable request.
                if word != "tomorrow" and epoch <= now:
                    epoch += 86400
                return epoch
            # today / tonight without an explicit hour
            base = time.localtime(now)
            clock = self._clock(21, 0, "") if word == "tonight" else self._clock(9, 0, "")
            assert clock is not None
            candidate = time.mktime((base.tm_year, base.tm_mon, base.tm_mday, clock[0], clock[1], 0, 0, 0, -1))
            return candidate if candidate > now else candidate + 86400

        # next <weekday> [time] | <weekday> [time]
        m = re.match(
            r"(?:next\s+|this\s+|on\s+)?(" + "|".join(self._WEEKDAYS) + r")\s*(?:at\s*)?(?:(\d{1,2})(?::(\d{2}))?\s*(am|pm)?)?\s*$",
            s,
        )
        if m:
            target = self._WEEKDAYS[m.group(1)]
            hr = int(m.group(2)) if m.group(2) else 9
            mn = int(m.group(3) or 0)
            clock = self._clock(hr, mn, m.group(4) or "")
            if clock is None:
                raise AutomationError(f"Invalid time in '{s}'")
            local = time.localtime(now)
            days_ahead = (target - local.tm_wday) % 7
            if days_ahead == 0:
                days_ahead = 7  # "monday" always means the next Monday
            base = time.localtime(now + days_ahead * 86400)
            return time.mktime((base.tm_year, base.tm_mon, base.tm_mday, clock[0], clock[1], 0, 0, 0, -1))

        # Try ISO/absolute datetime. The 'T' separator is the standard ISO-8601
        # spelling and ``strptime`` handles it through the literal, so it needs no
        # special case here; a trailing 'Z' (UTC) is normalised first.
        iso = s[:-1].strip() if s.endswith("z") else s
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
            try:
                return time.mktime(time.strptime(iso, fmt))
            except ValueError:
                continue
        raise AutomationError(
            f"Could not parse time '{s}': try 'in 2 hours', 'tomorrow 3pm', 'next monday 10am', or '2026-12-01 10:00'"
        )

    def list_reminders(self, limit: int = 20, *, pending_only: bool = True) -> dict[str, Any]:
        """List reminders, newest-due first.

        Only ``pending`` reminders are returned by default: once a reminder has
        fired it is marked ``done`` (or advanced if it repeats), and showing it
        as though it were still waiting would be a lie.
        """
        if not self.db:
            return {"reminders": []}
        if pending_only:
            rows = self.db.fetch_all(
                "SELECT * FROM reminders WHERE status = 'pending' ORDER BY remind_at_epoch ASC LIMIT ?",
                (limit,),
            )
        else:
            rows = self.db.fetch_all(
                "SELECT * FROM reminders ORDER BY remind_at_epoch ASC LIMIT ?", (limit,)
            )
        return {"reminders": rows, "count": len(rows)}

    def due_reminders(self, *, now_epoch: float | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Reminders whose time has arrived and that have not yet been fired.

        Nothing else in the codebase read this table, so a reminder was written,
        listed forever, and never actually went off. This is the query that makes
        the feature real; :meth:`fire_due_reminders` consumes it.
        """
        if not self.db:
            return []
        now = time.time() if now_epoch is None else float(now_epoch)
        return self.db.fetch_all(
            "SELECT * FROM reminders WHERE status = 'pending' AND remind_at_epoch <= ? "
            "ORDER BY remind_at_epoch ASC LIMIT ?",
            (now, limit),
        )

    def fire_due_reminders(self, *, now_epoch: float | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Claim every reminder that is due; repeating ones advance, others finish.

        Returns the reminders that fired. A repeating reminder moves to its next
        occurrence (skipping past anything already missed, so a reminder that was
        due while HINATA was closed does not fire in a burst); a one-shot
        reminder is marked ``done``.
        """
        if self.db is None:
            return []
        now = time.time() if now_epoch is None else float(now_epoch)
        fired: list[dict[str, Any]] = []
        for row in self.due_reminders(now_epoch=now, limit=limit):
            interval = float(row["repeat_interval"] or 0)
            if interval > 0:
                next_at = float(row["remind_at_epoch"])
                while next_at <= now:
                    next_at += interval
                self.db.execute(
                    "UPDATE reminders SET remind_at_epoch = ?, status = 'pending' WHERE reminder_id = ?",
                    (next_at, row["reminder_id"]),
                )
            else:
                self.db.execute(
                    "UPDATE reminders SET status = 'done' WHERE reminder_id = ?",
                    (row["reminder_id"],),
                )
            fired.append(dict(row))
        return fired

    def broadcast(self, message: str, channels: list[str] | None = None) -> dict[str, Any]:
        """Mock broadcast — writes a notification file and returns what would be sent."""
        chans = channels or ["local", "clipboard"]
        msg = message.strip()
        if not msg:
            raise AutomationError("Broadcast message cannot be empty")
        if len(msg) > 5000:
            raise AutomationError("Broadcast too long")
        # Write to clipboard if possible (via DesktopCockpit) would be done by orchestrator; here just mock
        out = {"message": msg[:200], "channels": chans, "would_send": True, "note": "Local draft only — connect SMTP/Slack/Telegram in config to send for real."}
        if self.files:
            try:
                dst = self.files.resolve(f"broadcast_{int(time.time())}.txt", must_exist=False)
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text(f"[{time.ctime()}] {msg}", encoding="utf-8")
                out["local_file"] = str(dst)
            except Exception:
                pass
        return out
