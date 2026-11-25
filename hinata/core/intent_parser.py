"""Fast deterministic commands plus strict parsing of local-LLM JSON plans."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from hinata.core.model_reply import extract_reply_object, normalize_plan, plain_text_reply
from hinata.core.types import ActionRequest
from hinata.security.action_validator import ActionValidator, ValidationError

_ADDRESS_PREFIX = re.compile(r"^hinata(?:[,!:]\s*|\s+)", re.I)
_BROWSER_FIRST_SEARCH = re.compile(
    r"(?:please\s+)?(search|google|bing)\s+(?:in|on|using)\s+(.+?)\s+(?:for|about)\s+(.+)", re.I
)


class PlanError(ValueError):
    pass


class IntentParser:
    def __init__(self, validator: ActionValidator) -> None:
        self.validator = validator

    def parse(self, text: str) -> ActionRequest | None:
        stripped = _ADDRESS_PREFIX.sub("", text.strip())
        # User-name capture and common internet/app/code requests are deterministic.
        match = re.fullmatch(r"(?:my name is|our names are|we are|we're)\s+(.+)", stripped, re.IGNORECASE)
        if match:
            return self._action("set_owner_name", {"name": match.group(1).strip()}, text)
        if re.fullmatch(r"(?:what is|what's|tell me)\s+(?:my|our)\s+name", stripped, re.IGNORECASE):
            return self._action("owner_name", {}, text)
        browser_search = _BROWSER_FIRST_SEARCH.fullmatch(stripped)
        if browser_search:
            verb, browser, query = browser_search.groups()
            engine = "duckduckgo" if verb.casefold() == "search" else verb.casefold()
            if verb.casefold() == "search" and browser.casefold() in {"google", "bing", "duckduckgo"}:
                engine, browser = browser.casefold(), "default"
            return self._action("browser_search", {"query": query, "browser": browser, "engine": engine}, text)
        browser_search = re.fullmatch(
            r"(?:please\s+)?(search|google|bing)\s+(?:for\s+)?(.+?)\s+(?:in|on|using)\s+"
            r"((?:my |default )?browser|(?:google )?chrome|(?:mozilla )?firefox|(?:microsoft )?edge|"
            r"brave(?: browser)?|opera(?: browser)?|vivaldi(?: browser)?)\s*[.!?]?", stripped, re.I
        )
        if browser_search:
            verb, query, browser = browser_search.groups()
            engine = "duckduckgo" if verb.casefold() == "search" else verb.casefold()
            return self._action("browser_search", {"query": query, "browser": browser, "engine": engine}, text)
        match = re.fullmatch(r"(?:please\s+)?scroll\s+(up|down)(?:\s+(\d{1,2}|one|two|three|four|five)(?:\s+(?:notches?|steps?))?)?[.!?]?", stripped, re.I)
        if match:
            direction, amount = match.groups()
            counts = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
            steps = int(amount) if amount and amount.isdecimal() else counts.get((amount or "").casefold(), 3)
            return self._action("scroll_window", {"direction": direction.casefold(), "steps": steps}, text)
        match = re.fullmatch(r"google\s+(.+)", stripped, re.IGNORECASE)
        if match and match.group(1).strip():
            return self._action("web_search", {"query": match.group(1).strip()}, text)
        match = re.fullmatch(r"search\s+(?:about|for)\s+(.+)", stripped, re.IGNORECASE)
        if match and match.group(1).strip() and not match.group(1).strip().lower().startswith("memory "):
            return self._action("web_search", {"query": match.group(1).strip()}, text)
        match = re.fullmatch(r"search\s+(?:the )?(?:internet|web|online)(?:\s+(?:for|about))?\s+(.+)", stripped, re.IGNORECASE)
        if match:
            return self._action("web_search", {"query": match.group(1).strip()}, text)
        match = re.fullmatch(r"fetch\s+(?:webpage|website|url)\s+(.+)", stripped, re.IGNORECASE)
        if match:
            return self._action("fetch_webpage", {"url": match.group(1).strip()}, text)
        # Automatically ground clearly factual/current research questions in live sources.
        # Casual chat and ordinary explanations still stay on the local chat path.
        match = re.fullmatch(
            r"(?:who\s+is|who\s+was|tell\s+me\s+about|look\s+up|find\s+out\s+about|"
            r"what\s+are\s+the\s+latest|what\s+is\s+the\s+latest|latest\s+news\s+(?:about|on)|"
            r"recent\s+news\s+(?:about|on)|current\s+(?:price|status|news|version)\s+(?:of|for)|"
            r"biography\s+of|career\s+of|works\s+of)\s+(.+)",
            stripped,
            re.IGNORECASE,
        )
        if match:
            return self._action("web_search", {"query": match.group(1).strip()}, text)
        if re.fullmatch(r"(?:list|show)\s+(?:my\s+)?installed\s+apps", stripped, re.IGNORECASE):
            return self._action("list_installed_apps", {}, text)
        match = re.fullmatch(r"(?:write|create|generate)\s+(?:complete\s+)?(?:code|program|script)\s+(?:in\s+)?([a-zA-Z0-9+#.-]+)\s*(?::|for|to)?\s*(.*)", stripped, re.IGNORECASE)
        if match:
            language, task = match.group(1), match.group(2).strip()
            task = task or f"Create a complete starter program in {language}."
            return self._action("write_code", {"language": language, "task": task}, text)
        # J.A.R.V.I.S. tactical & desktop cockpit natural commands
        if re.fullmatch(
            r"(?:executive|tactical|morning|jarvis|status)\s+(?:briefing|report)|brief\s+me",
            stripped,
            re.IGNORECASE,
        ):
            return self._action("executive_briefing", {}, text)
        match = re.fullmatch(
            r"list\s+(?:open\s+)?windows(?:\s+matching\s+(.+))?", stripped, re.IGNORECASE
        )
        if match:
            w_args: dict[str, Any] = {}
            if match.group(1):
                w_args["filter"] = match.group(1).strip()
            return self._action("list_windows", w_args, text)
        match = re.fullmatch(r"focus\s+window\s+(.+)", stripped, re.IGNORECASE)
        if match:
            return self._action("focus_window", {"title": match.group(1).strip()}, text)
        match = re.fullmatch(
            r"(?:snap|arrange)\s+window\s+(.+?)\s+(left|right|maximize|minimize|restore)",
            stripped,
            re.IGNORECASE,
        )
        if match:
            return self._action(
                "snap_window",
                {"title": match.group(1).strip(), "layout": match.group(2).lower()},
                text,
            )
        match = re.fullmatch(
            r"(?:maximize|minimize|restore)\s+window\s+(.+)", stripped, re.IGNORECASE
        )
        if match:
            verb = stripped.split()[0].lower()
            return self._action(
                "snap_window", {"title": match.group(1).strip(), "layout": verb}, text
            )
        match = re.fullmatch(
            r"send\s+(?:hotkey|shortcut)\s+(\w+)(?:\s+to\s+(.+))?", stripped, re.IGNORECASE
        )
        if match:
            hk_args: dict[str, Any] = {"shortcut": match.group(1).lower()}
            if match.group(2):
                hk_args["target_window"] = match.group(2).strip()
            return self._action("send_hotkey", hk_args, text)
        if re.fullmatch(r"read\s+clipboard", stripped, re.IGNORECASE):
            return self._action("read_clipboard", {}, text)
        match = re.fullmatch(r"(?:copy\s+to|write)\s+clipboard\s+(.+)", stripped, re.IGNORECASE)
        if match:
            return self._action(
                "write_clipboard",
                {"text": self._strip_matching_quotes(match.group(1).strip())},
                text,
            )
        match = re.fullmatch(
            r"(?:capture|screenshot)\s+screen\s+(?:to\s+)?(\S+)(?:\s+and\s+analyze\s+(.+))?",
            stripped,
            re.IGNORECASE,
        )
        if match:
            sc_args: dict[str, Any] = {"path": match.group(1).strip()}
            if match.group(2):
                sc_args["analyze_prompt"] = match.group(2).strip()
            return self._action("capture_screen", sc_args, text)
        match = re.fullmatch(
            r"list\s+(?:running\s+|top\s+)?processes(?:\s+for\s+(.+))?", stripped, re.IGNORECASE
        )
        if match:
            p_args: dict[str, Any] = {}
            if match.group(1):
                p_args["filter"] = match.group(1).strip()
            return self._action("list_processes", p_args, text)
        match = re.fullmatch(
            r"(lock|sleep|hibernate)\s+(?:workstation|pc|computer)", stripped, re.IGNORECASE
        )
        if match:
            return self._action("power_control", {"action": match.group(1).lower()}, text)

        match = re.fullmatch(r"(?:inspect|analyze) (?:python|code)(?: file)?\s+(.+)", stripped, re.IGNORECASE)
        if match:
            return self._action("code_inspect", {"path": match.group(1)}, text)
        if re.fullmatch(r"inspect (?:repository|workspace) imports", stripped, re.IGNORECASE):
            return self._action("inspect_repository_imports", {}, text)
        if re.fullmatch(r"(?:ai|model|jarvis|lenovo|loq)\s+fleet(?:\s+profile)?", stripped, re.IGNORECASE):
            return self._action("ai_fleet_profile", {}, text)
        match = re.fullmatch(r"(?:generate|create|make)\s+image\s+(.+?)\s+(?:to|at)\s+(.+)", stripped, re.IGNORECASE)
        if match:
            return self._action("generate_image", {"prompt": match.group(1), "output_path": match.group(2)}, text)
        match = re.fullmatch(r"(?:build|create)\s+project\s+(.+?)\s+(?:in|at)\s+(.+)", stripped, re.IGNORECASE)
        if match:
            return self._action("build_project", {"specification": match.group(1), "project_dir": match.group(2)}, text)
        if re.fullmatch(r"(?:hardware|gpu|device)\s+profile", stripped, re.IGNORECASE):
            return self._action("hardware_profile", {}, text)
        match = re.fullmatch(r"hybrid search(?: knowledge)?(?: for)?\s+(.+)", stripped, re.IGNORECASE)
        if match:
            return self._action("knowledge_hybrid_search", {"query": match.group(1)}, text)
        match = re.fullmatch(r"list (?:directory|folder)\s+(.+)", stripped, re.IGNORECASE)
        if match:
            return self._action("list_directory", {"path": match.group(1)}, text)
        match = re.fullmatch(r"search file content(?: for)?\s+(.+?)(?:\s+in\s+(.+))?", stripped, re.IGNORECASE)
        if match:
            c_args: dict[str, Any] = {"query": match.group(1).strip()}
            if match.group(2):
                c_args["root"] = match.group(2).strip()
            return self._action("search_file_content", c_args, text)
        match = re.fullmatch(r"compare files\s+(\S+)\s+(?:and|with)\s+(\S+)", stripped, re.IGNORECASE)
        if match:
            return self._action("compare_files", {"first": match.group(1), "second": match.group(2)}, text)
        if re.fullmatch(r"workspace (?:list|inventory)", stripped, re.IGNORECASE):
            return self._action("workspace_list", {}, text)
        match = re.fullmatch(r"workspace read\s+(.+)", stripped, re.IGNORECASE)
        if match:
            return self._action("workspace_read", {"path": match.group(1)}, text)
        match = re.fullmatch(r"workspace search\s+(.+)", stripped, re.IGNORECASE)
        if match:
            return self._action("workspace_search", {"query": match.group(1)}, text)
        if re.fullmatch(r"workspace diff", stripped, re.IGNORECASE):
            return self._action("workspace_diff", {}, text)
        if re.fullmatch(r"run workspace linter", stripped, re.IGNORECASE):
            return self._action("run_linter", {}, text)
        if re.fullmatch(r"run workspace type checker", stripped, re.IGNORECASE):
            return self._action("run_type_checker", {}, text)
        if re.fullmatch(r"run workspace tests", stripped, re.IGNORECASE):
            return self._action("run_project_tests", {}, text)
        if re.fullmatch(r"(?:list|show) knowledge(?: documents)?", stripped, re.IGNORECASE):
            return self._action("knowledge_list", {}, text)
        match = re.fullmatch(r"(?:index folder|sync knowledge folder)\s+(.+)", stripped, re.IGNORECASE)
        if match:
            return self._action("knowledge_sync", {"path": match.group(1)}, text)
        match = re.fullmatch(r"(?:semantic index|semantically index) document\s+(.+)", stripped, re.IGNORECASE)
        if match:
            return self._action("knowledge_index_semantic", {"path": match.group(1)}, text)
        match = re.fullmatch(r"(?:semantic search|search semantic knowledge)(?: for)?\s+(.+)", stripped, re.IGNORECASE)
        if match:
            return self._action("knowledge_semantic_search", {"query": match.group(1)}, text)
        match = re.fullmatch(r"ask knowledge\s+(.+)", stripped, re.IGNORECASE)
        if match:
            return self._action("knowledge_answer", {"query": match.group(1)}, text)
        match = re.fullmatch(r"(?:index|import) document\s+(.+)", stripped, re.IGNORECASE)
        if match:
            return self._action("knowledge_index", {"path": match.group(1)}, text)
        match = re.fullmatch(r"remove document from index\s+(.+)", stripped, re.IGNORECASE)
        if match:
            return self._action("knowledge_remove", {"path": match.group(1)}, text)
        match = re.fullmatch(r"search knowledge(?: for)?\s+(.+)", stripped, re.IGNORECASE)
        if match:
            return self._action("knowledge_search", {"query": match.group(1)}, text)
        mode_match = re.fullmatch(
            r"(?:(?:switch|change|set)(?:\s+to)?\s+)?"
            r"(friend|advisor|technical|strategist|professional|jarvis)(?:\s+mode)?",
            stripped,
            flags=re.IGNORECASE,
        )
        if mode_match:
            return self._action("set_personality_mode", {"mode": mode_match.group(1).lower()}, text)

        # ── HINATA Web Autofill & Identity Vault (high priority — before generic open) ──
        #   open website / go to <url>
        result = self._parse_web(stripped, text)
        if result is not None:
            return result
        result = self._parse_scraper(stripped, text)
        if result is not None:
            return result
        result = self._parse_files(stripped, text)
        if result is not None:
            return result
        result = self._parse_comms(stripped, text)
        if result is not None:
            return result
        result = self._parse_system(stripped, text)
        if result is not None:
            return result
        result = self._parse_research(stripped, text)
        if result is not None:
            return result
        result = self._parse_dev(stripped, text)
        if result is not None:
            return result
        return None

    def _parse_web(self, stripped: str, text: str) -> ActionRequest | None:
        match = re.fullmatch(r"(?:please\s+)?(?:open|go\s+to|launch|visit)\s+(?:website\s+)?(?:https?://)?(\S+\.[^\s]+)(?:\s+in\s+(\w+)(?:\s+browser)?)?", stripped, re.I)
        if match:
            raw_url = match.group(1).strip()
            if not raw_url.lower().startswith('http'):
                raw_url = 'https://' + raw_url
            args: dict[str, object] = {"url": raw_url}
            if match.group(2):
                args["browser"] = match.group(2).lower()
            return self._action("open_website", args, text)
        # explicit open website syntax
        match = re.fullmatch(r"open\s+website\s+(\S+)(?:\s+in\s+(\w+))?", stripped, re.I)
        if match:
            raw_url = match.group(1).strip()
            if not raw_url.lower().startswith('http'):
                raw_url = 'https://' + raw_url
            oargs: dict[str, object] = {"url": raw_url}
            if match.group(2):
                oargs["browser"] = match.group(2).lower()
            return self._action("open_website", oargs, text)
        match = re.fullmatch(r"inspect\s+(?:forms?\s+on|page)\s+(\S+)", stripped, re.I)
        if match:
            raw_url = match.group(1).strip()
            if not raw_url.lower().startswith('http'):
                raw_url = 'https://' + raw_url
            return self._action("inspect_forms", {"url": raw_url}, text)
        match = re.fullmatch(r"(?:save|store|remember|update)\s+(?:my\s+)?(?:identity|profile|details)(?:\s+as\s+(\w+))?(?::\s*(.+))?", stripped, re.I)
        if match:
            prof = (match.group(1) or 'personal').strip().lower()
            rest = (match.group(2) or '').strip()
            fields_json = rest
            if rest and not (rest.strip().startswith('{') and rest.strip().endswith('}')):
                import json as _json
                kv: dict[str, str] = {}
                import re as _re
                parts = _re.split(r"\s*[;,]\s*", rest)
                for part in parts:
                    if '=' in part:
                        k, v = part.split('=', 1)
                        kv[k.strip().lower()] = v.strip()
                    elif ':' in part:
                        k, v = part.split(':', 1)
                        kv[k.strip().lower()] = v.strip()
                if kv:
                    fields_json = _json.dumps(kv)
                else:
                    fields_json = _json.dumps({'full_name': rest})
            if not fields_json:
                fields_json = '{}'
            return self._action("save_identity", {"profile": prof, "fields_json": fields_json}, text)
        match = re.fullmatch(r"my\s+(email|phone|address|city|company|website)\s+is\s+(.+)", stripped, re.I)
        if match:
            key = match.group(1).strip().lower()
            val = match.group(2).strip()
            import json as _json
            return self._action("save_identity", {"profile": "personal", "fields_json": _json.dumps({key: val})}, text)
        match = re.fullmatch(r"list\s+(?:my\s+)?(?:identities|profiles|details|saved\s+info)", stripped, re.I)
        if match:
            return self._action("list_identities", {}, text)
        match = re.fullmatch(r"(?:show|get|view)\s+(?:my\s+)?(?:identity|profile|details)(?:\s+(\w+))?", stripped, re.I)
        if match:
            prof = (match.group(1) or '').strip()
            return self._action("get_identity", {"profile": prof} if prof else {}, text)
        match = re.fullmatch(r"(?:delete|forget|remove)\s+(?:my\s+)?(?:identity|profile)\s+(\w+)", stripped, re.I)
        if match:
            return self._action("delete_identity", {"profile": match.group(1).strip().lower()}, text)
        match = re.fullmatch(r"(?:please\s+)?(?:autofill|fill\s+(?:form|details|my\s+details|my\s+info)|complete\s+form)(?:\s+on|\s+at|\s+for)?\s+(\S+)(?:\s+with\s+(\w+)(?:\s+profile)?)?(?:\s+and\s+submit)?", stripped, re.I)
        if match:
            raw_url = match.group(1).strip().rstrip('.,!?')
            if not raw_url.lower().startswith('http'):
                raw_url = 'https://' + raw_url
            fargs: dict[str, object] = {"url": raw_url}
            if match.group(2):
                fargs["profile"] = match.group(2).strip().lower()
            if 'submit' in stripped.lower():
                fargs["submit"] = True
            return self._action("web_autofill", fargs, text)
        match = re.fullmatch(r"fill\s+form\s+(\S+)", stripped, re.I)
        if match:
            raw_url = match.group(1).strip()
            if not raw_url.lower().startswith('http'):
                raw_url = 'https://' + raw_url
            return self._action("web_autofill", {"url": raw_url}, text)
        match = re.fullmatch(r"submit\s+form\s+(\S+)", stripped, re.I)
        if match:
            raw_url = match.group(1).strip()
            if not raw_url.lower().startswith('http'):
                raw_url = 'https://' + raw_url
            return self._action("web_autofill", {"url": raw_url, "submit": True}, text)
        return None

    def _parse_scraper(self, stripped: str, text: str) -> ActionRequest | None:
        match = re.fullmatch(r"scrape\s+tables?\s+(?:from\s+)?(\S+)", stripped, re.I)
        if match:
            raw = match.group(1).strip()
            if not raw.lower().startswith('http'):
                raw = 'https://' + raw
            return self._action("scrape_tables", {"url": raw}, text)
        match = re.fullmatch(r"scrape\s+lists?\s+(?:from\s+)?(\S+)", stripped, re.I)
        if match:
            raw = match.group(1).strip()
            if not raw.lower().startswith('http'):
                raw = 'https://' + raw
            return self._action("scrape_lists", {"url": raw}, text)
        match = re.fullmatch(r"(?:monitor|track|check)\s+price\s+(?:for\s+)?(\S+)", stripped, re.I)
        if match:
            raw = match.group(1).strip()
            if not raw.lower().startswith('http'):
                raw = 'https://' + raw
            return self._action("monitor_price", {"url": raw}, text)
        match = re.fullmatch(r"crawl\s+(?:pages?\s+)?(?:from\s+)?(\S+)(?:\s+max\s+(\d+)\s+pages?)?", stripped, re.I)
        if match:
            raw = match.group(1).strip()
            if not raw.lower().startswith('http'):
                raw = 'https://' + raw
            args: dict[str, object] = {"url": raw}
            if match.group(2):
                args["max_pages"] = int(match.group(2))
            return self._action("crawl_pages", args, text)
        match = re.fullmatch(r"summarize\s+(?:url|page|website)\s+(\S+)", stripped, re.I)
        if match:
            raw = match.group(1).strip()
            if not raw.lower().startswith('http'):
                raw = 'https://' + raw
            return self._action("summarize_url", {"url": raw}, text)
        return None

    def _parse_files(self, stripped: str, text: str) -> ActionRequest | None:
        match = re.fullmatch(r"analyze\s+storage(?:\s+(?:for|in)\s+(.+))?", stripped, re.I)
        if match:
            args2: dict[str, object] = {}
            if match.group(1):
                args2["path"] = match.group(1).strip()
            return self._action("analyze_storage", args2, text)
        match = re.fullmatch(r"find\s+duplicates?(?:\s+(?:in|for)\s+(.+))?", stripped, re.I)
        if match:
            args2 = {"path": match.group(1).strip()} if match.group(1) else {}
            return self._action("find_duplicates", args2, text)
        match = re.fullmatch(r"organize\s+folder\s+(.+?)(?:\s+by\s+(type|date|ext))?", stripped, re.I)
        if match:
            args2 = {"path": match.group(1).strip()}
            if match.group(2):
                args2["mode"] = f"by_{match.group(2).lower()}"
            return self._action("organize_folder", args2, text)
        match = re.fullmatch(r"batch\s+rename\s+(.+?)\s+(numbered|find_replace|lowercase|uppercase|prefix)(?:\s+(.+))?", stripped, re.I)
        if match:
            args2 = {"folder": match.group(1).strip(), "pattern": match.group(2).lower()}
            if match.group(3):
                args2["replacement"] = match.group(3).strip()
            return self._action("batch_rename", args2, text)
        match = re.fullmatch(r"convert\s+(?:file\s+)?(\S+)\s+to\s+(\S+)", stripped, re.I)
        if match:
            return self._action("convert_file", {"source": match.group(1).strip(), "dest": match.group(2).strip()}, text)
        match = re.fullmatch(r"clean\s+temp(?:\s+(?:in\s+)?(.+))?", stripped, re.I)
        if match:
            args2 = {"path": match.group(1).strip()} if match.group(1) else {}
            return self._action("clean_temp", args2, text)
        match = re.fullmatch(r"quick\s+cleanup(?:\s+(.+))?", stripped, re.I)
        if match:
            args2 = {"path": match.group(1).strip()} if match.group(1) else {}
            return self._action("quick_cleanup", args2, text)
        return None

    def _parse_comms(self, stripped: str, text: str) -> ActionRequest | None:
        match = re.fullmatch(r"draft\s+email\s+to\s+(\S+)(?:\s+subject\s+(.+?))?(?:\s+using\s+(\w+))?\s*$", stripped, re.I)
        if match:
            args2 = {"to": match.group(1).strip()}
            if match.group(2):
                args2["subject"] = match.group(2).strip()
            if match.group(3):
                args2["template"] = match.group(3).strip()
            return self._action("draft_email", args2, text)
        match = re.fullmatch(r"list\s+drafts", stripped, re.I)
        if match:
            return self._action("list_drafts", {}, text)
        match = re.fullmatch(r"(?:show|get)\s+template\s+(\w+)", stripped, re.I)
        if match:
            return self._action("email_template_info", {"name": match.group(1).strip().lower()}, text)
        match = re.fullmatch(r"remind\s+me\s+(.+?)\s+in\s+(.+)", stripped, re.I)
        if match:
            return self._action("create_reminder", {"title": match.group(1).strip(), "when_str": "in " + match.group(2).strip()}, text)
        match = re.fullmatch(r"create\s+reminder\s+(.+?)(?:\s+at\s+(.+))?", stripped, re.I)
        if match:
            args2 = {"title": match.group(1).strip()}
            if match.group(2):
                args2["when_str"] = match.group(2).strip()
            return self._action("create_reminder", args2, text)
        match = re.fullmatch(r"list\s+reminders", stripped, re.I)
        if match:
            return self._action("list_reminders", {}, text)
        match = re.fullmatch(r"broadcast\s+(.+)", stripped, re.I)
        if match:
            return self._action("broadcast_message", {"message": match.group(1).strip()}, text)
        return None

    def _parse_system(self, stripped: str, text: str) -> ActionRequest | None:
        match = re.fullmatch(r"(?:health\s+check|system\s+health|check\s+health)", stripped, re.I)
        if match:
            return self._action("health_check", {}, text)
        match = re.fullmatch(r"(?:network\s+diag|check\s+network|internet\s+status)", stripped, re.I)
        if match:
            return self._action("network_diag", {}, text)
        match = re.fullmatch(r"backup\s+(\S+)\s+to\s+(\S+)", stripped, re.I)
        if match:
            return self._action("backup_create", {"source": match.group(1).strip(), "dest_dir": match.group(2).strip()}, text)
        match = re.fullmatch(r"search\s+process(?:es)?\s+(?:for\s+)?(.+)", stripped, re.I)
        if match:
            return self._action("process_search", {"name": match.group(1).strip()}, text)
        return None

    def _parse_research(self, stripped: str, text: str) -> ActionRequest | None:
        match = re.fullmatch(r"deep\s+research(?:\s+on)?\s+(.+)", stripped, re.I)
        if match:
            return self._action("deep_research", {"topic": match.group(1).strip()}, text)
        match = re.fullmatch(r"research\s+(.+)", stripped, re.I)
        # Avoid catching simple web_search already handled; only if topic looks researchy
        if match and len(match.group(1).strip().split()) >= 2:
            # Let existing web_search handle plain short queries; we keep this as fallback for explicit 'research' verb
            return self._action("deep_research", {"topic": match.group(1).strip()}, text)
        match = re.fullmatch(r"compare\s+(.+?)\s+(?:vs|versus|against)\s+(.+)", stripped, re.I)
        if match:
            return self._action("compare_topics", {"topic_a": match.group(1).strip(), "topic_b": match.group(2).strip()}, text)
        return None

    def _parse_dev(self, stripped: str, text: str) -> ActionRequest | None:
        match = re.fullmatch(r"git\s+status(?:\s+(.+))?", stripped, re.I)
        if match:
            args2 = {}
            if match.group(1):
                args2["workspace"] = match.group(1).strip()
            return self._action("git_status", args2, text)
        match = re.fullmatch(r"git\s+diff(?:\s+(staged))?\s*(.*)", stripped, re.I)
        if match:
            args2 = {}
            if match.group(2) and match.group(2).strip():
                args2["workspace"] = match.group(2).strip()
            if match.group(1):
                args2["staged"] = True
            return self._action("git_diff", args2, text)
        match = re.fullmatch(r"git\s+commit\s+(.+?)(?:\s+in\s+(.+))?", stripped, re.I)
        if match:
            args2 = {"message": match.group(1).strip().strip('"\'')}
            if match.group(2):
                args2["workspace"] = match.group(2).strip()
            return self._action("git_commit", args2, text)
        match = re.fullmatch(r"code\s+review(?:\s+(.+))?", stripped, re.I)
        if match:
            args2 = {}
            if match.group(1):
                args2["path"] = match.group(1).strip()
            return self._action("code_review", args2, text)
        match = re.fullmatch(r"dependency\s+audit(?:\s+(.+))?", stripped, re.I)
        if match:
            args2 = {}
            if match.group(1):
                args2["workspace"] = match.group(1).strip()
            return self._action("dependency_audit", args2, text)
        match = re.fullmatch(r"run\s+tests?(?:\s+(.+))?", stripped, re.I)
        if match:
            args2 = {}
            if match.group(1):
                args2["target"] = match.group(1).strip()
            return self._action("dev_run_tests", args2, text)

        patterns: list[tuple[str, str, tuple[str, ...]]] = [
            # --- two-path file operations. These are listed first and are far
            # more specific than the generic "open <app>" rule below, which would
            # otherwise swallow ("copy X to Y" never reached copy_file at all).
            (r"^(?:copy|duplicate)(?:\s+the)?(?:\s+file)?\s+(.+?)\s+(?:to|into)\s+(.+)$", "copy_file", ("source", "destination")),
            (r"^(?:move|rename)(?:\s+the)?(?:\s+file)?\s+(.+?)\s+(?:to|into|as)\s+(.+)$", "move_file", ("source", "destination")),
            (r"^(?:archive|zip|compress)(?:\s+the)?(?:\s+folder)?\s+(.+?)\s+(?:to|into|as)\s+(.+)$", "archive_files", ("source", "archive_path")),
            (r"^compare(?:\s+the)?(?:\s+files?)?\s+(.+?)\s+(?:with|and|to)\s+(.+)$", "compare_files", ("first", "second")),
            # --- write a text file: "write <text> to <path>" / "save <text> as <path>"
            (r"^(?:write|save)\s+(?:a\s+note\s+)?(?:saying\s+)?(.+?)\s+(?:to|into|as)\s+(\S+\.[\w]+)$", "write_text_file", ("content", "path")),
            # --- destructive, but the guard/confirmation tier handles that
            (r"^(?:delete|remove|trash)(?:\s+the)?(?:\s+file)?\s+(.+)$", "quarantine_delete_file", ("path",)),
            (r"^(?:open|launch|start)\s+(.+)$", "open_application", ("name",)),
            (r"^(?:close|exit|quit)\s+(.+)$", "close_application", ("name",)),
            (r"^(?:open|show)(?:\s+the)?\s+folder\s+(.+)$", "open_folder", ("path",)),
            (r"^open(?:\s+the)?\s+file\s+(.+)$", "open_file", ("path",)),
            (r"^(?:create|make)(?:\s+a)?(?:\s+new)?\s+folder(?:\s+called|\s+named)?\s+(.+)$", "create_folder", ("path",)),
            (r"^(?:read|open\s+and\s+read)(?:\s+the)?(?:\s+text)?\s+file\s+(.+)$", "read_text_file", ("path",)),
            (r"^type\s+(.+)$", "type_text", ("text",)),
        ]
        # More specific path commands must run before generic open application.
        _PATH_TOOLS = {
            "open_folder", "open_file", "create_folder", "read_text_file", "type_text",
            "copy_file", "move_file", "archive_files", "compare_files", "write_text_file",
            "quarantine_delete_file",
        }
        patterns.sort(key=lambda item: 0 if item[1] in _PATH_TOOLS else 1)
        for pattern, tool, keys in patterns:
            match = re.fullmatch(pattern, stripped, flags=re.IGNORECASE)
            if match:
                values = [value.strip() for value in match.groups()]
                if tool == "type_text":
                    values[0] = self._strip_matching_quotes(stripped[5:].strip())
                return self._action(tool, dict(zip(keys, values, strict=True)), text)

        match = re.fullmatch(r"(?:search|find)(?:\s+local)?\s+files?(?:\s+for|\s+named)?\s+(.+?)(?:\s+in\s+(.+))?", stripped, re.IGNORECASE)
        if match:
            args: dict[str, Any] = {"query": match.group(1).strip()}
            if match.group(2):
                args["root"] = match.group(2).strip()
            return self._action("search_files", args, text)

        match = re.fullmatch(r"(?:set\s+)?volume\s*(?:to)?\s*(\d{1,3})(?:\s*percent)?", stripped, re.IGNORECASE)
        if match:
            return self._action("volume", {"action": "set", "value": int(match.group(1))}, text)
        match = re.fullmatch(r"(?:turn\s+)?volume\s+(up|down)|mute(?:\s+volume)?", stripped, re.IGNORECASE)
        if match:
            return self._action("volume", {"action": match.group(1).lower() if match.group(1) else "mute"}, text)

        if re.fullmatch(r"(?:system|pc|computer)\s+(?:info|information|status)|system\s+info", stripped, re.IGNORECASE):
            return self._action("system_info", {}, text)
        if re.fullmatch(r"(?:list|show)(?:\s+my)?\s+preferences", stripped, re.IGNORECASE):
            return self._action("list_preferences", {}, text)
        match = re.fullmatch(r"remember\s+(?:that\s+)?my\s+([\w .-]+)\s+is\s+(.+)", stripped, re.IGNORECASE)
        if match:
            return self._action("remember_preference", {"key": match.group(1), "value": match.group(2)}, text)
        match = re.fullmatch(r"(?:search|find)\s+memory\s+(?:for\s+)?(.+)", stripped, re.IGNORECASE)
        if match:
            return self._action("memory_search", {"query": match.group(1)}, text)
        match = re.fullmatch(r"(?:clear|delete|forget)\s+(conversations|commands|preferences|memory|everything)", stripped, re.IGNORECASE)
        if match:
            scope = match.group(1).lower()
            return self._action("clear_memory", {"scope": "all" if scope in {"memory", "everything"} else scope}, text)
        match = re.fullmatch(r"(?:please\s+)?(?:search(?:\s+for)?|web\s+search(?:\s+for)?)\s+(.+)", stripped, re.I)
        if match:
            return self._action("web_search", {"query": match.group(1).strip()}, text)
        return None
        return None

    def parse_multitask(self, text: str) -> list[str]:
        """Split clear independent compound requests; return [] when any part is ambiguous."""
        raw = text.strip()
        # Browser-first syntax takes the entire query literally, including words
        # such as "then" and punctuation such as semicolons.
        unaddressed = _ADDRESS_PREFIX.sub("", raw)
        if _BROWSER_FIRST_SEARCH.fullmatch(unaddressed):
            return []
        # In a free-form search, “and open source” is query text, not a second
        # command. Require an explicit separator for those searches.
        separator = r";|\band\s+then\b|\bthen\b"
        if not re.match(r"^(?:please\s+)?search\s+(?!(?:the\s+)?(?:web|internet|online)\b)", unaddressed, re.I):
            separator += r"|\balso\b|\band\s+(?=(?:search|google|open|launch|start|close|list|show|write|create|generate|fetch|system|find|set\s+volume)\b)"
        parts = re.split(rf"\s*(?:{separator})\s*", raw, flags=re.IGNORECASE)
        parts = [part.strip(" ,") for part in parts if part.strip(" ,")]
        if len(parts) < 2 or len(parts) > 5:
            return []
        if any(self.parse(part) is None for part in parts):
            return []
        return parts

    @staticmethod
    def _strip_matching_quotes(value: str) -> str:
        pairs = {('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’")}
        for opening, closing in pairs:
            if len(value) >= 2 and value.startswith(opening) and value.endswith(closing):
                return value[1:-1]
        return value

    def _action(self, tool: str, arguments: dict[str, Any], original: str) -> ActionRequest:
        action = self.validator.validate(tool, arguments)
        return replace(action, original_text=original)

    def parse_llm_plan(self, content: str, original_text: str) -> tuple[str, Any]:
        candidate = extract_reply_object(content)
        # A small model sometimes answers in plain prose.  That is still a usable
        # answer; only an unrelated blob is rejected as an invalid plan.
        if candidate is None:
            prose = plain_text_reply(content)
            if prose is not None:
                return "chat", prose[:4000]
            raise PlanError("The local model did not return a valid HINATA JSON plan")
        kind = candidate.get("type")
        if kind == "plan":
            return "plan", normalize_plan(candidate)
        if kind == "tool":
            tool_name = candidate.get("tool")
            if not isinstance(tool_name, str):
                raise PlanError("The local model omitted a valid tool name")
            try:
                action = self.validator.validate(tool_name, candidate.get("arguments"))
            except ValidationError as exc:
                raise PlanError(f"The local model proposed an invalid action: {exc}") from exc
            return "tool", replace(action, original_text=original_text)
        if kind in {"chat", "clarify"}:
            text = candidate.get("content")
            if isinstance(text, str) and text.strip():
                return kind, text.strip()[:4000]
            # The model identified a conversational response but chose another
            # common text key.  Recover display text only; actions never reach
            # this branch and remain strictly validated above.
            salvaged = plain_text_reply(content)
            if salvaged:
                return kind, salvaged[:4000]
        raise PlanError("The local model did not return a valid HINATA JSON plan")
