import asyncio
import json
import os
import random
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib

import edge_tts
class SpellingBeeApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="com.example.SpellingBee")
        self.words = []
        self.current_word = None
        self.correct = 0
        self.total = 0
        self.tts_lock = threading.Lock()
        self.edge_voice = os.environ.get("EDGE_TTS_VOICE", "en-US-AriaNeural")
        self.recent_lists = []
        self.recent_path = self.get_config_path() / "recent_lists.json"

    def do_activate(self):
        window = Gtk.ApplicationWindow(application=self)
        window.set_title("Spelling Bee")
        window.set_default_size(520, 240)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        outer.set_margin_top(12)
        outer.set_margin_bottom(12)
        outer.set_margin_start(12)
        outer.set_margin_end(12)

        self.header = Gtk.Label(label="Load a word list to begin.")
        self.header.set_xalign(0.0)

        self.score_label = Gtk.Label(label="Score: 0/0")
        self.score_label.set_xalign(0.0)

        self.recent_label = Gtk.Label(label="Recent word lists:")
        self.recent_label.set_xalign(0.0)

        self.recent_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)

        self.load_button = Gtk.Button(label="Choose Word List")
        self.load_button.connect("clicked", self.on_choose_file, window)

        self.start_button = Gtk.Button(label="Start Game")
        self.start_button.connect("clicked", self.on_start_game)

        self.word_label = Gtk.Label(label="")
        self.word_label.set_xalign(0.0)

        self.entry = Gtk.Entry()
        self.entry.set_placeholder_text("Type your spelling here")
        self.entry.connect("activate", self.on_submit)

        self.submit_button = Gtk.Button(label="Submit")
        self.submit_button.connect("clicked", self.on_submit)

        self.say_again_button = Gtk.Button(label="Say Again")
        self.say_again_button.connect("clicked", self.on_say_again)

        self.button_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.button_row.append(self.submit_button)
        self.button_row.append(self.say_again_button)

        outer.append(self.header)
        outer.append(self.recent_label)
        outer.append(self.recent_box)
        outer.append(self.load_button)
        outer.append(self.start_button)
        outer.append(self.word_label)
        outer.append(self.entry)
        outer.append(self.button_row)
        outer.append(self.score_label)

        self.start_button.set_visible(False)
        self.entry.set_visible(False)
        self.button_row.set_visible(False)
        self.score_label.set_visible(False)

        self.load_recent_lists()
        self.refresh_recent_ui()

        window.set_child(outer)
        window.present()

    def on_choose_file(self, _button, window):
        dialog = Gtk.FileChooserNative(
            title="Select Word List",
            transient_for=window,
            action=Gtk.FileChooserAction.OPEN,
            accept_label="Open",
            cancel_label="Cancel",
        )
        dialog.connect("response", self.on_file_response)
        dialog.show()

    def on_file_response(self, dialog, response):
        if response == Gtk.ResponseType.ACCEPT:
            file = dialog.get_file()
            if file:
                path = Path(file.get_path())
                self.load_words(path)
        dialog.destroy()

    def load_words(self, path):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            self.word_label.set_text("Failed to read file.")
            return

        words = []
        for line in text.splitlines():
            stripped = line.strip()
            if stripped:
                words.append(stripped)

        if not words:
            self.word_label.set_text("No words found in file.")
            return

        self.words = words
        self.correct = 0
        self.total = 0
        self.update_score()
        self.remember_recent_list(path)
        self.load_button.set_visible(False)
        self.header.set_visible(False)
        self.recent_label.set_visible(False)
        self.recent_box.set_visible(False)
        self.start_button.set_visible(True)
        self.word_label.set_text("Ready when you are.")

    def on_start_game(self, _button):
        if not self.words:
            return
        self.start_button.set_visible(False)
        self.entry.set_visible(True)
        self.button_row.set_visible(True)
        self.score_label.set_visible(True)
        self.entry.grab_focus()
        self.next_word()

    def next_word(self):
        if not self.words:
            self.word_label.set_text("Load a word list to begin.")
            return

        self.current_word = random.choice(self.words)
        self.entry.set_text("")
        self.word_label.set_text("Listen and type the spelling.")
        self.speak(self.current_word)

    def on_submit(self, _widget):
        if not self.current_word:
            return

        guess = self.entry.get_text().strip()
        if not guess:
            return

        self.total += 1
        if guess.lower() == self.current_word.lower():
            self.correct += 1
            self.word_label.set_text("Correct! Next word...")
        else:
            self.word_label.set_text(f"Incorrect. It was: {self.current_word}")

        self.update_score()
        GLib.timeout_add(900, self.after_feedback)

    def after_feedback(self):
        self.next_word()
        return False

    def on_say_again(self, _button):
        if self.current_word:
            self.speak(self.current_word)

    def update_score(self):
        self.score_label.set_text(f"Score: {self.correct}/{self.total}")

    def speak(self, text):
        mp3_players = self.pick_mp3_players()
        if not mp3_players:
            self.word_label.set_text("Install mpv/ffplay/mpg123 to play TTS audio.")
            return

        def run():
            with self.tts_lock:
                GLib.idle_add(self.word_label.set_text, "Generating audio...")
                try:
                    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=True) as tmp:
                        asyncio.run(
                            edge_tts.Communicate(
                                f"Please spell: {text}", voice=self.edge_voice
                            ).save(tmp.name)
                        )
                        size = Path(tmp.name).stat().st_size
                        if size == 0:
                            GLib.idle_add(
                                self.word_label.set_text,
                                "TTS produced empty audio. Check network access.",
                            )
                            return
                        ok = self.play_audio(mp3_players, tmp.name)
                        if not ok:
                            GLib.idle_add(
                                self.word_label.set_text,
                                "Audio playback failed. Check your sound device.",
                            )
                except Exception as exc:
                    GLib.idle_add(
                        self.word_label.set_text,
                        f"TTS failed: {exc}",
                    )

        threading.Thread(target=run, daemon=True).start()

    def load_recent_lists(self):
        if not self.recent_path.exists():
            return
        try:
            data = json.loads(self.recent_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(data, list):
            self.recent_lists = [str(Path(p)) for p in data if p]

    def remember_recent_list(self, path):
        normalized = str(Path(path).resolve())
        self.recent_lists = [p for p in self.recent_lists if p != normalized]
        self.recent_lists.insert(0, normalized)
        self.recent_lists = self.recent_lists[:10]
        try:
            self.recent_path.parent.mkdir(parents=True, exist_ok=True)
            self.recent_path.write_text(
                json.dumps(self.recent_lists, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass
        self.refresh_recent_ui()

    def refresh_recent_ui(self):
        while child := self.recent_box.get_first_child():
            self.recent_box.remove(child)

        existing = []
        for path_str in self.recent_lists:
            path = Path(path_str)
            if path.exists():
                existing.append(path_str)

        self.recent_lists = existing

        if not self.recent_lists:
            self.recent_label.set_visible(False)
            self.recent_box.set_visible(False)
            return

        self.recent_label.set_visible(True)
        self.recent_box.set_visible(True)
        for path_str in self.recent_lists:
            button = Gtk.Button(label=path_str)
            button.set_halign(Gtk.Align.START)
            button.connect("clicked", self.on_recent_clicked, path_str)
            self.recent_box.append(button)

    def on_recent_clicked(self, _button, path_str):
        self.load_words(Path(path_str))

    def get_config_path(self):
        config_home = os.environ.get("XDG_CONFIG_HOME")
        if config_home:
            return Path(config_home) / "spellingbee"
        return Path.home() / ".config" / "spellingbee"

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


def main():
    app = SpellingBeeApp()
    app.run(None)


if __name__ == "__main__":
    main()
