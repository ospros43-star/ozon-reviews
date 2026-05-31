import anthropic as _anthropic_module
from app.config import settings

SYSTEM_PROMPT = """Ты — живой, дружелюбный представитель бренда KEDROVICH на маркетплейсе Ozon.
Пишешь ответы на отзывы покупателей. Твой стиль:
— Тёплый, человечный, как будто отвечает реальный человек а не робот
— Без канцелярита и шаблонных фраз типа "Спасибо за отзыв, нам важно ваше мнение"
— Конкретный: цепляйся за детали из отзыва, называй товар по имени
— Лёгкий и приятный: покупатель должен улыбнуться читая ответ
— Всегда на русском языке
— Не более 1000 символов
— Верни только текст ответа, без кавычек и пояснений"""

POSITIVE_TEMPLATE = """Покупатель {author} оставил отзыв {rating}★ на товар «{product}»:
"{text}"

Напиши живой дружелюбный ответ. Требования:
- Обратись по имени если оно не "покупатель"
- Зацепись за конкретную деталь из отзыва (если она есть)
- Порадуйся вместе с покупателем, покажи что тебе не всё равно
- Пригласи вернуться — но не шаблонно, а с теплотой
- Максимум 2-3 предложения, без воды
- НЕ используй: "Спасибо за отзыв", "Нам важно ваше мнение", "Рады что вам понравилось"

Примеры хорошего тона:
✓ "Ореховое настроение обеспечено 😊 Макадамия — наш фаворит, рады что и вам она пришлась по вкусу!"
✓ "Приятно слышать, {author}! Фисташки у нас правда отборные — ждём вас снова 🌿"
"""

NEGATIVE_TEMPLATE = """Покупатель {author} оставил отзыв {rating}★ на товар «{product}»:
"{text}"

Напиши искренний человечный ответ. Требования:
- Признай проблему честно, без оправданий
- Извинись — но коротко и по-человечески, не раздувай
- Предложи написать в чат Ozon — мы разберёмся
- Тон: спокойный, заботливый, не заискивающий
- 3-4 предложения максимум
- НЕ используй: "Нам очень жаль", "Приносим извинения за доставленные неудобства", "Ваше мнение важно для нас"

Примеры хорошего тона:
✓ "Это точно не то, чего мы хотели для вас 😔 Напишите нам в чат Ozon — посмотрим что пошло не так и исправим ситуацию."
✓ "Горькие орехи — это брак, и это неприемлемо. Напишите в чат, разберёмся и компенсируем."
"""

IMPROVE_TEMPLATE = """Вот ответ продавца на отзыв покупателя {author} ({rating}★):

Отзыв: "{review_text}"
Текущий ответ: "{existing_response}"

Сделай ответ живее и человечнее. Сохрани смысл, но убери канцелярит и шаблоны.
Добавь тепла, конкретики, лёгкости. Не более 1000 символов."""


class AIServiceError(Exception):
    pass


def _backend() -> str:
    if settings.groq_api_key:
        return "groq"
    if settings.anthropic_api_key:
        return "claude"
    return "gemini"


async def generate_response(
    rating: int,
    review_text: str | None,
    product_name: str,
    author_name: str,
) -> str:
    from app.services.response_settings import load as _load_rs
    rs = _load_rs()

    is_positive = rating >= 4
    template = POSITIVE_TEMPLATE if is_positive else NEGATIVE_TEMPLATE

    # Добавляем инструкцию о компенсации в негативный промпт
    if not is_positive and rs.get("compensation"):
        template = template + "\n- Предложи компенсацию: напиши что покупатель может написать нам в чат Ozon и мы разберёмся и компенсируем неудобства"

    prompt = template.format(
        rating=rating,
        product=product_name or "наш товар",
        text=review_text or "Без текста",
        author=author_name or "покупатель",
    )
    b = _backend()
    if b == "groq":   result = await _groq_generate(prompt)
    elif b == "claude": result = await _claude_generate(prompt)
    else:               result = await _gemini_generate(prompt)

    # Добавляем подпись если включена и задана
    if rs.get("signature_enabled") and rs.get("signature", "").strip():
        result = result.rstrip() + "\n\n" + rs["signature"].strip()

    return result


async def improve_response(
    existing_response: str,
    review_text: str | None,
    rating: int,
    author_name: str = "",
) -> str:
    prompt = IMPROVE_TEMPLATE.format(
        rating=rating,
        review_text=review_text or "Без текста",
        existing_response=existing_response,
        author=author_name or "покупатель",
    )
    b = _backend()
    if b == "groq": return await _groq_generate(prompt)
    if b == "claude": return await _claude_generate(prompt)
    return await _gemini_generate(prompt)


_GROQ_MODELS = [
    "llama-3.3-70b-versatile",   # качество выше
    "llama-3.1-8b-instant",      # 500k токенов/день — fallback при лимите
]


async def _groq_generate(prompt: str) -> str:
    import asyncio
    from groq import AsyncGroq, RateLimitError
    client = AsyncGroq(api_key=settings.groq_api_key)

    for attempt in range(3):  # до 3 попыток с паузой при TPM
        last_exc = None
        for model in _GROQ_MODELS:
            try:
                msg = await client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    max_tokens=800,
                    temperature=0.85,
                )
                return msg.choices[0].message.content.strip()
            except RateLimitError as exc:
                last_exc = exc
                continue  # попробуем следующую модель
            except Exception as exc:
                raise AIServiceError(str(exc)) from exc

        # Все модели отклонены — ждём и повторяем (TPM сбрасывается за ~1 мин)
        if attempt < 2:
            await asyncio.sleep(30)

    raise AIServiceError(str(last_exc)) from last_exc


async def _claude_generate(prompt: str) -> str:
    try:
        client = _anthropic_module.AsyncAnthropic(api_key=settings.anthropic_api_key)
        msg = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=800,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception as exc:
        raise AIServiceError(str(exc)) from exc


async def _gemini_generate(prompt: str) -> str:
    try:
        from google import genai
        from google.genai import types
        response = await genai.Client(api_key=settings.gemini_api_key).aio.models.generate_content(
            model=settings.gemini_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                max_output_tokens=800,
                temperature=0.85,
            ),
        )
        return response.text.strip()
    except Exception as exc:
        raise AIServiceError(str(exc)) from exc
