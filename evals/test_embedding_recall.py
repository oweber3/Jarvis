"""Memory recall with a real embedding model, across languages.

Diary-style memories in six languages are embedded with the model under test, then found again from
paraphrased questions that share almost no words with them: once in the memory's own language and once in
English. Each memory has a distractor in the same language that shares its words but states a different
fact. The vectors alone are scored by whether the memory is the top hit (and by mean reciprocal rank);
``Database.search_hybrid``, the path diary search uses, by whether it is in the top 3.

    EVAL_EMBED_MODEL=embeddinggemma-2:270m pytest evals/test_embedding_recall.py -s

The default is the shipped embedding model. The test skips when Ollama or the model is unavailable.
"""

import json
import math
import os
import time

import pytest
import requests

from helpers import MockConfig
from jarvis.llm.factory import get_embedding_backend
from jarvis.memory.db import Database
from jarvis.utils.vector_store import PythonVectorStore

pytestmark = pytest.mark.eval

MODEL = os.environ.get("EVAL_EMBED_MODEL", "").strip() or MockConfig().ollama_embed_model

# (language, memory, question in the same language, question in English)
MEMORIES = [
    ("en", "Booked the car in for its annual MOT at the garage on Friday; the brake pads need replacing.",
     "When is my vehicle getting its yearly safety inspection?", None),
    ("en", "Sarah said her daughter starts university in Edinburgh this September, studying marine biology.",
     "Which subject is my friend's child going to read at uni?", None),
    ("en", "Started taking magnesium before bed because the doctor thought it might help with leg cramps.",
     "Why did I begin a new supplement at night?", None),
    ("es", "Mi hermano Javier se muda a Valencia en marzo porque consiguió trabajo en un hospital.",
     "¿Por qué cambia de ciudad mi familiar a principios de primavera?",
     "Why is my brother relocating next spring?"),
    ("es", "La caldera hace un ruido extraño por las mañanas; el técnico viene el jueves a revisarla.",
     "¿Cuándo vendrá alguien a arreglar la calefacción?",
     "When is the repair person coming to look at the heating?"),
    ("es", "Decidí aprender a tocar el piano; las clases son los martes con una profesora del barrio.",
     "¿Qué instrumento musical estoy estudiando y con quién?",
     "Which musical instrument am I learning, and from whom?"),
    ("de", "Unser Flug nach Lissabon wurde auf Samstagmorgen verschoben, Abflug jetzt um 6:40 Uhr.",
     "Wann geht unsere Reise nach Portugal jetzt los?",
     "What time does our trip to Portugal leave now?"),
    ("de", "Die Katze frisst seit zwei Tagen kaum etwas, morgen habe ich einen Termin beim Tierarzt.",
     "Was ist mit meinem Haustier los?",
     "What is wrong with my pet?"),
    ("de", "Im Garten wollen wir dieses Jahr Tomaten, Zucchini und Basilikum im Hochbeet anpflanzen.",
     "Welches Gemüse planen wir draußen anzubauen?",
     "Which vegetables are we planning to grow outside?"),
    ("tr", "Annemin doğum günü için İzmir'de bir balık restoranında masa ayırttım, cumartesi akşamı.",
     "Annemin kutlaması için nerede yemek yiyeceğiz?",
     "Where are we eating to celebrate my mum?"),
    ("tr", "Ev kirası gelecek aydan itibaren yüzde yirmi artıyor; ev sahibi bugün mesaj attı.",
     "Oturduğum dairenin ödemesi neden değişiyor?",
     "Why is the payment for my flat changing?"),
    ("tr", "Sabahları koşmaya başladım, şimdilik haftada üç gün beş kilometre yapıyorum.",
     "Ne sıklıkla spor yapıyorum?",
     "How often am I exercising?"),
    ("ja", "来週の水曜日に歯医者の予約を入れた。親知らずを抜くかもしれない。",
     "口の治療はいつ受けますか？",
     "When is my dental appointment?"),
    ("ja", "会社の同僚の田中さんが十月に結婚するので、お祝いのプレゼントを探している。",
     "職場の人の結婚祝いに何を準備していますか？",
     "What am I getting ready for a colleague's wedding?"),
    ("ja", "最近よく眠れないので、寝る前にスマホを見るのをやめることにした。",
     "睡眠の質を良くするために何を決めましたか？",
     "What did I decide to do to sleep better?"),
    ("fr", "Le notaire a fixé la signature de l'achat de l'appartement au 14 novembre à Lyon.",
     "Quand est-ce que je deviens officiellement propriétaire ?",
     "When does the flat purchase become official?"),
    ("fr", "Ma fille a une allergie aux arachides confirmée par le pédiatre la semaine dernière.",
     "Mon enfant doit-elle éviter certains aliments ?",
     "Does my child need to avoid any foods?"),
    ("fr", "Je me suis inscrit à un cours de poterie le jeudi soir pour me détendre après le travail.",
     "Quelle activité manuelle ai-je commencée en semaine ?",
     "Which craft did I take up on weekday evenings?"),
]

