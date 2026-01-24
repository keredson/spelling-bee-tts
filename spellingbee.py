import asyncio
import csv
import gzip
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import sysconfig
import site
import tempfile
import threading
import time
import urllib.request
from bisect import bisect_left
from pathlib import Path
import math
import random

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import GLib, Gtk, Gdk

import edge_tts


class SettingsStore:
    def __init__(self, app):
        self.app = app

    def _conn(self):
        return self.app.tracking_conn

    def get(self, key, default=None):
        try:
            row = self._conn().execute(
                "SELECT value FROM settings WHERE key = ?",
                (key,),
            ).fetchone()
        except (AttributeError, sqlite3.Error):
            return default
        if not row:
            return default
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            return row[0]

    def __setitem__(self, key, value):
        try:
            self._conn().execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, json.dumps(value)),
            )
            self._conn().commit()
        except (AttributeError, sqlite3.Error):
            return

    def pop(self, key, default=None):
        current = self.get(key, default)
        try:
            self._conn().execute("DELETE FROM settings WHERE key = ?", (key,))
            self._conn().commit()
        except (AttributeError, sqlite3.Error):
            return current
        return current


class SpellingBeeApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="org.pypi.project.spelling-bee-tts")
        GLib.set_application_name("Spelling Bee TTS")
        self.db_conn = None
        self.tracking_conn = None
        self.word_count = 0
        self.current_word = None
        self.correct = 0
        self.total = 0
        self.tts_lock = threading.Lock()
        self.edge_voice = os.environ.get("EDGE_TTS_VOICE", "en-US-AriaNeural")
        self.audio_cache = {}
        self.audio_dir = tempfile.TemporaryDirectory()
        self.llm = None
        self.llm_lock = threading.Lock()
        # "LLM_REPO_ID", "Qwen/Qwen2.5-1.5B-Instruct-GGUF"
        self.llm_model_repo = os.environ.get(
            "LLM_REPO_ID", "unsloth/Qwen3-4B-Instruct-2507-GGUF"
        )
        self.llm_model_filename = "Qwen3-4B-Instruct-2507-Q8_0.gguf"
        self.llm_model_path = None
        self.current_sentence = None
        self._sentence_generation_id = 0
        self.current_definition = None
        self._definition_generation_id = 0
        self.is_speaking = False
        self.is_defining = False
        self.is_generating_sentence = False
        self.profile_id = None
        self.profile_name = None
        self.profile_grade = None
        self.profile_window = None
        self.profile_dropdown = None
        self.profile_start_button = None
        self.profile_delete_button = None
        self.profile_history_button = None
        self.profile_ids = []
        self.profile_real_count = 0
        self.profile_create_marker = object()
        self.profile_placeholder_marker = object()
        self.profile_allow_close = False
        self.grade_levels = self.build_grade_levels()
        self.profile_rating = None
        self.profile_attempts = 0
        self.difficulty_mean = None
        self.difficulty_std = None
        self.difficulty_sorted = None
        self.word_sample_size = 1000
        self.word_sigma = 100.0
        self.awaiting_continue = False
        self.css_provider = None
        self.word_prompt_text = "Listen and type the spelling."
        self.entry_feedback_icon_name = None
        self.entry_icon_override = None
        self.profile_last_selected_index = None
        self.profile_create_confirmed = False
        self.settings = None

    def do_activate(self):
        self.window = Gtk.ApplicationWindow(application=self)
        version = self.get_current_version() or "dev"
        self.window.set_title(f"Spelling Bee TTS  v{version}")
        self.window.set_default_size(520, -1)
        self.window.connect("close-request", self.on_close_request)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        outer.set_margin_top(12)
        outer.set_margin_bottom(12)
        outer.set_margin_start(12)
        outer.set_margin_end(12)

        self.header = Gtk.Label(label="Loading word list...")
        self.header.set_xalign(0.0)

        self.score_label = Gtk.Label(label="Score: 0/0")
        self.score_label.set_xalign(0.0)

        self.word_label = Gtk.Label(label="")
        self.word_label.set_xalign(0.0)

        self.entry = Gtk.Entry()
        self.entry.set_placeholder_text("Type the word here...")
        self.entry.connect("activate", self.on_submit)
        self.ensure_css()

        self.submit_button = Gtk.Button(label="Check")
        self.submit_button.set_tooltip_text("Check spelling")
        self.submit_button.connect("clicked", self.on_submit)

        self.say_again_button = Gtk.Button()
        self.say_again_button.set_tooltip_text("Hear the word again")
        self.say_again_button.connect("clicked", self.on_say_again)
        self.say_again_label = Gtk.Label(label="Say it Again")
        self.say_again_spinner = Gtk.Spinner()
        self.say_again_spinner.set_visible(False)
        say_again_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        say_again_box.append(self.say_again_label)
        say_again_box.append(self.say_again_spinner)
        self.say_again_button.set_child(say_again_box)

        self.sentence_button = Gtk.Button()
        self.sentence_button.set_tooltip_text("Hear an example sentence")
        self.sentence_button.connect("clicked", self.on_use_sentence)
        self.sentence_label = Gtk.Label(label="Use in a Sentence")
        self.sentence_spinner = Gtk.Spinner()
        self.sentence_spinner.set_visible(False)
        sentence_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        sentence_box.append(self.sentence_label)
        sentence_box.append(self.sentence_spinner)
        self.sentence_button.set_child(sentence_box)
        self.sentence_button.set_sensitive(False)

        self.define_button = Gtk.Button()
        self.define_button.set_tooltip_text("Hear a simple definition")
        self.define_button.connect("clicked", self.on_define)
        self.define_label = Gtk.Label(label="Define")
        self.define_spinner = Gtk.Spinner()
        self.define_spinner.set_visible(False)
        define_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        define_box.append(self.define_label)
        define_box.append(self.define_spinner)
        self.define_button.set_child(define_box)
        self.define_button.set_sensitive(False)

        self.button_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.button_row.set_halign(Gtk.Align.FILL)
        self.button_row.append(self.submit_button)
        button_spacer = Gtk.Box()
        button_spacer.set_hexpand(True)
        self.button_row.append(button_spacer)
        self.button_row.append(self.say_again_button)
        self.button_row.append(self.define_button)
        self.button_row.append(self.sentence_button)

        outer.append(self.header)
        outer.append(self.word_label)
        outer.append(self.entry)
        outer.append(self.button_row)
        outer.append(self.score_label)

        self.entry.set_visible(False)
        self.button_row.set_visible(False)
        self.score_label.set_visible(False)

        self.window.set_child(outer)
        self.window.present()
        self.init_tracking_db()
        self.show_profile_selector()
        self.check_system_dependencies(self.window)
        self.maybe_check_for_updates()

    def on_close_request(self, _window):
        if self.db_conn:
            self.db_conn.close()
        if self.tracking_conn:
            self.tracking_conn.close()
        self.quit()
        return False

    def load_default_words(self):
        path = self.find_words_csv()
        if not path:
            self.word_label.set_text("words.csv.gz not found.")
            return
        try:
            self.db_conn = self.build_words_db(path)
        except Exception as exc:
            self.word_label.set_text(f"Failed to load words.csv.gz: {exc}")
            return

        self.correct = 0
        self.total = 0
        self.update_score()
        self.header.set_visible(False)
        self.entry.set_visible(True)
        self.button_row.set_visible(True)
        self.score_label.set_visible(True)
        self.entry.grab_focus()
        self.next_word()
        self.update_window_height()

    def init_tracking_db(self):
        data_path = self.get_data_path()
        try:
            data_path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.show_error_dialog("Data directory error", str(exc))
            return
        db_path = data_path / "spellingbee.db"
        print('using db:', db_path)
        try:
            self.tracking_conn = sqlite3.connect(str(db_path))
            self.tracking_conn.execute("PRAGMA foreign_keys = ON")
            self.ensure_tracking_schema()
            self.settings = SettingsStore(self)
        except sqlite3.Error as exc:
            self.show_error_dialog("Database error", str(exc))

    def ensure_tracking_schema(self):
        if not self.tracking_conn:
            return
        version = self.tracking_conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            self.tracking_conn.execute(
                """
                CREATE TABLE profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    grade_level INTEGER,
                    ability_rating REAL,
                    attempts_count INTEGER NOT NULL DEFAULT 0,
                    ability_updated_at INTEGER,
                    created_at INTEGER NOT NULL
                )
                """
            )
            self.tracking_conn.execute(
                """
                CREATE TABLE attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    profile_id INTEGER NOT NULL,
                    word TEXT NOT NULL,
                    attempt TEXT NOT NULL,
                    edit_distance INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY(profile_id) REFERENCES profiles(id) ON DELETE CASCADE
                )
                """
            )
            self.tracking_conn.execute(
                "CREATE INDEX attempts_profile_time ON attempts(profile_id, created_at)"
            )
            self.tracking_conn.execute(
                """
                CREATE TABLE settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            self.tracking_conn.execute("PRAGMA user_version = 4")
            self.tracking_conn.commit()
        elif version == 1:
            self.migrate_profiles_to_int()
            self.migrate_add_profile_ratings()
            self.migrate_add_settings()
        elif version == 2:
            self.migrate_add_profile_ratings()
            self.migrate_add_settings()
        elif version == 3:
            self.migrate_add_settings()

    def migrate_profiles_to_int(self):
        if not self.tracking_conn:
            return
        try:
            self.tracking_conn.execute("PRAGMA foreign_keys = OFF")
            self.tracking_conn.execute(
                """
                CREATE TABLE profiles_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    grade_level INTEGER,
                    created_at INTEGER NOT NULL
                )
                """
            )
            rows = self.tracking_conn.execute(
                "SELECT id, name, grade_level, created_at FROM profiles"
            ).fetchall()
            converted = []
            for profile_id, name, grade_text, created_at in rows:
                grade_int = self.parse_grade_level(grade_text)
                converted.append((profile_id, name, grade_int, created_at))
            self.tracking_conn.executemany(
                "INSERT INTO profiles_new (id, name, grade_level, created_at) VALUES (?, ?, ?, ?)",
                converted,
            )
            self.tracking_conn.execute("DROP TABLE profiles")
            self.tracking_conn.execute("ALTER TABLE profiles_new RENAME TO profiles")
            self.tracking_conn.execute("PRAGMA user_version = 2")
            self.tracking_conn.commit()
        finally:
            self.tracking_conn.execute("PRAGMA foreign_keys = ON")

    def migrate_add_profile_ratings(self):
        if not self.tracking_conn:
            return
        try:
            self.tracking_conn.execute(
                "ALTER TABLE profiles ADD COLUMN ability_rating REAL"
            )
        except sqlite3.Error:
            pass
        try:
            self.tracking_conn.execute(
                "ALTER TABLE profiles ADD COLUMN attempts_count INTEGER NOT NULL DEFAULT 0"
            )
        except sqlite3.Error:
            pass
        try:
            self.tracking_conn.execute(
                "ALTER TABLE profiles ADD COLUMN ability_updated_at INTEGER"
            )
        except sqlite3.Error:
            pass
        rows = self.tracking_conn.execute(
            "SELECT id, grade_level FROM profiles"
        ).fetchall()
        for profile_id, grade_level in rows:
            rating = self.grade_to_rating(grade_level)
            self.tracking_conn.execute(
                """
                UPDATE profiles
                SET ability_rating = COALESCE(ability_rating, ?),
                    attempts_count = COALESCE(attempts_count, 0),
                    ability_updated_at = COALESCE(ability_updated_at, created_at)
                WHERE id = ?
                """,
                (rating, profile_id),
            )
        self.tracking_conn.execute("PRAGMA user_version = 3")
        self.tracking_conn.commit()

    def migrate_add_settings(self):
        if not self.tracking_conn:
            return
        try:
            self.tracking_conn.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            self.tracking_conn.execute("PRAGMA user_version = 4")
            self.tracking_conn.commit()
        except sqlite3.Error:
            return

    def load_profiles(self):
        if not self.tracking_conn:
            return []
        rows = self.tracking_conn.execute(
            "SELECT id, name, grade_level, ability_rating FROM profiles ORDER BY name"
        ).fetchall()
        profiles = []
        for profile_id, name, grade_level, ability_rating in rows:
            profiles.append(
                {
                    "id": profile_id,
                    "name": name,
                    "grade_level": grade_level,
                    "ability_rating": ability_rating,
                }
            )
        return profiles

    def show_profile_selector(self):
        if self.profile_window:
            self.profile_window.present()
            return
        window = Gtk.Window(transient_for=self.window, modal=True, title="Select profile")
        window.set_default_size(360, -1)
        window.connect("close-request", self.on_profile_close_request)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)

        label = Gtk.Label(label="Choose a profile to begin.")
        label.set_xalign(0.0)
        box.append(label)

        self.profile_dropdown = Gtk.DropDown()
        self.profile_dropdown.connect("notify::selected", self.on_profile_selection_changed)
        box.append(self.profile_dropdown)

        button_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        button_row.set_halign(Gtk.Align.FILL)

        self.profile_delete_button = Gtk.Button()
        self.profile_delete_button.set_tooltip_text("Delete selected profile")
        delete_icon = Gtk.Image.new_from_icon_name("user-trash-symbolic")
        self.profile_delete_button.set_child(delete_icon)
        self.profile_delete_button.set_halign(Gtk.Align.START)
        self.profile_delete_button.connect("clicked", self.on_delete_profile_clicked)
        self.profile_history_button = Gtk.Button()
        self.profile_history_button.set_tooltip_text("View attempt history")
        history_icon = Gtk.Image.new_from_icon_name("view-history-symbolic")
        self.profile_history_button.set_child(history_icon)
        self.profile_history_button.connect("clicked", self.on_history_clicked)
        self.profile_start_button = Gtk.Button()
        self.profile_start_button.set_tooltip_text("Start game with selected profile")
        start_icon = Gtk.Image.new_from_icon_name("media-playback-start-symbolic")
        start_label = Gtk.Label(label="Start Game")
        start_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        start_box.append(start_icon)
        start_box.append(start_label)
        self.profile_start_button.set_child(start_box)
        self.profile_start_button.add_css_class("start-button")
        self.profile_start_button.connect("clicked", self.on_profile_start_clicked)

        button_row.append(self.profile_delete_button)
        button_spacer = Gtk.Box()
        button_spacer.set_hexpand(True)
        button_row.append(button_spacer)
        button_row.append(self.profile_history_button)
        button_row.append(self.profile_start_button)
        box.append(button_row)

        window.set_child(box)
        window.present()
        self.profile_window = window
        self.refresh_profile_dropdown()
        if self.profile_real_count == 0:
            self.on_create_profile_clicked(None)

    def refresh_profile_dropdown(self):
        if not self.profile_dropdown:
            return
        profiles = self.load_profiles()
        self.profile_real_count = len(profiles)
        self.profile_ids = [self.profile_placeholder_marker]
        labels = []
        labels.append("Select a profile...")
        for profile in profiles:
            name = profile["name"]
            ability_rating = profile.get("ability_rating")
            if ability_rating is not None:
                grade_text = self.format_grade_label(
                    self.rating_to_grade_level(ability_rating)
                )
            else:
                grade_text = self.format_grade_label(profile["grade_level"])
            if grade_text:
                labels.append(f"{name} ({grade_text})")
            else:
                labels.append(name)
            self.profile_ids.append(profile["id"])
        labels.append("Create new profile...")
        self.profile_ids.append(self.profile_create_marker)
        model = Gtk.StringList.new(labels)
        self.profile_dropdown.set_model(model)
        if profiles:
            selected_index = 1
            last_profile_id = self.settings.get("last_profile_id")
            if last_profile_id in self.profile_ids:
                selected_index = self.profile_ids.index(last_profile_id)
            self.profile_dropdown.set_selected(selected_index)
            self.profile_start_button.set_sensitive(True)
            if self.profile_history_button:
                self.profile_history_button.set_sensitive(True)
        else:
            self.profile_dropdown.set_selected(0)
            self.profile_start_button.set_sensitive(False)
            if self.profile_history_button:
                self.profile_history_button.set_sensitive(False)
        if self.profile_delete_button:
            self.profile_delete_button.set_sensitive(bool(profiles))

    def on_profile_selection_changed(self, _dropdown, _param):
        if not self.profile_delete_button:
            return
        if not self.profile_dropdown:
            return
        index = self.profile_dropdown.get_selected()
        if index < 0 or index >= len(self.profile_ids):
            self.profile_delete_button.set_sensitive(False)
            self.profile_start_button.set_sensitive(False)
            if self.profile_history_button:
                self.profile_history_button.set_sensitive(False)
            return
        profile_id = self.profile_ids[index]
        if profile_id is self.profile_placeholder_marker:
            self.profile_delete_button.set_sensitive(False)
            self.profile_start_button.set_sensitive(False)
            if self.profile_history_button:
                self.profile_history_button.set_sensitive(False)
            return
        if profile_id is self.profile_create_marker:
            self.profile_delete_button.set_sensitive(False)
            self.profile_start_button.set_sensitive(False)
            if self.profile_history_button:
                self.profile_history_button.set_sensitive(False)
            self.on_create_profile_clicked(None)
            return
        self.profile_last_selected_index = index
        self.profile_delete_button.set_sensitive(True)
        self.profile_start_button.set_sensitive(True)
        if self.profile_history_button:
            self.profile_history_button.set_sensitive(True)

    def on_profile_close_request(self, _window):
        if self.profile_allow_close:
            return False
        self.quit()
        return False

    def on_profile_start_clicked(self, _button):
        if not self.profile_ids:
            return
        index = self.profile_dropdown.get_selected()
        if index < 0 or index >= len(self.profile_ids):
            return
        profile_id = self.profile_ids[index]
        if profile_id in (self.profile_placeholder_marker, self.profile_create_marker):
            return
        row = self.tracking_conn.execute(
            "SELECT name, grade_level, ability_rating, attempts_count FROM profiles WHERE id = ?",
            (profile_id,),
        ).fetchone()
        if row:
            self.profile_id = profile_id
            self.profile_name = row[0]
            self.profile_grade = row[1]
            self.profile_rating = row[2]
            self.profile_attempts = row[3] or 0
            self.settings["last_profile_id"] = profile_id
            if self.profile_rating is None:
                self.profile_rating = self.grade_to_rating(self.profile_grade)
                self.tracking_conn.execute(
                    """
                    UPDATE profiles
                    SET ability_rating = ?, ability_updated_at = ?
                    WHERE id = ?
                    """,
                    (self.profile_rating, int(time.time()), self.profile_id),
                )
                self.tracking_conn.commit()
        if self.profile_window:
            self.profile_allow_close = True
            self.profile_window.close()
            self.profile_window = None
            self.profile_allow_close = False
        self.load_default_words()

    def on_history_clicked(self, _button):
        if not self.tracking_conn or not self.profile_dropdown:
            self.show_error_dialog("History", "No profile selected.")
            return
        index = self.profile_dropdown.get_selected()
        if index < 0 or index >= len(self.profile_ids):
            self.show_error_dialog("History", "No profile selected.")
            return
        profile_id = self.profile_ids[index]
        if profile_id in (self.profile_placeholder_marker, self.profile_create_marker):
            self.show_error_dialog("History", "No profile selected.")
            return
        rows = self.tracking_conn.execute(
            """
            SELECT word, attempt, edit_distance, created_at
            FROM attempts
            WHERE profile_id = ?
            ORDER BY created_at DESC
            """,
            (profile_id,),
        ).fetchall()
        dialog = Gtk.Window(
            transient_for=self.profile_window or self.window,
            modal=True,
            title="Attempt history",
        )
        dialog.set_default_size(520, 360)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)

        header = Gtk.Label(label=f"Attempts: {len(rows)}")
        header.set_xalign(0.0)
        box.append(header)

        size_groups = [
            Gtk.SizeGroup.new(Gtk.SizeGroupMode.HORIZONTAL) for _ in range(4)
        ]
        header_grid = Gtk.Grid()
        header_grid.set_column_spacing(12)
        headers = ["When", "Word", "Your Spelling", "Accuracy"]
        for col, text in enumerate(headers):
            label = Gtk.Label(label=text)
            label.set_xalign(0.0)
            header_grid.attach(label, col, 0, 1, 1)
            size_groups[col].add_widget(label)
        box.append(header_grid)

        list_box = Gtk.ListBox()
        list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        for word, attempt, edit_distance, created_at in rows:
            when = time.strftime("%x %X", time.localtime(created_at))
            length = max(len(word), 1)
            accuracy = max(0.0, 1.0 - (edit_distance / length))
            accuracy_text = f"{int(round(accuracy * 100))}%"
            row_grid = Gtk.Grid()
            row_grid.set_column_spacing(12)
            row_labels = [when, word, attempt, accuracy_text]
            for col, text in enumerate(row_labels):
                label = Gtk.Label(label=str(text))
                label.set_xalign(0.0)
                row_grid.attach(label, col, 0, 1, 1)
                size_groups[col].add_widget(label)
            list_row = Gtk.ListBoxRow()
            list_row.set_child(row_grid)
            list_box.append(list_row)

        scroller = Gtk.ScrolledWindow()
        scroller.set_hexpand(True)
        scroller.set_vexpand(True)
        scroller.set_child(list_box)
        box.append(scroller)

        close_button = Gtk.Button(label="Close")
        close_button.set_tooltip_text("Close this window")
        close_button.connect("clicked", lambda _b: dialog.close())
        close_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        close_row.set_halign(Gtk.Align.END)
        close_row.append(close_button)
        box.append(close_row)

        dialog.set_child(box)
        dialog.present()

    def on_create_profile_clicked(self, _button):
        self.profile_create_confirmed = False
        dialog = Gtk.Window(
            transient_for=self.profile_window or self.window,
            modal=True,
            title="Create profile",
        )
        dialog.connect("close-request", self.on_create_profile_cancel)
        dialog.set_default_size(320, -1)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)

        name_label = Gtk.Label(label="Name")
        name_label.set_xalign(0.0)
        name_entry = Gtk.Entry()
        name_entry.set_placeholder_text("Student name")

        grade_label = Gtk.Label(label="Grade level")
        grade_label.set_xalign(0.0)
        grade_dropdown = Gtk.DropDown()
        grade_dropdown.set_model(Gtk.StringList.new([label for _value, label in self.grade_levels]))
        grade_dropdown.set_selected(0)

        button_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        button_row.set_halign(Gtk.Align.END)
        cancel_button = Gtk.Button(label="Cancel")
        create_button = Gtk.Button(label="Create")
        cancel_button.set_tooltip_text("Cancel profile creation")
        create_button.set_tooltip_text("Create the profile")

        cancel_button.connect("clicked", lambda _b: dialog.close())
        name_entry.connect(
            "activate",
            self.on_create_profile_confirm,
            dialog,
            name_entry,
            grade_dropdown,
        )
        create_button.connect(
            "clicked",
            self.on_create_profile_confirm,
            dialog,
            name_entry,
            grade_dropdown,
        )

        button_row.append(cancel_button)
        button_row.append(create_button)

        box.append(name_label)
        box.append(name_entry)
        box.append(grade_label)
        box.append(grade_dropdown)
        box.append(button_row)

        dialog.set_child(box)
        dialog.present()
        name_entry.grab_focus()

    def on_delete_profile_clicked(self, _button):
        if not self.profile_ids:
            return
        index = self.profile_dropdown.get_selected()
        if index < 0 or index >= len(self.profile_ids):
            return
        profile_id = self.profile_ids[index]
        if profile_id in (self.profile_placeholder_marker, self.profile_create_marker):
            return
        row = self.tracking_conn.execute(
            "SELECT name FROM profiles WHERE id = ?",
            (profile_id,),
        ).fetchone()
        if not row:
            return
        name = row[0]

        dialog = Gtk.AlertDialog()
        dialog.set_message("Delete profile?")
        dialog.set_detail(f"This will remove {name} and all attempts.")
        dialog.set_buttons(["Delete", "Cancel"])
        dialog.choose(
            self.profile_window or self.window,
            None,
            self.on_delete_profile_response,
            profile_id,
        )

    def on_delete_profile_response(self, dialog, async_result, profile_id):
        try:
            response = dialog.choose_finish(async_result)
        except Exception:
            return
        if response != 0:
            return
        try:
            self.tracking_conn.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
            self.tracking_conn.commit()
        except sqlite3.Error as exc:
            self.show_error_dialog("Profile error", str(exc))
            return
        if self.settings.get("last_profile_id") == profile_id:
            self.settings.pop("last_profile_id", None)
        self.refresh_profile_dropdown()

    def on_create_profile_confirm(self, _button, dialog, name_entry, grade_dropdown):
        name = name_entry.get_text().strip()
        if not name:
            self.show_error_dialog("Profile error", "Please enter a name.")
            return
        selected = grade_dropdown.get_selected()
        if selected < 0 or selected >= len(self.grade_levels):
            self.show_error_dialog("Profile error", "Please select a grade level.")
            return
        grade = self.grade_levels[selected][0]
        try:
            cursor = self.tracking_conn.execute(
                """
                INSERT INTO profiles (name, grade_level, ability_rating, attempts_count, ability_updated_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    grade,
                    self.grade_to_rating(grade),
                    0,
                    int(time.time()),
                    int(time.time()),
                ),
            )
            self.tracking_conn.commit()
            new_profile_id = cursor.lastrowid
        except sqlite3.IntegrityError:
            self.show_error_dialog("Profile error", "That name already exists.")
            return
        except sqlite3.Error as exc:
            self.show_error_dialog("Profile error", str(exc))
            return

        self.profile_create_confirmed = True
        dialog.close()
        self.refresh_profile_dropdown()
        if new_profile_id in self.profile_ids:
            self.profile_dropdown.set_selected(self.profile_ids.index(new_profile_id))

    def on_create_profile_cancel(self, _dialog):
        if self.profile_create_confirmed:
            return False
        if not self.profile_dropdown:
            return False
        index = self.profile_last_selected_index
        if index is not None and 0 <= index < len(self.profile_ids):
            if self.profile_ids[index] not in (
                self.profile_placeholder_marker,
                self.profile_create_marker,
            ):
                self.profile_dropdown.set_selected(index)
                return False
        self.profile_dropdown.set_selected(0)
        if self.profile_delete_button:
            self.profile_delete_button.set_sensitive(False)
        if self.profile_start_button:
            self.profile_start_button.set_sensitive(False)
        return False

    def find_words_csv(self):
        data_roots = [sysconfig.get_paths().get("data"), site.USER_BASE]
        xdg_data_home = os.environ.get("XDG_DATA_HOME")
        xdg_data_dirs = os.environ.get("XDG_DATA_DIRS", "")
        candidates = [
            Path.cwd() / "words.csv.gz",
            Path(__file__).resolve().parent / "words.csv.gz",
        ]
        for data_root in data_roots:
            if data_root:
                candidates.append(
                    Path(data_root) / "share" / "spellingbee" / "words.csv.gz"
                )
        if xdg_data_home:
            candidates.append(Path(xdg_data_home) / "spellingbee" / "words.csv.gz")
        for data_root in [p for p in xdg_data_dirs.split(":") if p]:
            candidates.append(Path(data_root) / "spellingbee" / "words.csv.gz")
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return None

    def build_words_db(self, path):
        conn = sqlite3.connect(":memory:")
        conn.execute(
            """
            CREATE TABLE words (
                word TEXT PRIMARY KEY,
                difficulty REAL,
                coverage REAL,
                cumulative_coverage REAL
            )
            """
        )
        with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            rows = []
            for row in reader:
                word = (row.get("word") or "").strip()
                if not word:
                    continue
                rows.append(
                    (
                        word,
                        self.parse_float(row.get("difficulty")),
                        self.parse_float(row.get("coverage")),
                        self.parse_float(row.get("cumulative_coverage")),
                    )
                )
                if len(rows) >= 1000:
                    conn.executemany(
                        "INSERT OR IGNORE INTO words VALUES (?, ?, ?, ?)", rows
                    )
                    rows.clear()
            if rows:
                conn.executemany(
                    "INSERT OR IGNORE INTO words VALUES (?, ?, ?, ?)", rows
                )
        self.word_count = conn.execute(
            "SELECT COUNT(*) FROM words"
        ).fetchone()[0]
        self.compute_difficulty_stats(conn)
        return conn

    def compute_difficulty_stats(self, conn):
        row = conn.execute(
            "SELECT AVG(difficulty), AVG(difficulty * difficulty) FROM words WHERE difficulty IS NOT NULL"
        ).fetchone()
        if not row:
            return
        mean = row[0]
        mean_sq = row[1]
        if mean is None or mean_sq is None:
            return
        variance = max(0.0, mean_sq - mean * mean)
        std = math.sqrt(variance)
        self.difficulty_mean = mean
        self.difficulty_std = std if std > 0 else None
        rows = conn.execute(
            "SELECT difficulty FROM words WHERE difficulty IS NOT NULL"
        ).fetchall()
        self.difficulty_sorted = sorted(row[0] for row in rows)

    def parse_float(self, value):
        if value is None or value == "":
            return None
        try:
            return float(value)
        except ValueError:
            return None

    def next_word(self):
        if not self.db_conn:
            self.word_label.set_text("Word list not loaded.")
            return

        self.awaiting_continue = False
        self.entry.set_editable(True)
        self.submit_button.set_label("Check")
        self.set_entry_feedback_icon(None)
        word = self.pick_next_word()
        if not word:
            self.word_label.set_text("No words available.")
            return

        self.current_word = word
        self.current_sentence = None
        self.current_definition = None
        self.update_define_button_state()
        self.update_sentence_button_state()
        self.entry.set_text("")
        difficulty = self.get_word_difficulty(self.current_word)
        word_rating = self.word_rating_from_difficulty(difficulty)
        self.word_prompt_text = (
            f"Listen and type the spelling.  (word difficulty: {int(word_rating)})"
        )
        self.word_label.set_text(self.word_prompt_text)
        self.focus_entry()
        self.speak("Please spell: " + self.current_word)
        self.prefetch_definition(self.current_word, allow_download=False)
        self.prefetch_sentence(self.current_word, allow_download=False)

    def pick_next_word(self):
        if not self.db_conn:
            return None
        if not self.profile_rating:
            row = self.db_conn.execute(
                "SELECT word FROM words ORDER BY RANDOM() LIMIT 1"
            ).fetchone()
            return row[0] if row else None
        candidate_limit = min(self.word_sample_size, self.word_count)
        rows = self.db_conn.execute(
            """
            SELECT word, difficulty
            FROM words
            ORDER BY RANDOM()
            LIMIT ?
            """,
            (candidate_limit,),
        ).fetchall()
        if not rows:
            return None
        attempt_info = self.fetch_attempt_info([row[0] for row in rows])
        now = int(time.time())
        weights = []
        total = 0.0
        for word, difficulty in rows:
            word_rating = self.word_rating_from_difficulty(difficulty)
            gap = abs(word_rating - self.profile_rating)
            base = math.exp(-(gap * gap) / (2.0 * self.word_sigma * self.word_sigma))
            weight = 0.1 + 0.9 * base
            info = attempt_info.get(word)
            if info:
                if info["edit_distance"] > 0:
                    weight *= 1.35
                else:
                    weight *= 0.75
            else:
                weight *= 1.15
            total += weight
            weights.append((word, total))
        if total <= 0:
            return rows[0][0]
        pick = random.random() * total
        for word, cumulative in weights:
            if pick <= cumulative:
                return word
        return rows[-1][0]

    def fetch_attempt_info(self, words):
        if not self.tracking_conn or not self.profile_id or not words:
            return {}
        placeholders = ",".join("?" for _ in words)
        query = f"""
            WITH last AS (
                SELECT word, MAX(created_at) AS last_seen
                FROM attempts
                WHERE profile_id = ? AND word IN ({placeholders})
                GROUP BY word
            )
            SELECT a.word, l.last_seen, a.edit_distance
            FROM attempts a
            JOIN last l ON l.word = a.word AND l.last_seen = a.created_at
            WHERE a.profile_id = ?
        """
        params = [self.profile_id, *words, self.profile_id]
        rows = self.tracking_conn.execute(query, params).fetchall()
        info = {}
        for word, last_seen, edit_distance in rows:
            info[word] = {"last_seen": last_seen, "edit_distance": edit_distance}
        return info

    def on_submit(self, _widget):
        if self.awaiting_continue:
            self.awaiting_continue = False
            self.entry.set_editable(True)
            self.submit_button.set_label("Check")
            self.set_entry_feedback_icon(None)
            self.next_word()
            return
        if not self.current_word:
            return

        guess = self.entry.get_text().strip()
        if not guess:
            return

        self.total += 1
        self.log_attempt(guess)
        if guess.lower() == self.current_word.lower():
            self.correct += 1
            self.set_entry_feedback_icon("emblem-ok-symbolic")
            self.word_label.set_text("Correct! Next word...")
            self.speak("That is correct!", on_done=self.after_feedback)
        else:
            self.set_entry_feedback_icon("dialog-error-symbolic")
            self.word_label.set_text(f"Incorrect. It was: {self.current_word}")
            self.awaiting_continue = True
            self.entry.set_editable(False)
            self.submit_button.set_label("Continue")
            self.speak(
                f"Sorry, that is not correct, the correct spelling is: {'. '.join(self.current_word)}",
            )

        self.update_score()

    def set_entry_feedback_icon(self, icon_name):
        self.entry_feedback_icon_name = icon_name
        if self.entry_icon_override:
            return
        self.entry.set_icon_from_icon_name(
            Gtk.EntryIconPosition.SECONDARY, icon_name
        )
        self.entry.remove_css_class("correct")
        self.entry.remove_css_class("incorrect")
        if icon_name == "emblem-ok-symbolic":
            self.entry.add_css_class("correct")
        elif icon_name == "dialog-error-symbolic":
            self.entry.add_css_class("incorrect")

    def set_entry_icon_override(self, icon_name):
        self.entry_icon_override = icon_name
        if icon_name:
            self.entry.set_icon_from_icon_name(
                Gtk.EntryIconPosition.SECONDARY, icon_name
            )
            self.entry.remove_css_class("correct")
            self.entry.remove_css_class("incorrect")
            return
        self.set_entry_feedback_icon(self.entry_feedback_icon_name)

    def ensure_css(self):
        if self.css_provider:
            return
        provider = Gtk.CssProvider()
        provider.load_from_data(
            b"""
            entry.correct image {
                color: #2e7d32;
            }
            entry.incorrect image {
                color: #c62828;
            }
            """
        )
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        self.css_provider = provider

    def after_feedback(self):
        self.next_word()
        return False

    def on_say_again(self, _button):
        if self.current_word:
            self.speak(self.current_word)
            self.focus_entry()

    def on_use_sentence(self, _button):
        if not self.current_word:
            return

        if self.current_sentence:
            self.speak(self.current_sentence)
            self.focus_entry()
            return
        self.prefetch_sentence(self.current_word, allow_download=True)

    def on_define(self, _button):
        if not self.current_word:
            return

        if self.current_definition:
            self.speak(self.current_definition)
            self.focus_entry()
            return
        self.prefetch_definition(self.current_word, allow_download=True)

    def update_score(self):
        rating_text = self.format_estimated_level()
        if rating_text:
            self.score_label.set_text(f"Rank: {int(self.profile_rating)} - {rating_text}")
        else:
            self.score_label.set_text(f"Rank: {int(self.profile_rating)}")

    def speak(self, text, on_done=None):
        mp3_players = self.pick_mp3_players()
        if not mp3_players:
            self.show_error_dialog(
                "Audio error",
                "Install mpv, ffplay, or mpg123 to play TTS audio.",
            )
            return

        def run():
            with self.tts_lock:
                GLib.idle_add(self.set_say_again_busy, True)
                success = False
                try:
                    cached_path = self.audio_cache.get(text)
                    if cached_path and Path(cached_path).exists():
                        ok = self.play_audio(mp3_players, cached_path)
                    else:
                        output_path = Path(self.audio_dir.name) / f"{len(self.audio_cache)}.mp3"
                        asyncio.run(
                            edge_tts.Communicate(
                                text, voice=self.edge_voice
                            ).save(str(output_path))
                        )
                        size = output_path.stat().st_size
                        if size == 0:
                            GLib.idle_add(
                                self.show_error_dialog,
                                "Audio error",
                                "TTS produced empty audio. Check network access.",
                            )
                            return
                        self.audio_cache[text] = str(output_path)
                        ok = self.play_audio(mp3_players, str(output_path))

                    if not ok:
                        GLib.idle_add(
                            self.show_error_dialog,
                            "Audio error",
                            "Audio playback failed. Check your sound device.",
                        )
                    else:
                        success = True
                except Exception as exc:
                    GLib.idle_add(
                        self.show_error_dialog,
                        "Audio error",
                        f"TTS failed: {exc}",
                    )
                finally:
                    GLib.idle_add(self.set_say_again_busy, False)
                    if on_done:
                        GLib.idle_add(on_done)

        threading.Thread(target=run, daemon=True).start()

    def get_data_path(self):
        data_home = os.environ.get("XDG_DATA_HOME")
        if data_home:
            return Path(data_home) / "spellingbee"
        return Path.home() / ".local" / "share" / "spellingbee"

    def build_grade_levels(self):
        levels = [(0, "Kindergarten")]
        levels.extend([(grade, 'Grade %i' % grade) for grade in range(1, 13)])
        levels.extend(
            [
                (13, "College Freshman"),
                (14, "College Sophomore"),
                (15, "College Junior"),
                (16, "College Senior"),
            ]
        )
        return levels

    def format_grade_label(self, grade_level):
        if grade_level is None:
            return ""
        if grade_level == 0:
            return "Kindergarten"
        if 1 <= grade_level <= 12:
            return 'Grade %i' % grade_level
        college = {
            13: "College Freshman",
            14: "College Sophomore",
            15: "College Junior",
            16: "College Senior",
        }
        return college.get(grade_level, str(grade_level))

    def parse_grade_level(self, value):
        if value is None:
            return None
        text = str(value).strip().lower()
        if not text:
            return None
        if text in {"k", "kindergarten"}:
            return 0
        if text.isdigit():
            return int(text)
        if "freshman" in text:
            return 13
        if "sophomore" in text:
            return 14
        if "junior" in text:
            return 15
        if "senior" in text:
            return 16
        return None

    def pick_mp3_players(self):
        players = []
        for candidate in ("mpv", "ffplay", "mpg123"):
            found = shutil.which(candidate)
            if found:
                players.append(found)
        return players

    def play_audio(self, players, path):
        for player in players:
            if player.endswith("mpv"):
                cmd = [player, "--no-video", "--quiet", path]
            elif player.endswith("ffplay"):
                cmd = [player, "-nodisp", "-autoexit", "-loglevel", "error", path]
            elif player.endswith("mpg123"):
                cmd = [player, "-q", path]
            else:
                cmd = [player, path]
            result = subprocess.run(cmd, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if result.returncode == 0:
                return True
        return False

    def set_say_again_busy(self, busy):
        self.is_speaking = busy
        self.say_again_button.set_sensitive(not busy)
        self.update_define_button_state()
        self.update_sentence_button_state()
        self.say_again_spinner.set_visible(False)
        #self.say_again_label.set_text("Speaking..." if busy else "Say it Again")
        if busy:
            if not self.entry_feedback_icon_name:
                self.set_entry_icon_override("audio-volume-high-symbolic")
        else:
            self.set_entry_icon_override(None)
        if busy:
            self.say_again_spinner.start()
        else:
            self.say_again_spinner.stop()

    def set_sentence_busy(self, busy):
        self.is_generating_sentence = busy
        self.sentence_spinner.set_visible(busy)
        #self.sentence_label.set_text("Generating sentence..." if busy else "Use in a Sentence")
        if busy:
            self.sentence_spinner.start()
        else:
            self.sentence_spinner.stop()
        self.update_sentence_button_state()

    def set_define_busy(self, busy):
        self.is_defining = busy
        self.define_spinner.set_visible(busy)
        #self.define_label.set_text("Generating definition..." if busy else "Define")
        if busy:
            self.define_spinner.start()
        else:
            self.define_spinner.stop()
        self.update_define_button_state()

    def update_define_button_state(self):
        if not getattr(self, "define_button", None):
            return
        enabled = (
            self.current_definition is not None
            and not self.is_defining
            and not self.is_speaking
        )
        self.define_button.set_sensitive(enabled)

    def update_sentence_button_state(self):
        if not getattr(self, "sentence_button", None):
            return
        enabled = (
            self.current_sentence is not None
            and not self.is_generating_sentence
            and not self.is_speaking
        )
        self.sentence_button.set_sensitive(enabled)
    def log_attempt(self, guess):
        if not self.tracking_conn or not self.profile_id or not self.current_word:
            return
        distance = self.edit_distance(guess.lower(), self.current_word.lower())
        try:
            self.tracking_conn.execute(
                """
                INSERT INTO attempts (profile_id, word, attempt, edit_distance, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    self.profile_id,
                    self.current_word,
                    guess,
                    distance,
                    int(time.time()),
                ),
            )
            self.tracking_conn.commit()
            self.update_profile_rating(distance, self.current_word)
        except sqlite3.Error:
            pass

    def edit_distance(self, source, target):
        if source == target:
            return 0
        if not source:
            return len(target)
        if not target:
            return len(source)
        prev = list(range(len(target) + 1))
        for i, s_char in enumerate(source, start=1):
            curr = [i]
            for j, t_char in enumerate(target, start=1):
                cost = 0 if s_char == t_char else 1
                curr.append(
                    min(
                        prev[j] + 1,
                        curr[j - 1] + 1,
                        prev[j - 1] + cost,
                    )
                )
            prev = curr
        return prev[-1]

    def grade_to_rating(self, grade_level):
        if grade_level is None:
            return 1000.0
        return 800.0 + float(grade_level) * 40.0

    def rating_to_grade_level(self, rating):
        if rating is None:
            return None
        grade = round((rating - 800.0) / 40.0)
        return max(0, min(16, grade))

    def format_estimated_level(self):
        if self.profile_rating is None:
            return ""
        grade = self.rating_to_grade_level(self.profile_rating)
        grade_text = self.format_grade_label(grade)
        return grade_text

    def get_word_difficulty(self, word):
        if not self.db_conn or not word:
            return None
        row = self.db_conn.execute(
            "SELECT difficulty FROM words WHERE word = ?",
            (word,),
        ).fetchone()
        if not row:
            return None
        return row[0]

    def word_rating_from_difficulty(self, difficulty):
        if difficulty is None:
            return 1000.0
        if self.difficulty_sorted:
            count = len(self.difficulty_sorted)
            if count == 1:
                percentile = 0.5
            else:
                index = bisect_left(self.difficulty_sorted, difficulty)
                index = max(0, min(index, count - 1))
                percentile = index / (count - 1)
            return 800.0 + 640.0 * percentile
        if self.difficulty_std:
            z = (difficulty - self.difficulty_mean) / self.difficulty_std
            z = max(-2.5, min(2.5, z))
            return 1000.0 + 300.0 * z
        return 800.0 + 400.0 * float(difficulty)

    def score_from_distance(self, distance, word):
        if distance == 0:
            return 1.0
        length = max(len(word), 1)
        proximity = max(0.0, 1.0 - (distance / length))
        return max(0.0, proximity * 0.7)

    def get_k_factor(self, attempts_count):
        if attempts_count < 50:
            return 32.0
        if attempts_count < 200:
            return 24.0
        return 16.0

    def update_profile_rating(self, distance, word):
        if not self.tracking_conn or not self.profile_id:
            return
        difficulty = self.get_word_difficulty(word)
        word_rating = self.word_rating_from_difficulty(difficulty)
        if self.profile_rating is None:
            self.profile_rating = self.grade_to_rating(self.profile_grade)
        expected = 1.0 / (1.0 + 10 ** ((word_rating - self.profile_rating) / 400.0))
        score = self.score_from_distance(distance, word)
        k = self.get_k_factor(self.profile_attempts)
        self.profile_rating = self.profile_rating + k * (score - expected)
        self.profile_attempts += 1
        self.tracking_conn.execute(
            """
            UPDATE profiles
            SET ability_rating = ?, attempts_count = ?, ability_updated_at = ?
            WHERE id = ?
            """,
            (self.profile_rating, self.profile_attempts, int(time.time()), self.profile_id),
        )
        self.tracking_conn.commit()

    def get_llm(self):
        if self.llm:
            return self.llm
        model_path = os.environ.get("LLM_MODEL_PATH", "").strip()
        try:
            from llama_cpp import Llama
        except Exception as exc:
            raise RuntimeError("Install llama-cpp-python to enable LLM output.") from exc
        if not model_path:
            model_path = self.ensure_model_path()
        n_ctx = int(os.environ.get("LLM_N_CTX", "2048"))
        n_threads = int(
            os.environ.get("LLM_THREADS", str(max(1, os.cpu_count() or 1)))
        )
        n_batch = int(os.environ.get("LLM_N_BATCH", "256"))
        self.llm = Llama(
            model_path=model_path,
            n_ctx=n_ctx,
            n_threads=n_threads,
            n_batch=n_batch,
        )
        return self.llm

    def ensure_model_path(self):
        if self.llm_model_path:
            return self.llm_model_path
        try:
            from huggingface_hub import hf_hub_download
            from huggingface_hub.utils import LocalEntryNotFoundError
            from huggingface_hub.constants import HF_HUB_CACHE
            from huggingface_hub.file_download import repo_folder_name
        except Exception as exc:
            raise RuntimeError(
                "Install huggingface_hub to auto-download the GGUF model."
            ) from exc

        cached_path = self.get_cached_model_path()
        if cached_path:
            self.llm_model_path = cached_path
            return self.llm_model_path

        size_text, expected_size = self.get_model_download_size()
        if not self.confirm_model_download(size_text):
            raise RuntimeError("Model download canceled.")

        repo_dir = (
            Path(HF_HUB_CACHE)
            / repo_folder_name(repo_id=self.llm_model_repo, repo_type="model")
        )
        progress = self.show_download_progress(size_text, expected_size, repo_dir)
        try:
            self.llm_model_path = hf_hub_download(
                repo_id=self.llm_model_repo,
                filename=self.llm_model_filename,
            )
            return self.llm_model_path
        finally:
            self.close_download_progress(progress)

    def get_cached_model_path(self):
        try:
            from huggingface_hub import hf_hub_download
            from huggingface_hub.utils import LocalEntryNotFoundError
        except Exception:
            return None
        try:
            return hf_hub_download(
                repo_id=self.llm_model_repo,
                filename=self.llm_model_filename,
                local_files_only=True,
            )
        except LocalEntryNotFoundError:
            return None
        except Exception:
            return None

    def get_model_download_size(self):
        try:
            from huggingface_hub import hf_hub_url, get_hf_file_metadata
        except Exception:
            return "", None
        try:
            url = hf_hub_url(self.llm_model_repo, filename=self.llm_model_filename)
            metadata = get_hf_file_metadata(url)
            size = getattr(metadata, "size", 0)
        except Exception:
            return "", None
        if not size:
            return "", None
        return self.format_size(size), size

    def format_size(self, size):
        units = ["B", "KB", "MB", "GB", "TB"]
        value = float(size)
        for unit in units:
            if value < 1024 or unit == units[-1]:
                return f"{value:.1f} {unit}"
            value /= 1024
        return f"{value:.1f} TB"

    def confirm_model_download(self, size_text):
        event = threading.Event()
        result = {"approved": False}
        detail = "Download the sentence model now?"
        if size_text:
            detail = f"Download the sentence model now?\n\nApprox size: {size_text}"

        def on_response(dialog, async_result, _data=None):
            try:
                response = dialog.choose_finish(async_result)
            except Exception:
                response = -1
            result["approved"] = response == 0
            event.set()

        def show_dialog():
            dialog = Gtk.AlertDialog()
            dialog.set_message("Download model")
            dialog.set_detail(detail)
            dialog.set_buttons(["Download", "Cancel"])
            dialog.choose(self.window, None, on_response, None)
            return False

        GLib.idle_add(show_dialog)
        event.wait()
        return result["approved"]

    def show_download_progress(self, size_text, expected_size, repo_dir):
        event = threading.Event()
        state = {}
        title = "Downloading sentence model"
        if size_text:
            title = f"Downloading sentence model ({size_text})"

        def show_window():
            window = Gtk.Window(transient_for=self.window, modal=True, title="Download")
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            box.set_margin_top(12)
            box.set_margin_bottom(12)
            box.set_margin_start(12)
            box.set_margin_end(12)
            label = Gtk.Label(label=title)
            label.set_xalign(0.0)
            bar = Gtk.ProgressBar()
            bar.set_show_text(True)
            bar.set_text("Downloading...")
            box.append(label)
            box.append(bar)
            window.set_child(box)
            window.set_default_size(360, -1)
            window.present()
            state["window"] = window
            state["bar"] = bar
            event.set()
            return False

        GLib.idle_add(show_window)
        event.wait()

        def update_progress():
            bar = state.get("bar")
            if not bar:
                return False
            if expected_size:
                current_size = self.get_incomplete_download_size(repo_dir)
                if current_size is not None:
                    fraction = min(1.0, current_size / expected_size)
                    bar.set_fraction(fraction)
                    bar.set_text(
                        f"{self.format_size(current_size)} / {self.format_size(expected_size)}"
                    )
                else:
                    bar.pulse()
            else:
                bar.pulse()
            return True

        state["pulse_id"] = GLib.timeout_add(200, update_progress)
        return state

    def get_incomplete_download_size(self, repo_dir):
        blobs_dir = Path(repo_dir) / "blobs"
        if not blobs_dir.exists():
            return None
        latest_path = None
        latest_mtime = 0
        for path in blobs_dir.glob("*.incomplete"):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime >= latest_mtime:
                latest_mtime = mtime
                latest_path = path
        if not latest_path:
            return None
        try:
            return latest_path.stat().st_size
        except OSError:
            return None

    def close_download_progress(self, state):
        def close():
            pulse_id = state.get("pulse_id")
            if pulse_id:
                GLib.source_remove(pulse_id)
            window = state.get("window")
            if window:
                window.close()
            return False

        GLib.idle_add(close)

    def generate_sentence(self, word):
        print('generate_sentence', word)
        llm = self.get_llm()
        prompt = (
            'You are an announcer in a spelling bee competition who is going to speak a sentence out loud. '
            'Say only a single sentence that uses the given word exactly in context. '
            'Use the word exactly as spelled. '
            'Keep the sentence concise and clear.'
            f'Use the word "{word}" in a meaningful sentence.\n\n'
            "Begin Sentence:\n"
        )
        print('prompt', prompt)
        result = llm(
            prompt,
            max_tokens=64,
            temperature=float(os.environ.get("LLM_TEMPERATURE", "0.9")),
            top_p=float(os.environ.get("LLM_TOP_P", "0.9")),
            stop=["\n"],
        )
        print('result', result)
        text = (result.get("choices") or [{}])[0].get("text", "").strip()
        print(text)
        return text

    def generate_definition(self, word):
        print('generate_definition', word)
        llm = self.get_llm()
        prompt = (
            "You are a spelling bee announcer. Provide a short, simple definition "
            "of the word that a student would understand. "
            "Use one sentence, fewer than 15 words. Do not use the word itself.\n\n"
            f'Word: "{word}"\n'
            "Definition:\n"
        )
        print('prompt', prompt)
        result = llm(
            prompt,
            max_tokens=48,
            temperature=float(os.environ.get("LLM_TEMPERATURE", "0.7")),
            top_p=float(os.environ.get("LLM_TOP_P", "0.9")),
            stop=["\n"],
        )
        print('result', result)
        text = (result.get("choices") or [{}])[0].get("text", "").strip()
        print(text)
        return text

    def prefetch_sentence(self, word, allow_download):
        if not allow_download and not self.get_cached_model_path():
            return
        self._sentence_generation_id += 1
        generation_id = self._sentence_generation_id

        def run():
            with self.llm_lock:
                GLib.idle_add(self.set_sentence_busy, True)
                try:
                    sentence = self.generate_sentence(word)
                    if not sentence:
                        GLib.idle_add(
                            self.show_error_dialog,
                            "Sentence generation failed",
                            "Sentence generation returned no text.",
                        )
                        return
                    if self.current_word == word:
                        self.current_sentence = sentence
                        GLib.idle_add(self.update_sentence_button_state)
                except Exception as exc:
                    GLib.idle_add(
                        self.show_error_dialog,
                        "Sentence generation failed",
                        str(exc),
                    )
                finally:
                    if generation_id == self._sentence_generation_id:
                        GLib.idle_add(self.set_sentence_busy, False)
                    GLib.idle_add(self.ensure_entry_focus)

        threading.Thread(target=run, daemon=True).start()

    def prefetch_definition(self, word, allow_download):
        if not allow_download and not self.get_cached_model_path():
            return
        self._definition_generation_id += 1
        generation_id = self._definition_generation_id

        def run():
            with self.llm_lock:
                GLib.idle_add(self.set_define_busy, True)
                try:
                    definition = self.generate_definition(word)
                    if not definition:
                        GLib.idle_add(
                            self.show_error_dialog,
                            "Definition generation failed",
                            "Definition generation returned no text.",
                        )
                        return
                    if self.current_word == word:
                        self.current_definition = definition
                        GLib.idle_add(self.update_define_button_state)
                        # Precompute only; speaking happens on user action.
                except Exception as exc:
                    GLib.idle_add(
                        self.show_error_dialog,
                        "Definition generation failed",
                        str(exc),
                    )
                finally:
                    if generation_id == self._definition_generation_id:
                        GLib.idle_add(self.set_define_busy, False)
                    GLib.idle_add(self.ensure_entry_focus)

        threading.Thread(target=run, daemon=True).start()

    def ensure_entry_focus(self):
        if not self.entry.has_focus():
            self.focus_entry()

    def focus_entry(self):
        self.entry.grab_focus()
        self.entry.set_position(-1)
        pos = self.entry.get_position()
        self.entry.select_region(pos, pos)

    def update_window_height(self):
        if not getattr(self, "window", None):
            return
        self.window.set_default_size(520, -1)

    def maybe_check_for_updates(self):
        last_check = self.settings.get("last_update_check", 0)
        now = int(time.time())
        if now - last_check < 7 * 24 * 60 * 60:
            return
        self.settings["last_update_check"] = now

        def run():
            latest = self.fetch_latest_version()
            current = self.get_current_version()
            if not latest or not current:
                return
            if self.compare_versions(latest, current) <= 0:
                return
            GLib.idle_add(self.prompt_update, current, latest)

        threading.Thread(target=run, daemon=True).start()

    def prompt_update(self, current, latest):
        dialog = Gtk.AlertDialog()
        dialog.set_message("Update available")
        dialog.set_detail(
            f"A newer version is available.\n\nCurrent: {current}\nLatest: {latest}\n\nUpgrade now?"
        )
        dialog.set_buttons(["Upgrade", "Later"])
        dialog.choose(self.window, None, self.on_update_response)

    def on_update_response(self, _dialog, response):
        if response != 0:
            return
        self.run_upgrade()

    def run_upgrade(self):
        def run():
            cmd = [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--user",
                "--upgrade",
                "spelling-bee-tts",
            ]
            result = subprocess.run(cmd, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if result.returncode == 0:
                message = "Upgrade complete. Please restart the app."
            else:
                message = "Upgrade failed. Please try again in a terminal."
            GLib.idle_add(self.show_info_dialog, message)

        threading.Thread(target=run, daemon=True).start()

    def show_info_dialog(self, message):
        dialog = Gtk.AlertDialog()
        dialog.set_message("Spelling Bee")
        dialog.set_detail(message)
        dialog.show(self.window)

    def show_error_dialog(self, title, detail):
        dialog = Gtk.AlertDialog()
        dialog.set_message(title)
        dialog.set_detail(detail)
        dialog.show(self.window)

    def fetch_latest_version(self):
        url = "https://pypi.org/pypi/spelling-bee-tts/json"
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                data = json.loads(response.read().decode("utf-8"))
            return data.get("info", {}).get("version", "")
        except Exception:
            return ""

    def get_current_version(self):
        try:
            import importlib.metadata as metadata
        except Exception:
            return ""
        try:
            return metadata.version("spelling-bee-tts")
        except metadata.PackageNotFoundError:
            return ""

    def compare_versions(self, a, b):
        def parts(version):
            out = []
            for chunk in re.split(r"[.-]", version):
                if chunk.isdigit():
                    out.append(int(chunk))
                else:
                    out.append(chunk)
            return out

        a_parts = parts(a)
        b_parts = parts(b)
        return (a_parts > b_parts) - (a_parts < b_parts)

    def check_system_dependencies(self, window):
        missing = []
        if not self.pick_mp3_players():
            preferred = self.get_preferred_player_package()
            if preferred:
                missing.append(preferred)
        if not missing:
            return

        command = self.format_install_command(missing)
        detail = "Missing system packages: " + ", ".join(missing)
        if command:
            detail += f"\n\nInstall with:\n{command}"
            detail += "\n\nAlternatives: ffmpeg (ffplay) or mpg123."
        else:
            detail += "\n\nInstall with your system package manager."

        dialog = Gtk.AlertDialog()
        dialog.set_message("Missing system dependencies")
        dialog.set_detail(detail)
        dialog.set_buttons(["Install", "Close"])
        dialog.choose(window, None, self.on_dependency_dialog_response, command)

    def format_install_command(self, packages):
        distro = self.get_distro_id()
        pkg_list = " ".join(packages)
        if distro in {"ubuntu", "debian", "linuxmint", "pop"}:
            return f"sudo apt install {pkg_list}"
        if distro in {"fedora", "rhel", "centos"}:
            return f"sudo dnf install {pkg_list}"
        if distro in {"arch", "manjaro"}:
            return f"sudo pacman -S {pkg_list}"
        if distro in {"opensuse", "suse"}:
            return f"sudo zypper install {pkg_list}"
        return ""

    def get_preferred_player_package(self):
        distro = self.get_distro_id()
        preferred = {
            "ubuntu": "mpv",
            "debian": "mpv",
            "linuxmint": "mpv",
            "pop": "mpv",
            "fedora": "mpv",
            "rhel": "mpv",
            "centos": "mpv",
            "arch": "mpv",
            "manjaro": "mpv",
            "opensuse": "mpv",
            "suse": "mpv",
        }
        return preferred.get(distro, "mpv")

    def on_dependency_dialog_response(self, dialog, response, command):
        if response != 0 or not command:
            return
        if not self.run_privileged_command(command):
            followup = Gtk.AlertDialog()
            followup.set_message("Could not launch privileged installer")
            followup.set_detail(
                "Please run the install command manually in a terminal."
            )
            followup.show(self.get_active_window())

    def run_privileged_command(self, command):
        helpers = [
            ("pkexec", ["pkexec", "sh", "-c", command]),
            ("gksudo", ["gksudo", "sh", "-c", command]),
            ("gksu", ["gksu", "sh", "-c", command]),
            ("kdesudo", ["kdesudo", "sh", "-c", command]),
        ]
        for name, cmd in helpers:
            if shutil.which(name):
                try:
                    subprocess.Popen(cmd)
                    return True
                except OSError:
                    return False
        return False

    def get_distro_id(self):
        os_release = Path("/etc/os-release")
        if not os_release.exists():
            return ""
        try:
            data = os_release.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return ""
        for line in data.splitlines():
            if line.startswith("ID="):
                return line.split("=", 1)[1].strip().strip('"')
        return ""


def main():
    app = SpellingBeeApp()
    app.run(sys.argv)




if __name__ == "__main__":
    main()
