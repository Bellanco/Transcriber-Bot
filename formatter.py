"""Formateo de transcripciones y párrafos."""

import re
from typing import Optional, List, Dict, Any, Tuple

from config import (
    PAUSE_THRESHOLD,
    SHORT_PAUSE_THRESHOLD,
    MAX_PARAGRAPH_CHARS,
    MAX_TELEGRAM_LENGTH,
)


def _seg_attr(seg: Any, key: str, default: Any = None) -> Any:
    """
    Lee un atributo de un segmento de Whisper de forma segura.
    El SDK de Groq devuelve objetos, no dicts, así que probamos ambas formas.
    """
    if isinstance(seg, dict):
        return seg.get(key, default)
    return getattr(seg, key, default)


_SENTENCE_END = re.compile(r'[.?!\u2026\u203c\u2049]"?\s*$')


def _ends_sentence(text: str) -> bool:
    """Devuelve True si el texto termina en cierre de frase."""
    return bool(_SENTENCE_END.search(text))


def _remove_control_chars(text: str) -> str:
    """Elimina caracteres de control del texto."""
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)


def paragraphs_from_segments(segments: List[Any]) -> str:
    """
    Agrupa los segmentos de Whisper en párrafos con tres niveles de corte:

      1. Pausa larga (>= PAUSE_THRESHOLD)
         → siempre abre párrafo nuevo.

      2. Pausa corta (>= SHORT_PAUSE_THRESHOLD) + segmento acaba en .?!
         → pausa natural tras terminar la idea.

      3. Párrafo >= MAX_PARAGRAPH_CHARS + segmento acaba en .?!
         → evita bloques enormes sin romper frases.

    Si ningún criterio se cumple, el segmento se une al párrafo actual.
    """
    if not segments:
        return ""

    paragraphs: List[str] = []
    current: List[str] = []
    current_chars = 0

    for i, seg in enumerate(segments):
        raw = _seg_attr(seg, "text", "") or ""
        text = _remove_control_chars(raw.strip())
        if not text:
            continue

        current.append(text)
        current_chars += len(text)

        is_last = i == len(segments) - 1
        ends_sentence = _ends_sentence(text)

        # Calcular pausa con el siguiente segmento
        gap = 0.0
        if not is_last:
            end = _seg_attr(seg, "end", 0) or 0
            start = _seg_attr(segments[i + 1], "start", 0) or 0
            gap = max(0.0, start - end)

        # Criterios de corte
        long_pause = not is_last and gap >= PAUSE_THRESHOLD
        short_pause = not is_last and gap >= SHORT_PAUSE_THRESHOLD and ends_sentence
        too_long = current_chars >= MAX_PARAGRAPH_CHARS and ends_sentence

        if long_pause or short_pause or too_long:
            paragraphs.append(" ".join(current))
            current = []
            current_chars = 0

    if current:
        paragraphs.append(" ".join(current))

    return "\n\n".join(paragraphs)


def clean_transcription(text: str) -> str:
    """
    Limpieza básica:
    - Elimina caracteres de control
    - Colapsa espacios múltiples
    - Mayúscula tras punto
    """
    text = _remove_control_chars(text)
    text = re.sub(r" {2,}", " ", text)
    text = re.sub(
        r"(\.) ([a-záéíóúüñ])",
        lambda m: m.group(1) + " " + m.group(2).upper(),
        text,
    )
    return text.strip()


def _normalize_segment(seg: Any, offset: float = 0.0) -> Optional[Dict[str, Any]]:
    """Convierte un segmento de Groq a dict normalizado con offset opcional."""
    raw_text = _seg_attr(seg, "text", "") or ""
    text = _remove_control_chars(raw_text.strip())
    if not text:
        return None

    start = float(_seg_attr(seg, "start", 0.0) or 0.0) + offset
    end = float(_seg_attr(seg, "end", 0.0) or 0.0) + offset
    if end < start:
        end = start

    return {
        "text": text,
        "start": start,
        "end": end,
    }


def _plain_from_segments(segments: List[Dict[str, Any]]) -> str:
    """Construye texto plano limpio a partir de segmentos normalizados."""
    if not segments:
        return ""
    plain = " ".join(s["text"] for s in segments if s.get("text"))
    return clean_transcription(plain)


