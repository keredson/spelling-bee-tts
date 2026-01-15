import asyncio
import json
import os
import random
import re
import shutil
import subprocess
import site
import sys
import sysconfig
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timezone
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
        super().__init__(application_id="org.pypi.project.spelling-bee-tts")
        GLib.set_application_name("Spelling Bee TTS")
        self.words = []
        self.current_word = None
        self.correct = 0
        self.total = 0
        self.tts_lock = threading.Lock()
        self.edge_voice = os.environ.get("EDGE_TTS_VOICE", "en-US-AriaNeural")
        self.recent_lists = []
        self.recent_path = self.get_config_path() / "recent_lists.json"
        self.settings_path = self.get_config_path() / "settings.json"
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

        self.load_button = Gtk.Button(label="Import Word List")
        self.load_button.connect("clicked", self.on_choose_file, self.window)
        self.generate_list_button = Gtk.Button(label="Generate Word List")
        self.generate_list_button.connect("clicked", self.on_generate_word_list)

        self.list_button_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8
        )
        self.list_button_row.set_halign(Gtk.Align.CENTER)
        self.list_button_row.append(self.load_button)
        self.list_button_row.append(self.generate_list_button)

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

        self.sentence_button = Gtk.Button()
        self.sentence_button.connect("clicked", self.on_use_sentence)
        self.sentence_label = Gtk.Label(label="Use in a Sentence")
        self.sentence_spinner = Gtk.Spinner()
        self.sentence_spinner.set_visible(False)
        sentence_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        sentence_box.append(self.sentence_label)
        sentence_box.append(self.sentence_spinner)
        self.sentence_button.set_child(sentence_box)

        self.button_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.button_row.set_halign(Gtk.Align.CENTER)
        self.button_row.append(self.submit_button)
        self.button_row.append(self.say_again_button)
        self.button_row.append(self.sentence_button)

        outer.append(self.header)
        outer.append(self.list_scroller)
        outer.append(self.list_button_row)
        outer.append(self.word_label)
        outer.append(self.entry)
        outer.append(self.button_row)
        outer.append(self.score_label)

        self.entry.set_visible(False)
        self.button_row.set_visible(False)
        self.score_label.set_visible(False)

        self.load_recent_lists()
        self.refresh_list_model()

        self.window.set_child(outer)
        self.window.present()
        self.check_system_dependencies(self.window)
        self.maybe_check_for_updates()

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

    def on_close_request(self, _window):
        self.quit()
        return False

    def on_file_response(self, dialog, response):
        if response == Gtk.ResponseType.ACCEPT:
            file = dialog.get_file()
            if file:
                path = Path(file.get_path())
                self.load_words(path)
        dialog.destroy()

    def on_generate_word_list(self, _button):
        self.prompt_word_list_description(self.on_word_list_description)

    def on_word_list_description(self, description):
        if not description:
            return

        cancel_event = threading.Event()
        progress = self.show_generate_list_progress(cancel_event)

        def run():
            GLib.idle_add(self.set_generate_list_busy, True)
            try:
                if cancel_event.is_set():
                    return
                words = self.generate_word_list(description, cancel_event, progress)
                if cancel_event.is_set() or words is None:
                    return
                if len(words) < 10:
                    raise RuntimeError(
                        "Generated list was too short. Try a different prompt."
                    )
                path = self.save_generated_word_list(description, words)
                GLib.idle_add(self.load_words, path)
            except Exception as exc:
                GLib.idle_add(
                    self.show_error_dialog,
                    "Word list generation failed",
                    str(exc),
                )
            finally:
                self.close_generate_list_progress(progress)
                GLib.idle_add(self.set_generate_list_busy, False)

        threading.Thread(target=run, daemon=True).start()

    def load_words(self, path):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            self.word_label.set_text("Failed to read file.")
            return

        words = []
        seen = set()
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            key = stripped.lower()
            if key in seen:
                continue
            seen.add(key)
            words.append(stripped)

        if not words:
            self.word_label.set_text("No words found in file.")
            return

        random.shuffle(words)
        self.words = words
        self.correct = 0
        self.total = 0
        self.update_score()
        self.remember_recent_list(path)
        self.list_scroller.set_visible(False)
        self.list_button_row.set_visible(False)
        self.header.set_visible(False)
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

        self.current_word = self.words.pop(0)
        self.current_sentence = None
        self.entry.set_text("")
        self.word_label.set_text("Listen and type the spelling.")
        self.entry.grab_focus()
        self.speak("Please spell: " + self.current_word)
        self.prefetch_sentence(self.current_word, allow_download=False)

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
            self.entry.grab_focus()

    def on_use_sentence(self, _button):
        if not self.current_word:
            return

        if self.current_sentence:
            self.speak(self.current_sentence, reset_label=False)
            self.entry.grab_focus()
            return
        self.prefetch_sentence(self.current_word, allow_download=True)

    def update_score(self):
        self.score_label.set_text(f"Score: {self.correct}/{self.total}")

    def speak(self, text, on_done=None, reset_label=True):
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
                    if success and reset_label:
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
            Path(sysconfig.get_paths()["data"]) / "spelling_bee_tts_word_lists",
            Path(sysconfig.get_paths()["purelib"]) / "spelling_bee_tts_word_lists",
            Path(site.USER_BASE) / "spelling_bee_tts_word_lists",
            Path(sys.prefix) / "spelling_bee_tts_word_lists",
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
        self.sentence_button.set_sensitive(not busy)
        self.say_again_spinner.set_visible(busy)
        self.say_again_label.set_text("Speaking..." if busy else "Say Again")
        if busy:
            self.say_again_spinner.start()
        else:
            self.say_again_spinner.stop()

    def set_sentence_busy(self, busy):
        self.sentence_button.set_sensitive(not busy)
        self.sentence_spinner.set_visible(busy)
        self.sentence_label.set_text("Generating sentence..." if busy else "Use in a Sentence")
        if busy:
            self.sentence_spinner.start()
        else:
            self.sentence_spinner.stop()

    def set_generate_list_busy(self, busy):
        self.generate_list_button.set_sensitive(not busy)
        self.generate_list_button.set_label(
            "Generating..." if busy else "Generate Word List"
        )

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

    def generate_word_list(self, description, cancel_event, progress):
        llm = self.get_llm()
        prompt = (
            f"Task: Generate 50 academic vocabulary words for {description}.\n"
            #'Structure: Provide 5 words for each of these 10 categories: Science, Literature, History, Emotions, Technology, Law, Arts, Travel, Health, and Logic.'
            f'Use words found in standard {description} textbooks and literature.\n'
            'Do not use obscure spelling bee words.\n'
            'Use distinct words with different roots and meanings.\n'
            f"Do not use proper nouns, abbreviations, symbols, numbers or hyphens.\n"
            f"Output one word per line. Each word must be unique.\n"
            'Do not include any explanations or additional text.\n'
        )
        stream = llm(
            prompt,
            max_tokens=512,
            temperature=float(os.environ.get("LLM_TEMPERATURE", "0.9")),
            top_p=float(os.environ.get("LLM_TOP_P", "0.9")),
            stop=["\n\n"],
            stream=True,
        )
        words = set()
        for word in self.iter_words_from_stream(stream, cancel_event, progress):
            words.add(word)
        return sorted(words)

    def iter_words_from_stream(self, stream, cancel_event, progress):
        buffer = ""
        unique_words = set()
        total_words = 0

        for chunk in stream:
            if cancel_event.is_set():
                return
            text_piece = (chunk.get("choices") or [{}])[0].get("text", "")
            if not text_piece:
                continue
            sys.stdout.write(text_piece)
            sys.stdout.flush()
            buffer += text_piece
            for word in self.consume_word_buffer(buffer):
                if cancel_event.is_set():
                    return
                total_words += 1
                if total_words >= 20:
                    unique_ratio = len(unique_words) / total_words
                    if unique_ratio < 0.2:
                        print(
                            f"generation repeat guard: unique={len(unique_words)} total={total_words}"
                        )
                        raise RuntimeError(
                            "Generation stalled with too many repeated words."
                        )
                unique_words.add(word)
                if progress:
                    GLib.idle_add(
                        self.update_generate_list_progress,
                        progress,
                        word,
                    )
                yield word
            buffer = self.trim_word_buffer(buffer)

        tail = self.normalize_word(buffer)
        if tail:
            if progress:
                GLib.idle_add(
                    self.update_generate_list_progress,
                    progress,
                    tail,
                )
            yield tail

    def consume_word_buffer(self, buffer):
        if "\n" not in buffer and "," not in buffer:
            return []
        parts = re.split(r"[,\n]", buffer)
        parts.pop()
        out = []
        for candidate in parts:
            cleaned = self.normalize_word(candidate)
            if cleaned:
                out.append(cleaned)
        return out

    def trim_word_buffer(self, buffer):
        if "\n" not in buffer and "," not in buffer:
            return buffer
        parts = re.split(r"[,\n]", buffer)
        return parts[-1]

    def parse_word_list(self, text):
        words = set()
        for raw in re.split(r"[\s,]+", text):
            cleaned = self.normalize_word(raw)
            if cleaned:
                words.add(cleaned)
        words = sorted(words)
        print('generated', words)
        return words

    def normalize_word(self, text):
        return re.sub(r"[^A-Za-z]+", "", text).lower()

    def save_generated_word_list(self, description, words):
        safe = re.sub(r"[^a-zA-Z0-9 -]+", "", description.strip())
        safe = re.sub(r"\s+", " ", safe).strip()[:40]
        if not safe:
            safe = "generated"
        timestamp = datetime.now().strftime("%Y-%m-%d %I.%M.%S %p")
        directory = self.get_config_path() / "generated_lists"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{safe} - {timestamp}.txt"
        path.write_text("\n".join(words) + "\n", encoding="utf-8")
        return path

    def prompt_word_list_description(self, on_done):
        def show_dialog():
            window = Gtk.Window(
                transient_for=self.window, modal=True, title="Generate Word List"
            )
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            box.set_margin_top(12)
            box.set_margin_bottom(12)
            box.set_margin_start(12)
            box.set_margin_end(12)
            label = Gtk.Label(
                label="Give a difficulty for the word list (e.g., 8th Grade):"
            )
            label.set_xalign(0.0)
            entry = Gtk.Entry()
            entry.set_activates_default(True)
            entry.set_placeholder_text("8th Grade")
            button_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            button_row.set_halign(Gtk.Align.END)
            cancel_button = Gtk.Button(label="Cancel")
            ok_button = Gtk.Button(label="Generate")
            if hasattr(ok_button, "set_receives_default"):
                ok_button.set_receives_default(True)
            button_row.append(cancel_button)
            button_row.append(ok_button)
            box.append(label)
            box.append(entry)
            box.append(button_row)
            window.set_child(box)
            window.set_default_size(360, -1)
            if hasattr(window, "set_default_widget"):
                window.set_default_widget(ok_button)

            def finish(value):
                if getattr(window, "_closing", False):
                    return
                window._closing = True
                window.close()
                on_done(value)

            cancel_button.connect("clicked", lambda _btn: finish(""))
            ok_button.connect("clicked", lambda _btn: finish(entry.get_text().strip()))
            entry.connect("activate", lambda _entry: finish(entry.get_text().strip()))
            window.connect("close-request", lambda _win: (finish(""), False)[1])
            window.present()
            entry.grab_focus()
            return False

        GLib.idle_add(show_dialog)

    def show_generate_list_progress(self, cancel_event):
        state = {}

        def show_window():
            window = Gtk.Window(
                transient_for=self.window, modal=True, title="Generating Word List"
            )
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            box.set_margin_top(12)
            box.set_margin_bottom(12)
            box.set_margin_start(12)
            box.set_margin_end(12)
            label = Gtk.Label(label="Generating word list...")
            label.set_xalign(0.0)
            bar = Gtk.ProgressBar()
            bar.set_show_text(True)
            bar.set_text("Working...")
            bar.set_margin_top(4)
            bar.pulse()
            cancel_button = Gtk.Button(label="Cancel")
            cancel_button.connect(
                "clicked", lambda _btn: self.cancel_generation(window, cancel_event)
            )
            box.append(label)
            box.append(bar)
            box.append(cancel_button)
            window.set_child(box)
            window.set_default_size(320, -1)
            window.present()
            state["window"] = window
            state["bar"] = bar
            state["pulse_id"] = GLib.timeout_add(150, pulse)
            return False

        def pulse():
            bar = state.get("bar")
            if not bar:
                return False
            bar.pulse()
            return True

        GLib.idle_add(show_window)
        return state

    def update_generate_list_progress(self, state, text):
        bar = state.get("bar")
        if not bar:
            return False
        bar.set_text(text)
        return False

    def close_generate_list_progress(self, state):
        def close():
            pulse_id = state.get("pulse_id")
            if pulse_id:
                GLib.source_remove(pulse_id)
            window = state.get("window")
            if window:
                window.close()
            return False

        GLib.idle_add(close)

    def cancel_generation(self, window, cancel_event):
        cancel_event.set()
        window.close()

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

    def ensure_entry_focus(self):
        if not self.entry.has_focus():
            self.entry.grab_focus()

    def update_window_height(self):
        if not getattr(self, "window", None):
            return
        self.window.set_default_size(520, -1)

    def maybe_check_for_updates(self):
        settings = self.load_settings()
        last_check = settings.get("last_update_check", 0)
        now = int(time.time())
        if now - last_check < 7 * 24 * 60 * 60:
            return
        settings["last_update_check"] = now
        self.save_settings(settings)

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

    def load_settings(self):
        if not self.settings_path.exists():
            return {}
        try:
            return json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def save_settings(self, data):
        try:
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            self.settings_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            pass

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
