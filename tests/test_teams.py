# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Wolfgang Moser
import argparse
import base64
import contextlib
import datetime as dt
import importlib.util
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest
from typing import Any
from unittest import mock

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "skills" / "ms-teams" / "scripts" / "teams.py"

spec = importlib.util.spec_from_file_location("teams", SCRIPT_PATH)
assert spec is not None
assert spec.loader is not None
teams: Any = importlib.util.module_from_spec(spec)
spec.loader.exec_module(teams)


class TeamsSkillTests(unittest.TestCase):
    def test_html_to_text_and_links(self):
        raw = (
            '<p>Hello <at id="0">Ada Lovelace</at>!<br/>'
            'Check <a href="https://example.com/docs">the docs</a> &amp; notes. '
            '<emoji id="man_detective" alt="🕵️" title="Detective"></emoji> '
            '<customemoji id="MC1uZGUtZDItYWJj" alt="shipit" source="https://example.com/x"></customemoji>'
            '<attachment id="card-1"></attachment></p>'
        )
        self.assertEqual(
            teams.html_to_text(raw),
            "Hello @Ada Lovelace!\nCheck the docs & notes. 🕵️ :shipit:",
        )
        self.assertEqual(
            teams.html_links(raw),
            ["https://example.com/docs"],
        )

    def test_custom_emoji_harvest_and_shortcode_expansion(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(teams, "_CACHE_DIR", pathlib.Path(tmp)),
        ):
            teams._custom_emojis_cache = None
            payload = (
                b"id=0-nde-d2-1234567890,type=1,url=https://de-prod.asyncgw.teams.microsoft.com"
                b"/v1/objects/0-nde-d2-1234567890/views/imgt2"
            )
            hosted = base64.b64encode(payload).decode()
            raw_msg = {
                "body": {"content": "<p>Test</p>"},
                "reactions": [
                    {
                        "reactionType": "custom",
                        "displayName": "shipit",
                        "reactionContentUrl": (
                            "https://graph.microsoft.com/v1.0/teams/t/channels/c/messages/1/"
                            f"hostedContents/{hosted}/$value"
                        ),
                    }
                ],
            }
            found = teams.harvest_custom_emojis(raw_msg)
            self.assertIn("shipit", found)
            self.assertEqual(found["shipit"]["objectId"], "0-nde-d2-1234567890")

            expanded = teams.expand_custom_emojis("<p>Status :shipit: and literal <code>:shipit:</code></p>")
            self.assertIn('<customemoji id="MC1uZGUtZDItMTIzNDU2Nzg5MA==" alt="shipit"', expanded)
            self.assertIn("<code>:shipit:</code>", expanded)

    def test_build_message_with_mentions_importance_and_card(self):
        fake_cfg = {
            "homeTeams": ["22222222-2222-2222-2222-222222222222"],
        }
        target = {
            "kind": "channel",
            "teamId": "22222222-2222-2222-2222-222222222222",
            "channelId": "19:rel@thread.tacv2",
            "team": "Core Engineering",
            "label": "Core Engineering / Releases",
        }
        fake_users = [
            {
                "userId": "u-ada",
                "name": "Ada Lovelace",
                "email": "ada.lovelace@example.com",
            }
        ]
        fake_channels = [{"id": "19:chan@thread.tacv2", "name": "General", "type": "standard"}]
        card = {
            "type": "AdaptiveCard",
            "version": "1.4",
            "body": [{"type": "TextBlock", "text": "Deployment v1.2.0 complete"}],
        }
        with (
            mock.patch.object(teams, "find_users", return_value=fake_users),
            mock.patch.object(teams, "team_channels", return_value=fake_channels),
        ):
            msg = teams.build_message(
                conn=None,
                cfg=fake_cfg,
                html_in="<p>Hi @[Ada], cc @[Ada] and @[channel:General]</p>",
                target=target,
                subject="Release v1.2.0",
                importance="high",
                card=card,
            )
        self.assertEqual(msg["subject"], "Release v1.2.0")
        self.assertEqual(msg["importance"], "high")
        self.assertIn(
            '<p>Hi <at id="0">Ada Lovelace</at>, cc <at id="0">Ada Lovelace</at> and <at id="1">General</at></p>',
            msg["body"]["content"],
        )
        self.assertEqual(len(msg["mentions"]), 2)
        self.assertEqual(msg["mentions"][0]["mentionText"], "Ada Lovelace")
        self.assertEqual(msg["mentions"][1]["mentionText"], "General")
        self.assertEqual(len(msg["attachments"]), 1)
        self.assertEqual(
            msg["attachments"][0]["contentType"],
            "application/vnd.microsoft.card.adaptive",
        )

    def test_plain_text_mode_does_not_parse_mentions(self):
        args = argparse.Namespace(html=None, html_file=None, text="Array access: items@[0] & <ok>")
        body_html, parse_mentions = teams.get_body_arg(args)
        self.assertFalse(parse_mentions)
        msg = teams.build_message(
            conn=None,
            cfg={},
            html_in=body_html,
            target={"kind": "chat"},
            parse_mentions=parse_mentions,
        )
        self.assertEqual(msg["body"]["content"], "<p>Array access: items@[0] &amp; &lt;ok&gt;</p>")
        self.assertNotIn("mentions", msg)

    def test_empty_and_punctuation_queries_rejected(self):
        items = [{"id": "id-1", "name": "General"}]
        with self.assertRaises(SystemExit) as ctx:
            teams.pick(items, "???", lambda x: x["name"], "channel")
        self.assertEqual(ctx.exception.code, 2)

        hit, err = teams.find_one(items, "🚀", lambda x: x["name"])
        self.assertIsNone(hit)
        self.assertIn("no match", err)

        with mock.patch.object(
            teams, "known_users", return_value={"u1": {"userId": "u1", "name": "Ada", "email": "a@example.com"}}
        ):
            self.assertEqual(teams.find_users(None, {}, "???", tenant=False), [])

        by_id = teams.pick_by_id_or_name(items, "id-1", lambda x: x["name"], "channel")
        self.assertEqual(by_id["name"], "General")

    def test_tenant_users_searches_by_email(self):
        captured = {}
        payload = json.dumps(
            {
                "value": [
                    {
                        "id": "u1",
                        "displayName": "Ada Lovelace",
                        "mail": "ada.lovelace@example.com",
                        "userPrincipalName": "ada.lovelace@example.com",
                    }
                ]
            }
        ).encode()

        def fake_http(_method, url, headers=None, _body=None, _timeout=60):
            captured["url"] = url
            captured["headers"] = headers or {}
            return 200, payload

        with (
            mock.patch.object(teams, "http", fake_http),
            mock.patch.object(teams, "az_token", return_value="t"),
        ):
            users = teams.tenant_users("ada.lovelace@example.com")
        self.assertEqual(users[0]["userId"], "u1")
        self.assertEqual(users[0]["email"], "ada.lovelace@example.com")
        self.assertIn("$filter=", captured["url"])
        self.assertIn("mail%20eq", captured["url"])
        self.assertIn("userPrincipalName", captured["url"])
        self.assertNotIn("ConsistencyLevel", captured["headers"])

    def test_find_users_falls_back_to_email_search(self):
        with (
            mock.patch.object(teams, "known_users", return_value={}),
            mock.patch.object(
                teams,
                "tenant_users",
                return_value=[{"userId": "u1", "name": "Ada", "email": "d@example.com"}],
            ) as tenant_search,
        ):
            hits = teams.find_users(None, {}, "d@example.com")
        self.assertEqual(hits[0]["userId"], "u1")
        tenant_search.assert_called_once_with("d@example.com")

    def test_verify_mentions_detects_missing(self):
        sent = {
            "mentions": [
                {"id": 0, "mentionText": "Ada Lovelace"},
                {"id": 1, "mentionText": "General"},
            ]
        }
        resp = {"mentions": [{"id": 0}]}
        ok, missing = teams.verify_mentions(sent, resp)
        self.assertEqual(ok, ["Ada Lovelace"])
        self.assertEqual(missing, ["General"])

    def test_norm_msg_extracts_attachments_and_importance(self):
        raw = {
            "id": "m-1",
            "createdDateTime": "2026-11-10T10:00:00Z",
            "lastModifiedDateTime": "2026-11-10T10:00:00Z",
            "importance": "urgent",
            "from": {"user": {"id": "u-grace", "displayName": "Grace Hopper"}},
            "body": {"content": ""},
            "attachments": [
                {
                    "id": "a-1",
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": json.dumps(
                        {
                            "type": "AdaptiveCard",
                            "body": [{"type": "TextBlock", "text": "Build succeeded"}],
                        }
                    ),
                }
            ],
        }
        normed = teams.norm_msg(raw, {}, "u-ada")
        self.assertEqual(normed["importance"], "urgent")
        self.assertEqual(normed["text"], "[attachment: Build succeeded]")
        self.assertEqual(normed["attachments"][0]["cardText"], "Build succeeded")

    def test_matches_search_checks_replies_and_author(self):
        msg = {
            "author": "Grace Hopper",
            "subject": "Architecture notes",
            "text": "Initial draft",
            "links": [],
            "replies": [
                {
                    "author": "Ada Lovelace",
                    "subject": None,
                    "text": "Updated the latency numbers for the new connector.",
                    "links": ["https://example.com/runbook"],
                }
            ],
        }
        latency = re.compile("latency", re.IGNORECASE)
        self.assertTrue(teams.matches_search(msg, latency, None))
        self.assertTrue(teams.matches_search(msg, latency, "ada"))
        self.assertFalse(teams.matches_search(msg, latency, "grace"))

    def test_generic_digest_lines_and_open_hints(self):
        thread = {
            "created": "2026-11-10T09:00:00Z",
            "byMe": False,
            "mentionsMe": True,
            "mentions": [{"type": "user", "name": "Ada Lovelace"}],
            "reactionsByMe": [],
            "_reps": [],
        }
        self.assertEqual(teams.open_hints(thread), {"openForMe": True})

        # Pre-window reply by me should suppress openForMe even if _reps only holds newer replies
        answered_before_window = {
            "created": "2026-11-08T09:00:00Z",
            "byMe": False,
            "mentionsMe": True,
            "mentions": [{"type": "user", "name": "Ada Lovelace"}],
            "reactionsByMe": [],
            "replies": [
                {
                    "created": "2026-11-08T09:10:00Z",
                    "byMe": True,
                    "mentionsMe": False,
                    "mentions": [],
                    "reactionsByMe": [],
                },
                {
                    "created": "2026-11-10T10:00:00Z",
                    "byMe": False,
                    "mentionsMe": False,
                    "mentions": [],
                    "reactionsByMe": [],
                },
            ],
            "_reps": [
                {
                    "created": "2026-11-10T10:00:00Z",
                    "byMe": False,
                    "mentionsMe": False,
                    "mentions": [],
                    "reactionsByMe": [],
                },
            ],
        }
        self.assertEqual(teams.open_hints(answered_before_window), {})

        groups = [
            {
                "name": "Engineering",
                "channels": [
                    {
                        "team": "Core Engineering",
                        "channel": "General",
                        "messages": [
                            {
                                "time": "2026-11-10 10:00",
                                "author": "Grace Hopper",
                                "byMe": False,
                                "text": "Can you check the release notes?",
                                "openForMe": True,
                                "repliesInWindow": [
                                    {
                                        "time": "2026-11-10 10:05",
                                        "author": "Ada Lovelace",
                                        "byMe": True,
                                        "text": "Checking now.",
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ]
        chats = [
            {
                "type": "oneOnOne",
                "with": "Grace Hopper",
                "lastMessageByMe": False,
                "messagesAtEndNotByMe": 1,
                "open": "question",
                "messages": [
                    {
                        "time": "2026-11-10 11:00",
                        "author": "Grace Hopper",
                        "byMe": False,
                        "text": "Are you free at 14:00?",
                    }
                ],
            }
        ]
        lines = [json.loads(ln) for ln in teams.digest_lines(groups, chats, verbose=False)]
        self.assertEqual([x["kind"] for x in lines], ["post", "reply", "chat", "chatmsg"])
        self.assertEqual(lines[0]["open"], "mention")
        self.assertNotIn("section", lines[0])

    def test_lookup_and_resolve_meeting_id(self):
        fake_conn = mock.MagicMock()
        fake_conn.op.return_value = {"id": "19:meeting_abc@thread.v2/0", "subject": "Sync"}
        res = teams.lookup_meeting(fake_conn, {}, "https://teams.microsoft.com/l/meetup-join/19%3ameeting")
        self.assertEqual(res["subject"], "Sync")
        self.assertIn("lookupType=JoinWebUrl", fake_conn.op.call_args[0][1])

        mid = teams.resolve_meeting_id(fake_conn, {}, "123 456 789")
        self.assertIn("lookupType=JoinMeetingId&lookupValue=123456789", fake_conn.op.call_args[0][1])
        self.assertEqual(mid, "19%3Ameeting_abc%40thread.v2%2F0")

        # Non-digit dashes should not be treated as numeric JoinMeetingId
        self.assertFalse(teams._is_numeric_meeting_id("---"))

    def test_own_message_check_and_edit_dry_run_guard(self):
        fake_conn = mock.MagicMock()
        fake_conn.graph_json.return_value = {"from": {"user": {"id": "u-other"}}}
        cfg = {"user": {"id": "u-me"}}
        target = {"kind": "chat", "chatId": "48:notes", "label": "self-chat", "msgs": "me/chats/48:notes/messages"}

        with self.assertRaises(SystemExit) as ctx:
            teams.own_message_check(fake_conn, cfg, target, "m-1", None)
        self.assertEqual(ctx.exception.code, 3)

        # Dry-run edit must also check ownership and reject --reply-id on chats
        args = argparse.Namespace(
            to="self",
            message_id="m-1",
            reply_id="r-1",
            subject=None,
            importance=None,
            html=None,
            html_file=None,
            text="edit",
            card=None,
            card_file=None,
            send=False,
        )
        with mock.patch.object(teams, "connect", return_value=(cfg, fake_conn)):
            with self.assertRaises(SystemExit) as ctx2:
                teams.cmd_edit(args)
            self.assertEqual(ctx2.exception.code, 2)

    def test_fetch_messages_counts_post_filter(self):
        fake_conn = mock.MagicMock()
        fake_conn.pages.return_value = [
            {
                "value": [
                    {"id": "del-1", "deletedDateTime": "2026-11-10T10:00:00Z", "messageType": "message"},
                    {"id": "sys-1", "messageType": "systemEventMessage"},
                    {
                        "id": "ok-1",
                        "createdDateTime": "2026-11-10T10:00:00Z",
                        "lastModifiedDateTime": "2026-11-10T10:00:00Z",
                        "messageType": "message",
                        "from": {"user": {"id": "u-1", "displayName": "Ada"}},
                        "body": {"content": "<p>Hello</p>"},
                    },
                ]
            },
            {
                "value": [
                    {
                        "id": "ok-2",
                        "createdDateTime": "2026-11-10T11:00:00Z",
                        "lastModifiedDateTime": "2026-11-10T11:00:00Z",
                        "messageType": "message",
                        "from": {"user": {"id": "u-1", "displayName": "Ada"}},
                        "body": {"content": "<p>World</p>"},
                    }
                ]
            },
        ]
        target = {"kind": "chat", "msgs": "me/chats/48:notes/messages"}
        res = teams.fetch_messages(fake_conn, target, {}, "u-1", limit=2, pages=2, expand_replies=False)
        self.assertEqual([m["id"] for m in res], ["ok-2", "ok-1"])

    def test_window_and_private_file_helpers(self):
        self.assertEqual(teams.last_working_day(dt.date(2026, 11, 9)), dt.date(2026, 11, 6))  # Mon -> Fri
        self.assertEqual(teams.last_working_day(dt.date(2026, 11, 8)), dt.date(2026, 11, 6))  # Sun -> Fri

        with tempfile.TemporaryDirectory() as tmp:
            p = pathlib.Path(tmp) / "secret.json"
            p.write_text("old")
            p.chmod(0o644)
            teams.write_private_bytes(p, b'{"ok":true}')
            if sys.platform != "win32":  # POSIX permission bits do not exist on Windows
                self.assertEqual(p.stat().st_mode & 0o777, 0o600)

        self.assertFalse(teams.open_browser("http://insecure.example.com"))
        self.assertFalse(teams.open_browser("javascript:alert(1)"))

        # Simulate Windows where os.fchmod is absent and os.startfile is used
        with tempfile.TemporaryDirectory() as tmp:
            p = pathlib.Path(tmp) / "win.json"
            with mock.patch.object(teams.os, "fchmod", None, create=True):
                teams.write_private_bytes(p, b'{"win":true}')
            self.assertEqual(json.loads(p.read_text()), {"win": True})

        with (
            mock.patch.object(teams.sys, "platform", "win32"),
            mock.patch.object(teams.os, "startfile", create=True) as sf,
        ):
            self.assertTrue(teams.open_browser("https://consent.azure-apim.net/test"))
            sf.assert_called_once_with("https://consent.azure-apim.net/test")

        # Simulate macOS where 'open' is used even if powershell.exe were on PATH
        with (
            mock.patch.object(teams.sys, "platform", "darwin"),
            mock.patch.object(teams.shutil, "which", return_value="/usr/bin/open"),
            mock.patch.object(teams.subprocess, "run") as sr,
        ):
            self.assertTrue(teams.open_browser("https://consent.azure-apim.net/test"))
            self.assertEqual(sr.call_args[0][0], ["open", "https://consent.azure-apim.net/test"])

    def test_fit_line_stays_under_limit_with_heavy_fields(self):
        huge = {
            "kind": "post",
            "text": "x" * 3000,
            "links": ["https://example.com/" + ("a" * 100) for _ in range(5)],
            "attachments": ["card-" + ("b" * 400) for _ in range(3)],
            "reactions": [{"emoji": "👍", "by": ["User " + str(i) for i in range(100)]}],
        }
        line = teams.fit_line(huge, verbose=False)
        self.assertLessEqual(len(line), teams.LINE_MAX)

    def test_cli_help_includes_all_generic_commands(self):
        r = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--help"],
            capture_output=True,
            encoding="utf-8",
            text=True,
            check=True,
        )
        for cmd in (
            "onboard",
            "doctor",
            "teams",
            "team-info",
            "channels",
            "channel-info",
            "members",
            "channel-members",
            "tags",
            "tag-members",
            "tabs",
            "files",
            "apps",
            "find-user",
            "chats",
            "find-chat",
            "chat-members",
            "create-chat",
            "sections",
            "section-items",
            "read",
            "thread",
            "message",
            "search",
            "delta",
            "graph",
            "op",
            "custom-emojis",
            "digest",
            "send",
            "reply",
            "edit",
            "delete",
            "react",
            "unreact",
            "meeting",
            "transcripts",
            "transcript",
            "recordings",
            "recording",
            "adhoc-calls",
            "ai-insights",
            "create-meeting",
        ):
            self.assertIn(cmd, r.stdout)

    def test_obsolete_team_specific_features_stay_removed(self):
        for attr in ("pr_summary", "alert_summary", "ALERT_RE", "PR_RE", "DEFAULT_ALIASES", "EMOJI_ALIASES"):
            self.assertFalse(hasattr(teams, attr), f"{attr} should not exist")
        self.assertFalse((REPO_ROOT / "examples" / "commands" / "announce-pr.md").exists())
        self.assertFalse((REPO_ROOT / "examples" / "commands" / "pr-queue.md").exists())
        self.assertFalse((REPO_ROOT / "examples" / "config.example.json").exists())
        self.assertTrue((REPO_ROOT / "examples" / "watchlist.example.json").exists())

        # Verify JWT claim extraction and multi-tenant watchlist loading
        payload = (
            base64.urlsafe_b64encode(
                json.dumps({"tid": "t-123", "oid": "u-456", "name": "Ada", "upn": "ada@example.com"}).encode()
            )
            .decode()
            .rstrip("=")
        )
        fake_jwt = f"eyJhbGciOiJub25lIn0.{payload}.sig"
        claims = teams.jwt_claims(fake_jwt)
        self.assertEqual(claims["tid"], "t-123")
        self.assertEqual(claims["oid"], "u-456")

        with tempfile.TemporaryDirectory() as tmp:
            wl_path = pathlib.Path(tmp) / "watchlist.json"
            wl_path.write_text(
                json.dumps(
                    {
                        "tenants": {
                            "t-123": [{"group": "Eng", "channels": [{"team": "Core", "names": ["General"]}]}],
                            "t-999": [{"group": "Other", "channels": [{"team": "Ops", "names": ["Alerts"]}]}],
                        }
                    }
                )
            )
            wl = teams.load_watchlist(wl_path, tenant_id="t-123")
            self.assertEqual(wl["watch"][0]["group"], "Eng")

    def test_custom_emoji_without_a_known_host_is_left_as_text(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(teams, "_CACHE_DIR", pathlib.Path(tmp)),
        ):
            teams._custom_emojis_cache = None
            no_host = base64.b64encode(b"id=0-nde-d2-42,type=1,url=").decode()
            raw = {
                "reactions": [
                    {
                        "reactionType": "custom",
                        "displayName": "lonely",
                        "reactionContentUrl": f"https://graph.microsoft.com/v1.0/x/hostedContents/{no_host}/$value",
                    }
                ]
            }
            self.assertEqual(teams.harvest_custom_emojis(raw), {})
            self.assertEqual(teams.expand_custom_emojis("<p>:lonely:</p>"), "<p>:lonely:</p>")

    def test_custom_emoji_host_is_learned_from_other_emojis(self):
        def reaction(name, payload):
            b64 = base64.b64encode(payload).decode()
            return {
                "reactions": [
                    {
                        "reactionType": "custom",
                        "displayName": name,
                        "reactionContentUrl": f"https://graph.microsoft.com/v1.0/x/hostedContents/{b64}/$value",
                    }
                ]
            }

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(teams, "_CACHE_DIR", pathlib.Path(tmp)),
        ):
            teams._custom_emojis_cache = None
            known = b"id=0-eu-d1-1,type=1,url=https://eu-prod.asyncgw.teams.microsoft.com/v1/objects/0-eu-d1-1/views/x"
            teams.harvest_custom_emojis(reaction("first", known))
            learned = teams.harvest_custom_emojis(reaction("second", b"id=0-eu-d1-2,type=1,url="))
            self.assertEqual(learned["second"]["host"], "eu-prod.asyncgw.teams.microsoft.com")

    def test_parse_ts_accepts_any_fraction_length(self):
        expected = dt.datetime(2026, 11, 10, 10, 0, 0, tzinfo=dt.timezone.utc)
        for raw in (
            "2026-11-10T10:00:00Z",
            "2026-11-10T10:00:00.0Z",
            "2026-11-10T10:00:00.000Z",
            "2026-11-10T10:00:00.0000000Z",
            "2026-11-10T10:00:00.0000000+00:00",
        ):
            self.assertEqual(teams.parse_ts(raw), expected, raw)
        self.assertEqual(teams.parse_ts("2026-11-10T10:00:00.1234567Z").microsecond, 123456)
        self.assertEqual(teams.parse_ts("2026-11-10T10:00:00.5Z").microsecond, 500000)

    def test_find_connection_only_returns_connections_owned_by_the_user(self):
        me = {"id": "u-me", "upn": "me@example.com"}

        def conn(name, owner_id, owner_upn, status="Connected"):
            return {
                "name": name,
                "properties": {
                    "createdBy": {"id": owner_id, "userPrincipalName": owner_upn},
                    "statuses": [{"status": status}],
                },
            }

        theirs = conn("theirs", "u-other", "other@example.com")
        mine = [conn("mine-broken", "u-me", "me@example.com", "Error"), conn("mine-ok", "u-me", "me@example.com")]
        with mock.patch.object(teams, "pp_get", return_value=(200, {"value": [theirs, *mine]})):
            self.assertEqual(teams.find_connection("Default-t", me)["name"], "mine-ok")
        with mock.patch.object(teams, "pp_get", return_value=(200, {"value": [theirs]})):
            self.assertIsNone(teams.find_connection("Default-t", me))

    def test_watchlist_tenant_block_without_a_match_warns_and_keeps_global_watch(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "watchlist.json"
            other = {"t-999": [{"group": "Other", "channels": []}]}
            path.write_text(json.dumps({"tenants": other}), encoding="utf-8")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(teams.load_watchlist(path, tenant_id="t-123"), {})
            self.assertIn("no entry for tenant t-123", err.getvalue())

            global_watch = [{"group": "Eng", "channels": []}]
            path.write_text(json.dumps({"watch": global_watch, "tenants": other}), encoding="utf-8")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(teams.load_watchlist(path, tenant_id="t-123")["watch"], global_watch)
            self.assertEqual(err.getvalue(), "")

    def test_create_meeting_body_matches_the_connector_schema(self):
        conn = mock.MagicMock()
        conn.op.return_value = {"id": "ev-1"}
        args = argparse.Namespace(
            subject="Sync",
            start="2026-11-12T15:00:00",
            end="2026-11-12T15:30:00",
            time_zone="W. Europe Standard Time",
            content=None,
            attendees=None,
            optional_attendees=None,
            calendar_id="Calendar",
            send=True,
        )
        with (
            mock.patch.object(teams, "connect", return_value=({}, conn)),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            teams.cmd_create_meeting(args)
        body = conn.op.call_args[0][2]
        self.assertTrue(body["isOnlineMeeting"])
        self.assertEqual(body["onlineMeetingProvider"], "teamsForBusiness")
        self.assertEqual(body["timeZone"], "W. Europe Standard Time")
        self.assertEqual(body["body"], {"content": "", "contentType": "html"})
        self.assertEqual(body["start"], {"dateTime": "2026-11-12T15:00:00"})
        self.assertEqual(body["end"], {"dateTime": "2026-11-12T15:30:00"})

    def test_create_chat_enforces_the_connector_user_limit(self):
        args = argparse.Namespace(users=",".join(f"u{i}@example.com" for i in range(21)), topic=None, send=False)
        user = {"name": "Ada", "email": "ada@example.com", "userId": "u-ada"}
        with (
            mock.patch.object(teams, "connect", return_value=({}, mock.MagicMock())),
            mock.patch.object(teams, "resolve_single_user", return_value=user),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as ctx,
        ):
            teams.cmd_create_chat(args)
        self.assertEqual(ctx.exception.code, 2)

    def test_refresh_bypasses_the_cache(self):
        self.addCleanup(teams.set_refresh, False)
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(teams, "_CACHE_DIR", pathlib.Path(tmp)),
        ):
            self.assertEqual(teams.cached("a.json", 3600, lambda: [1]), [1])
            self.assertEqual(teams.cached("a.json", 3600, lambda: [2]), [1])
            teams.set_refresh(True)
            self.assertEqual(teams.cached("a.json", 3600, lambda: [2]), [2])

    def test_write_private_bytes_keeps_the_bytes_untouched(self):
        data = b"line1\nline2\r\n\x00\xff\n"
        with tempfile.TemporaryDirectory() as tmp:
            path = teams.write_private_bytes(pathlib.Path(tmp) / "blob.bin", data)
            self.assertEqual(path.read_bytes(), data)

    def test_cache_roundtrip_is_independent_of_the_locale_encoding(self):
        code = (
            "import importlib.util, json, pathlib, sys, tempfile\n"
            "spec = importlib.util.spec_from_file_location('teams', sys.argv[1])\n"
            "t = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(t)\n"
            "t._CACHE_DIR = pathlib.Path(tempfile.mkdtemp())\n"
            "t.cached('x.json', 60, lambda: {'name': 'J\\u00f6rg \\U0001f44d'})\n"
            "print(json.dumps(t.cached('x.json', 60, lambda: {})))\n"
        )
        env = {**os.environ, "LC_ALL": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0"}
        r = subprocess.run([sys.executable, "-c", code, str(SCRIPT_PATH)], capture_output=True, env=env, check=False)
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
        self.assertEqual(json.loads(r.stdout), {"name": "J\u00f6rg \U0001f44d"})

    def test_cli_output_is_utf8_even_with_a_legacy_stdout_encoding(self):
        env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
        r = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "react", "--help"], capture_output=True, env=env, check=False
        )
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
        self.assertIn("\U0001f44d".encode(), r.stdout)


if __name__ == "__main__":
    unittest.main()