# One per memory, same language and wording, different fact: a model that matches words or topic rather
# than meaning ranks these with the target.
DISTRACTORS = [
    "The garage rang about the MOT on my old motorbike; I sold it last year so I told them to cancel.",
    "Sarah studied marine biology in Edinburgh herself, years ago, before she moved south.",
    "The doctor said magnesium levels in my blood test were normal, nothing to take.",
    "Javier trabajaba en un hospital de Valencia antes de jubilarse el año pasado.",
    "El técnico de la caldera dijo en marzo que todo estaba bien y que no hacía falta otra revisión.",
    "La profesora del barrio vendió su piano porque se muda a otra ciudad.",
    "Lissabon war unser Ziel vor drei Jahren, damals mit dem Auto statt mit dem Flug.",
    "Die Katze der Nachbarn war beim Tierarzt zur Impfung, alles in Ordnung.",
    "Die Tomaten vom Markt waren dieses Jahr teuer, Basilikum gab es gar nicht.",
    "İzmir'deki balık restoranı kapanmış, annem geçen hafta haber verdi.",
    "Ev sahibi bu ay mutfak musluğunu tamir ettirdi, kira konusu açılmadı.",
    "Kardeşim sabahları beş kilometre koşuyor ama ben hiç katılmadım.",
    "歯医者の受付の仕事に田中さんの妹が応募したらしい。",
    "田中さんは去年の十月に引っ越した。",
    "スマホの新しい機種を買ったが、寝る前にはあまり使わない。",
    "Le notaire de Lyon a pris sa retraite et son cabinet a fermé en novembre.",
    "La pédiatre de ma fille part en congé la semaine prochaine.",
    "Le cours de poterie du jeudi a été annulé faute d'inscrits, je n'y suis pas allé.",
]


def _available(model: str) -> bool:
    try:
        tags = requests.get(f"{MockConfig().ollama_base_url}/api/tags", timeout=3).json()
    except Exception:
        return False
    names = {m.get("name", "") for m in tags.get("models", [])}
    return model in names or f"{model}:latest" in names


def _cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)) or 1.0)


@pytest.mark.skipif(not _available(MODEL), reason=f"Ollama or embedding model '{MODEL}' unavailable")
def test_memories_are_found_from_paraphrased_questions(tmp_path):
    cfg = MockConfig(embedding_model=MODEL)
    backend = get_embedding_backend(cfg)

    def embed(text):
        vec = backend.embed(text, MODEL, timeout_sec=60)
        assert vec, f"no embedding returned by {MODEL}"
        return vec

    embed("warm-up")
    started = time.perf_counter()
    corpus = [(m[1], embed(m[1])) for m in MEMORIES] + [(d, embed(d)) for d in DISTRACTORS]
    per_text_ms = (time.perf_counter() - started) * 1000 / len(corpus)

    db = Database(str(tmp_path / "recall.db"))
    db._python_vector_store = PythonVectorStore(db.db_path)
    try:
        ids = []
        for index, (text, vec) in enumerate(corpus):
            sid = db.upsert_conversation_summary(f"2026-0{1 + index // 28}-{1 + index % 28:02}", text)
            db.upsert_summary_embedding(sid, vec)
            ids.append(sid)

        scores = {}
        for subset, column in (("same language", 2), ("asked in English", 3)):
            vector_hits = hybrid_hits = asked = 0
            reciprocal = 0.0
            by_language = {}
            for index, memory in enumerate(MEMORIES):
                question = memory[column]
                if question is None:
                    continue
                asked += 1
                qvec = embed(question)
                ranked = sorted(range(len(corpus)), key=lambda i: -_cosine(qvec, corpus[i][1]))
                vector_hit = ranked[0] == index
                reciprocal += 1 / (ranked.index(index) + 1)
                rows = db.search_hybrid(question, json.dumps(qvec), top_k=3)
                hybrid_hits += ids[index] in [row["id"] for row in rows]
                vector_hits += vector_hit
                hit, total = by_language.get(memory[0], (0, 0))
                by_language[memory[0]] = (hit + vector_hit, total + 1)
            scores[subset] = (vector_hits, hybrid_hits, asked)
            mrr = reciprocal / asked
            languages = ", ".join(f"{lang} {hit}/{total}" for lang, (hit, total) in by_language.items())
            print(f"\n📊 {MODEL} · {subset}: top hit {vector_hits}/{asked} (MRR {mrr:.2f}), "
                  f"hybrid top 3 {hybrid_hits}/{asked}")
            print(f"   🌍 top hit by language: {languages}")
        print(f"   ⏱️ {per_text_ms:.0f} ms per memory, {len(corpus[0][1])} dimensions")
    finally:
        db.close()

    _, hybrid_hits, asked = scores["same language"]
    # Diary search must still find most memories from a paraphrase in the user's language; the vector-only
    # and English-question figures are printed for comparing models.
    assert hybrid_hits >= asked * 0.5, f"same-language hybrid top 3 {hybrid_hits}/{asked}"
