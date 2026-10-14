"""Live translation to English for the subtitle bar and saved recordings, adapted from Live Subtitles'
translate_worker.py (github.com/ted91/teams-subtitles): opus-mt-<lang>-en models run with CTranslate2
(int8, CPU), with a per-language glossary of forced terms.

Per update, the utterance's words are split into closed sentences (end in . ? !) -- translated once and
cached -- and the open tail, re-translated every update. To show which English words are stable, the
tail is translated twice in one batch: committed words only, and committed + draft words; their common
prefix is stable, the rest is "dim" (it may still change).

Models: <models_dir>/opus-mt-<lang>-en-ct2/ (model.bin, source.spm, target.spm). Glossaries:
<models_dir>/glossary_<lang>.txt, one "term => English" per line.
"""
import logging
import os
import re
import threading

log = logging.getLogger("live_translator")

LANG_NAMES = {"de": "German", "fr": "French", "es": "Spanish", "it": "Italian", "nl": "Dutch", "pl": "Polish",
              "pt": "Portuguese", "ru": "Russian", "sv": "Swedish", "cs": "Czech", "da": "Danish", "fi": "Finnish"}
_PUNCT = re.compile(r"[^\w]+", re.UNICODE)
MIN_TAIL_WORDS = 3     # the still-growing part of a sentence is only translated once it has this many words

# Spoken fillers -- in both languages, since Parakeet often writes German "ja" as English "yeah" and the
# translation turns every "ja" into "yeah"/"yes". Used by the subtitle bar to keep lines readable.
FILLERS = {"yeah", "yes", "yep", "ja", "jo", "jaja", "joa", "okay", "ok", "hm", "hmm", "mhm", "mm", "mmh",
           "äh", "ähm", "eh", "ehm", "uh", "um", "uhm", "er", "erm", "ah", "oh", "na", "so", "also", "genau"}


def clean_subtitle(text: str) -> str:
    """Display-only cleanup for the subtitle bar: collapses repeated fillers ("yeah yeah yeah" -> "yeah")
    and returns "" for a line made only of fillers. Real content is never changed."""
    words = (text or "").split()
    out = []
    for w in words:
        n = _PUNCT.sub("", w.lower())
        if n in FILLERS and out and _PUNCT.sub("", out[-1].lower()) == n:
            continue
        out.append(w)
    if not out or all(_PUNCT.sub("", w.lower()) in FILLERS for w in out):
        return ""
    while _PUNCT.sub("", out[0].lower()) in FILLERS:   # "Yeah. So... I think we should" -> "I think we should"
        out.pop(0)
    out[0] = out[0][:1].upper() + out[0][1:]
    return " ".join(out)


# Parakeet picks the language per word and writes a German "ja" as "Yeah." -- put it back before
# translating so the German model sees German (opus-mt-de-en passes an English "Yeah" straight through).
_ENGLISH_YES = {"de": "ja", "nl": "ja", "sv": "ja", "da": "ja"}


def _fix_source_words(words, lang):
    ja = _ENGLISH_YES.get(lang)
    if not ja:
        return list(words)
    out = []
    for w in words:
        n = _norm(w)
        if n in ("yeah", "yes", "yep", "ya"):
            core = ja.capitalize() if w[:1].isupper() else ja
            w = core + w[len(w.rstrip(".,!?…;:")):]           # keep the punctuation
        out.append(w)
    return out


def _models_dir() -> str:
    import live_transcriber
    return live_transcriber.models_dir()


def _model_dir(lang: str) -> str:
    return os.path.join(_models_dir(), f"opus-mt-{lang}-en-ct2")


_langs_cache = (0.0, [])


def available_languages() -> list:
    """Source languages with an installed opus-mt-<lang>-en model, e.g. ['de', 'es', 'fr'] (cached 30 s:
    the subtitle bar asks several times a second)."""
    global _langs_cache
    import time
    if time.monotonic() - _langs_cache[0] < 30:
        return _langs_cache[1]
    _langs_cache = (time.monotonic(), _scan_languages())
    return _langs_cache[1]


def _scan_languages() -> list:
    try:
        import ctranslate2  # noqa: F401
        import sentencepiece  # noqa: F401
    except ImportError:
        return []
    d = _models_dir()
    if not os.path.isdir(d):
        return []
    out = []
    for name in sorted(os.listdir(d)):
        if name.startswith("opus-mt-") and name.endswith("-en-ct2") and os.path.exists(os.path.join(d, name, "model.bin")):
            out.append(name[len("opus-mt-"):-len("-en-ct2")])
    return out


def lang_name(code: str) -> str:
    return LANG_NAMES.get(code, (code or "").upper())