def _merge_segments_with_overlap(segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Ordena y deduplica segmentos por ventana temporal."""
    if not segments:
        return []

    ordered = sorted(
        segments,
        key=lambda s: (float(s.get("start", 0.0)), float(s.get("end", 0.0))),
    )
    merged: List[Dict[str, Any]] = []

    for seg in ordered:
        if not merged:
            merged.append(seg)
            continue

        prev = merged[-1]
        same_text = (
            seg.get("text", "").strip().lower()
            == prev.get("text", "").strip().lower()
        )
        close_time = (
            abs(float(seg.get("start", 0.0)) - float(prev.get("start", 0.0)))
            <= 1.2
        )

        if same_text and close_time:
            continue

        merged.append(seg)

    return merged


def parse_transcription_result(
    result: Any, offset: float = 0.0
) -> Tuple[str, List[Dict[str, Any]]]:
    """Extrae texto plano y segmentos normalizados desde la respuesta de Groq."""
    raw_segments = getattr(result, "segments", None) or []
    segments: List[Dict[str, Any]] = []

    for seg in raw_segments:
        normalized = _normalize_segment(seg, offset=offset)
        if normalized:
            segments.append(normalized)

    if segments:
        plain = _plain_from_segments(segments)
    else:
        plain = clean_transcription(getattr(result, "text", "") or "")

    return plain, segments


# ── Partición de mensajes largos ──────────────────────────────────────────────


async def stream_text(message: "Message", text: str) -> Optional["Message"]:  # type: ignore
    """
    Revela el texto párrafo a párrafo editando el mismo mensaje.
    Si el mensaje acumulado supera el límite de Telegram, abre uno nuevo.
    Devuelve el último mensaje enviado.
    """
    import asyncio
    from telegram.error import BadRequest, TelegramError

    paragraphs = [p for p in text.split("\n\n") if p.strip()]
    if not paragraphs:
        return await message.reply_text(text or "—")

    chunks: List[str] = []
    for paragraph in paragraphs:
        chunks.extend(split_text(paragraph) or [paragraph])

    sent = await message.reply_text(chunks[0])
    accumulated = chunks[0]

    from config import STREAM_DELAY

    for paragraph in chunks[1:]:
        await asyncio.sleep(STREAM_DELAY)
        candidate = accumulated + "\n\n" + paragraph

        if len(candidate) > MAX_TELEGRAM_LENGTH:
            # El bloque no cabe: enviar mensaje nuevo
            sent = await message.reply_text(paragraph)
            accumulated = paragraph
        else:
            try:
                await sent.edit_text(candidate)
                accumulated = candidate
            except BadRequest:
                sent = await message.reply_text(paragraph)
                accumulated = paragraph
            except TelegramError:
                pass

    return sent


def split_text(text: str, limit: int = MAX_TELEGRAM_LENGTH) -> List[str]:
    """
    Divide texto largo en trozos respetando párrafos, frases y palabras.
    Nunca corta a mitad de palabra.
    """
    text = text.strip()
    if not text:
        return []

    parts = []
    remaining = text

    while len(remaining) > limit:
        # Intentar cortar en párrafo, luego frase, luego espacio
        cut = remaining.rfind("\n\n", 0, limit)
        if cut == -1:
            cut = remaining.rfind("\n", 0, limit)
        if cut == -1:
            cut = remaining.rfind(". ", 0, limit)
        if cut == -1:
            cut = remaining.rfind(" ", 0, limit)
        if cut == -1:
            cut = limit

        parts.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()

    if remaining:
        parts.append(remaining)

    return [p for p in parts if p]


def _truncate_words(text: str, max_words: int = 22) -> str:
    """Recorta una frase a un máximo de palabras para mantenerla escaneable."""
    words = text.split()
    if len(words) <= max_words:
        return text.strip()
    return " ".join(words[:max_words]).strip(" ,;:") + "..."


def _extract_json_payload(raw_text: str) -> Optional[str]:
    """Extrae un bloque JSON desde texto libre o dentro de ```json ... ```."""
    if not raw_text:
        return None

    fenced = re.search(
        r"```json\s*(.*?)\s*```", raw_text, flags=re.DOTALL | re.IGNORECASE
    )
    if fenced:
        return fenced.group(1).strip()

    # Fallback: intenta tomar desde el primer '[' hasta el ultimo ']'.
    start = raw_text.find("[")
    end = raw_text.rfind("]")
    if start != -1 and end != -1 and end > start:
        return raw_text[start : end + 1].strip()

    return None


def _format_summary_from_topics(topics: List[Dict[str, Any]]) -> str:
    """Renderiza temas en formato fijo y en orden cronológico."""
    if not topics:
        return ""

    normalized: List[Tuple[float, int, str, str]] = []

    for idx, item in enumerate(topics):
        if not isinstance(item, dict):
            continue

        topic = str(item.get("tema", "")).strip(" .,-") or "Tema"
        summary = str(item.get("resumen", "")).strip(" .,-")
        if not summary:
            continue

        order_raw = item.get("posicion_inicial", None)
        try:
            order_value = float(order_raw)
        except (TypeError, ValueError):
            order_value = float("inf")

        normalized.append((order_value, idx, topic, _truncate_words(summary)))

    if not normalized:
        return ""

    normalized.sort(key=lambda t: (t[0], t[1]))
    bullets = [
        f"• {topic}: {summary}"
        for _, _, topic, summary in normalized
    ]
    return "\n\n".join(bullets)


def format_summary(text: str) -> str:
    """
    Normaliza el resumen para formato de vistazo:
    - Una línea por tema
    - Formato fijo: "• Tema: frase breve"
    - Frases compactas para lectura rápida
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    bullets: List[str] = []

    for raw_line in lines:
        line = re.sub(r"^[\*\-•·]\s+", "", raw_line)
        line = re.sub(r"^\d+[\.)]\s+", "", line)
        line = re.sub(r"\s+", " ", line).strip()
        if not line:
            continue

        if ":" in line:
            topic, summary = line.split(":", 1)
            topic = topic.strip(" .,-") or "Tema"
            summary = summary.strip(" .,-")
            if not summary:
                continue
            summary = _truncate_words(summary)
            bullets.append(f"• {topic}: {summary}")
        else:
            summary = _truncate_words(line.strip(" .,-"))
            if summary:
                bullets.append(f"• Tema: {summary}")

    if bullets:
        return "\n\n".join(bullets)

    compact = re.sub(r"\s+", " ", text).strip()
    if not compact:
        return "• Tema general: No se detectaron ideas suficientes para resumir."
    return f"• Tema general: {_truncate_words(compact)}"
