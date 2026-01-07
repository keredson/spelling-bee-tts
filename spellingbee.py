import asyncio
import json
import os
import random
import re
import shutil
import subprocess
import sysconfig
import tempfile
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, GObject, Gtk

import edge_tts


class ListEntry(GObject.Object):
    kind = GObject.Property(type=str)
    label = GObject.Property(type=str)
    path = GObject.Property(type=str)


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
        self.audio_cache = {}
        self.audio_dir = tempfile.TemporaryDirectory()

    def do_activate(self):
        self.window = Gtk.ApplicationWindow(application=self)
        self.window.set_title("Spelling Bee")
        self.window.set_default_size(520, -1)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        outer.set_margin_top(12)
        outer.set_margin_bottom(12)
        outer.set_margin_start(12)
        outer.set_margin_end(12)

        self.header = Gtk.Label(label="Load a word list to begin.")
        self.header.set_xalign(0.0)

        self.score_label = Gtk.Label(label="Score: 0/0")
        self.score_label.set_xalign(0.0)

        self.list_store = Gio.ListStore.new(ListEntry)
        self.list_selection = Gtk.NoSelection.new(self.list_store)
        self.list_view = Gtk.ListView(
            model=self.list_selection, factory=self.build_list_factory()
        )
        self.list_view.set_single_click_activate(True)
        self.list_view.connect("activate", self.on_list_activate)

        self.list_scroller = Gtk.ScrolledWindow()
        self.list_scroller.set_child(self.list_view)
        self.list_scroller.set_vexpand(True)
        self.list_scroller.set_min_content_height(220)

        self.load_button = Gtk.Button(label="Add Word List")
        self.load_button.connect("clicked", self.on_choose_file, self.window)

        self.start_button = Gtk.Button(label="Start Game")
        self.start_button.connect("clicked", self.on_start_game)

        self.word_label = Gtk.Label(label="")
        self.word_label.set_xalign(0.0)

        self.entry = Gtk.Entry()
        self.entry.set_placeholder_text("Type your spelling here")
        self.entry.connect("activate", self.on_submit)

        self.submit_button = Gtk.Button(label="Submit")
        self.submit_button.connect("clicked", self.on_submit)

        self.say_again_button = Gtk.Button()
        self.say_again_button.connect("clicked", self.on_say_again)
        self.say_again_label = Gtk.Label(label="Say Again")
        self.say_again_spinner = Gtk.Spinner()
        self.say_again_spinner.set_visible(False)
        say_again_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        say_again_box.append(self.say_again_label)
        say_again_box.append(self.say_again_spinner)
        self.say_again_button.set_child(say_again_box)

        self.button_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.button_row.append(self.submit_button)
        self.button_row.append(self.say_again_button)

        outer.append(self.header)
        outer.append(self.list_scroller)
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
        self.refresh_list_model()

        self.window.set_child(outer)
        self.window.present()
        self.check_system_dependencies(self.window)

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
        self.list_scroller.set_visible(False)
        self.load_button.set_visible(False)
        self.header.set_visible(False)
        self.start_button.set_visible(True)
        self.word_label.set_text("Ready when you are.")
        self.update_window_height()

    def on_start_game(self, _button):
        if not self.words:
            return
        self.start_button.set_visible(False)
        self.entry.set_visible(True)
        self.button_row.set_visible(True)
        self.score_label.set_visible(True)
        self.entry.grab_focus()
        self.next_word()
        self.update_window_height()

    def next_word(self):
        if not self.words:
            self.word_label.set_text("Load a word list to begin.")
            return

        self.current_word = random.choice(self.words)
        self.entry.set_text("")
        self.word_label.set_text("Listen and type the spelling.")
        self.speak("Please spell: "+ self.current_word)

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
            self.speak("That is correct!", on_done=self.after_feedback)
        else:
            self.word_label.set_text(f"Incorrect. It was: {self.current_word}")
            self.speak(
                f"Sorry, that is not correct, the correct spelling is: {'. '.join(self.current_word)}"
                ,
                on_done=self.after_feedback,
            )

        self.update_score()

    def after_feedback(self):
        self.next_word()
        return False

    def on_say_again(self, _button):
        if self.current_word:
            self.speak(self.current_word)

    def update_score(self):
        self.score_label.set_text(f"Score: {self.correct}/{self.total}")

    def speak(self, text, on_done=None):
        mp3_players = self.pick_mp3_players()
        if not mp3_players:
            self.word_label.set_text("Install mpv/ffplay/mpg123 to play TTS audio.")
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
                                self.word_label.set_text,
                                "TTS produced empty audio. Check network access.",
                            )
                            return
                        self.audio_cache[text] = str(output_path)
                        ok = self.play_audio(mp3_players, str(output_path))

                    if not ok:
                        GLib.idle_add(
                            self.word_label.set_text,
                            "Audio playback failed. Check your sound device.",
                        )
                    else:
                        success = True
                except Exception as exc:
                    GLib.idle_add(
                        self.word_label.set_text,
                        f"TTS failed: {exc}",
                    )
                finally:
                    if success:
                        GLib.idle_add(
                            self.word_label.set_text,
                            "Listen and type the spelling.",
                        )
                    GLib.idle_add(self.set_say_again_busy, False)
                    if on_done:
                        GLib.idle_add(on_done)

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
        self.refresh_list_model()

    def refresh_list_model(self):
        while self.list_store.get_n_items():
            self.list_store.remove(0)

        def append(kind, label, path=""):
            self.list_store.append(ListEntry(kind=kind, label=label, path=path))

        existing = []
        for path_str in self.recent_lists:
            if Path(path_str).exists():
                existing.append(path_str)
        self.recent_lists = existing[:10]

        append("header", "Recent")
        if self.recent_lists:
            for path_str in self.recent_lists:
                display = Path(path_str).stem
                append("item", display, path_str)
        else:
            append("empty", "No recent lists yet.")

        append("header", "Built-in")
        builtin_dir = self.get_builtin_dir()
        builtin_paths = []
        if builtin_dir:
            builtin_paths = sorted(
                builtin_dir.glob("*.txt"), key=lambda p: self.natural_key(p.stem)
            )
        if builtin_paths:
            for path in builtin_paths:
                append("item", path.stem, str(path))
        else:
            append("empty", "No built-in lists found.")

    def build_list_factory(self):
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self.on_list_setup)
        factory.connect("bind", self.on_list_bind)
        return factory

    def on_list_setup(self, _factory, list_item):
        label = Gtk.Label(xalign=0.0)
        label.set_wrap(True)
        label.set_selectable(False)
        list_item.set_child(label)

    def on_list_bind(self, _factory, list_item):
        entry = list_item.get_item()
        label = list_item.get_child()
        label.set_css_classes([])
        label.set_margin_top(4)
        label.set_margin_bottom(4)

        if entry.kind == "header":
            label.set_use_markup(True)
            label.set_markup(f"<b>{entry.label}</b>")
            label.set_margin_top(12)
            label.set_margin_bottom(6)
        elif entry.kind == "empty":
            label.set_use_markup(False)
            label.set_text(entry.label)
            label.add_css_class("dim-label")
        else:
            label.set_use_markup(False)
            label.set_text(entry.label)
    def on_list_activate(self, _list_view, position):
        entry = self.list_store.get_item(position)
        if not entry or entry.kind != "item":
            return
        self.load_words(Path(entry.path))

    def natural_key(self, text):
        parts = re.split(r"(\d+)", text.lower())
        return [int(part) if part.isdigit() else part for part in parts]

    def get_builtin_dir(self):
        candidates = [
            Path(__file__).resolve().parent / "word_lists",
            Path.cwd() / "word_lists",
            Path(sysconfig.get_paths()["purelib"]) / "spellingbee_word_lists",
        ]
        for candidate in candidates:
            if candidate.exists() and list(candidate.glob("*.txt")):
                return candidate
        return None

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

    def set_say_again_busy(self, busy):
        self.say_again_button.set_sensitive(not busy)
        self.say_again_spinner.set_visible(busy)
        self.say_again_label.set_text("Speaking..." if busy else "Say Again")
        if busy:
            self.say_again_spinner.start()
        else:
            self.say_again_spinner.stop()

    def update_window_height(self):
        if not getattr(self, "window", None):
            return
        self.window.set_default_size(520, -1)

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
    app.run(None)


if __name__ == "__main__":
    main()
