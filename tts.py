"""Text-to-speech for voice channels, for people without a mic.

Piper runs on the NAS CPU: a sentence takes ~0.15s to synthesise (measured
2026-10-21, 5.3s of audio in 0.16s), so what people type goes to nobody but
Discord. Voices are baked into the image at build time (see Dockerfile).
"""
import re
import wave

from piper import PiperVoice

VOICE_DIR = "/opt/voices"
VOICES = {                  # value for /tts start voice: -> Piper model
    "lessac": "en_US-lessac-medium",
    "amy": "en_US-amy-medium",
    "ryan": "en_US-ryan-medium",
}
DEFAULT_VOICE = "lessac"
MAX_CHARS = 300             # per message, so a pasted wall of text can't hog the channel

_loaded: dict[str, PiperVoice] = {}

CUSTOM_EMOJI = re.compile(r"<a?:(\w+):\d+>")
URL = re.compile(r"https?://\S+")
SPOILER = re.compile(r"\|\|.+?\|\|", re.S)
CODE = re.compile(r"```.*?```", re.S)
MARKUP = re.compile(r"[*_~`>#|]+")
SPEAKABLE = re.compile(r"[^\W_]")   # any letter or digit, in any script


def speakable(text: str) -> str | None:
    """Turn a chat message into something worth saying, or None.

    Expects clean_content, so mentions already read as @name.
    """
    text = CODE.sub(" code block ", text)
    text = SPOILER.sub(" spoiler ", text)
    text = URL.sub(" link ", text)
    text = CUSTOM_EMOJI.sub(lambda m: f" {m[1].replace('_', ' ')} ", text)
    text = MARKUP.sub(" ", text)
    text = " ".join(text.split())
    if not SPEAKABLE.search(text) or text in ("link", "spoiler"):
        return None         # a bare link or emoji isn't worth interrupting anyone for
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS].rsplit(" ", 1)[0] + ", and so on"
    return text


def preload():
    for voice in VOICES:
        _load(voice)


def _load(voice: str) -> PiperVoice:
    if voice not in _loaded:
        _loaded[voice] = PiperVoice.load(f"{VOICE_DIR}/{VOICES[voice]}.onnx")
    return _loaded[voice]


def synth(text: str, voice: str, path: str):
    """Blocking: write text as a WAV to path. Run it in a thread."""
    with wave.open(path, "wb") as w:
        _load(voice).synthesize_wav(text, w)