def current_language() -> str:
    """The spoken language to translate from ("" = translation off)."""
    import settings
    lang = settings.get_all().get("subtitle_translate_from", "")
    return lang if lang in available_languages() else ""


def _norm(w):
    return _PUNCT.sub("", w.lower())


def _ends_sentence(word):
    return word.endswith((".", "?", "!", "…"))


def _common_prefix_len(a, b):
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


class _Glossary:
    """Term overrides: applied to the source before MT (forces the term) and to the English after."""

    def __init__(self, path):
        pairs = []
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.split("#", 1)[0].strip()
                    if "=>" in line:
                        src, en = (s.strip() for s in line.split("=>", 1))
                        if src and en:
                            pairs.append((src, en))
        pairs.sort(key=lambda p: -len(p[0]))
        self.rules = [(re.compile(r"(?<!\w)" + re.escape(s) + r"(?!\w)", re.IGNORECASE), en) for s, en in pairs]

    def apply(self, text):
        for rx, en in self.rules:
            text = rx.sub(en, text)
        return text


class Translator:
    """One loaded opus-mt model per language (loaded on first use, ~1 s each), shared by all recordings."""

    def __init__(self, threads: int = 2, beam_size: int = 1):
        self.threads, self.beam = threads, beam_size
        self._models, self._glossaries, self._cache = {}, {}, {}
        self._lock = threading.Lock()

    def _model(self, lang):
        if lang not in self._models:
            import ctranslate2
            import sentencepiece as spm
            d = _model_dir(lang)
            ct = ctranslate2.Translator(d, device="cpu", compute_type="int8", inter_threads=1, intra_threads=self.threads)
            self._models[lang] = (ct, spm.SentencePieceProcessor(model_file=os.path.join(d, "source.spm")),
                                  spm.SentencePieceProcessor(model_file=os.path.join(d, "target.spm")))
            self._glossaries[lang] = _Glossary(os.path.join(_models_dir(), f"glossary_{lang}.txt"))
        return self._models[lang], self._glossaries[lang]

    def translate_batch(self, texts, lang):
        with self._lock:
            (ct, sp_src, sp_tgt), g = self._model(lang)
            src = [sp_src.encode(g.apply(t), out_type=str) + ["</s>"] for t in texts]
            res = ct.translate_batch(src, beam_size=self.beam, max_decoding_length=256)
            return [g.apply(sp_tgt.decode(r.hypotheses[0])) for r in res]

    def sentence(self, text, lang):
        key = (lang, text)
        if key not in self._cache:
            if len(self._cache) > 500:
                self._cache.clear()
            self._cache[key] = self.translate_batch([text], lang)[0]
        return self._cache[key]

    def update(self, committed, draft, final, lang):
        """-> {"sentences": [closed English sentences], "stable": str, "dim": str}"""
        committed, draft = _fix_source_words(committed, lang), _fix_source_words(draft, lang)
        sentences, cur = [], []
        for w in committed:
            cur.append(w)
            if _ends_sentence(w):
                sentences.append(" ".join(cur))
                cur = []
        closed = [self.sentence(s, lang) for s in sentences]
        tail_c, tail_f = " ".join(cur), " ".join(cur + list(draft))
        if final:
            if tail_f:
                closed.append(self.sentence(tail_f, lang))
            return {"sentences": closed, "stable": "", "dim": ""}
        if not tail_f:
            return {"sentences": closed, "stable": "", "dim": ""}
        if len(tail_f.split()) < MIN_TAIL_WORDS:
            # A 1-2 word fragment at the start of a sentence ("Ja", "Also", "Und") translates to filler
            # like "Yeah." / "So." that then flickers away -- wait for a little context first.
            return {"sentences": closed, "stable": "", "dim": ""}
        if tail_c and draft:
            e_c, e_f = self.translate_batch([tail_c, tail_f], lang)
            fw, cw = e_f.split(), e_c.split()
            k = _common_prefix_len([_norm(x) for x in cw], [_norm(x) for x in fw])
            return {"sentences": closed, "stable": " ".join(fw[:k]), "dim": " ".join(fw[k:])}
        e_f = self.translate_batch([tail_f], lang)[0]
        return {"sentences": closed, "stable": "" if draft else e_f, "dim": e_f if draft else ""}


_translator = None
_translator_lock = threading.Lock()


def get_translator() -> Translator:
    global _translator
    with _translator_lock:
        if _translator is None:
            _translator = Translator()
        return _translator


def preload_in_background():
    lang = current_language()
    if lang:
        threading.Thread(target=lambda: get_translator().sentence("Hallo.", lang), daemon=True,
                         name="translator-preload").start()
