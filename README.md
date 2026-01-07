![Spelling Bee icon](spelling-bee-tts.png)

Install
-------

System dependencies (Ubuntu):

```
sudo apt install python3-gi gir1.2-gtk-4.0 mpv
```

Python dependencies:

```
python3 -m pip install --user -r requirements.txt
```

Run:

```
python3 -m spellingbee
```

Notes:
- `edge-tts` requires network access.
- You can override the voice with `EDGE_TTS_VOICE` (default: `en-US-AriaNeural`).
