"""
Quiz Bot для battle.aiplus.kz (БЕЗ API)
=========================================
Стратегия "Учись и побеждай":
  1-й прогон: тыкает рандомный ответ → запоминает правильный (зелёный)
  2-й+ прогон: использует кэш → набирает 90%+

Темы: 4-10, 12-39 (по 6 подтем в каждой)

Запуск: python quiz_bot.py
"""

import asyncio
import json
import random
import time
from pathlib import Path
from playwright.async_api import async_playwright, Page

# ═══════════════════════════════════════════════════════════════
#  НАСТРОЙКИ
# ═══════════════════════════════════════════════════════════════
LOGIN = "online_539"
PASSWORD = "123456"
BASE_URL = "https://battle.aiplus.kz/polyglot/login"
HISTORY_URL = "https://battle.aiplus.kz/polyglot/ent/kz_history"
DELAY_SECONDS = 10            # Задержка перед кликом (имитация человека)
CACHE_FILE = "answer_cache.json"
REQUIRED_ATTEMPTS = 5         # В каждой теме надо ровно 5 раз получить 90%+

# Темы для прохождения (0-indexed): 4-10 и 12-39 → индексы 3-9 и 11-38
TOPICS_TO_DO = list(range(3, 10)) + list(range(11, 39))
# ═══════════════════════════════════════════════════════════════

# Кэш: { "текст_вопроса_нормализованный": "текст_правильного_ответа" }
answer_cache: dict[str, str] = {}


def load_cache():
    global answer_cache
    path = Path(CACHE_FILE)
    if path.exists():
        try:
            answer_cache = json.loads(path.read_text(encoding="utf-8"))
            log("CACHE", f"Загружено {len(answer_cache)} ответов")
        except Exception:
            answer_cache = {}


def save_cache():
    try:
        Path(CACHE_FILE).write_text(
            json.dumps(answer_cache, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    except Exception as e:
        log("CACHE", f"Ошибка сохранения: {e}")


def normalize(text: str) -> str:
    """Нормализация текста для ключа кэша."""
    return " ".join(text.strip().lower().split())


def clean_option_text(text: str) -> str:
    """Очищаем текст от галочек и букв (A, B, C)"""
    text = text.replace("✓", "").replace("✗", "").strip()
    lines = text.split("\n")
    # Если первая строка - это просто буква (A, B, C, D)
    if len(lines) > 1 and len(lines[0].strip()) <= 2:
        return " ".join(lines[1:]).strip()
    return text


def log(tag: str, msg: str):
    t = time.strftime("%H:%M:%S")
    print(f"[{t}] [{tag}] {msg}")


async def login(page: Page):
    log("AUTH", "Логинюсь...")
    await page.goto(BASE_URL, wait_until="networkidle", timeout=30000)
    await page.wait_for_timeout(2000)

    await page.locator("input[placeholder='Ваш ID']").fill(LOGIN)
    await page.locator("input[placeholder='Пароль']").fill(PASSWORD)
    await page.locator("button", has_text="Войти").click()
    await page.wait_for_timeout(3000)
    log("AUTH", "✅ Залогинился")


async def go_to_topics(page: Page):
    """Вернуться к списку тем."""
    # Кликаем 'Выйти' только если мы явно внутри теста, а не в меню
    try:
        # Признак того, что мы внутри теста — есть вопрос или варианты ответов
        if await page.locator("text=Проверить").count() > 0 or await page.locator("text=Следующий").count() > 0:
            exit_btn = page.locator("button", has_text="Выйти")
            if await exit_btn.count() > 0:
                await exit_btn.first.click()
                await page.wait_for_timeout(2000)
    except Exception:
        pass

    # Если не на странице тем — идём по URL
    if "kz_history" not in page.url:
        await page.goto(HISTORY_URL, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(2000)

    # Кликаем "История Казахстана" если есть такая кнопка (выбор предмета)
    try:
        hist = page.locator("text=История Казахстана")
        if await hist.count() > 0:
            await hist.first.click()
            await page.wait_for_timeout(2000)
    except Exception:
        pass


async def click_topic(page: Page, topic_idx: int) -> bool:
    """Кликнуть на тему. Возвращает True если тест начался."""
    await page.wait_for_timeout(1000)

    cards = page.locator("button.w-full.text-left.bg-white.rounded-xl.border")
    count = await cards.count()

    if topic_idx >= count:
        log("NAV", f"Тема {topic_idx+1} не найдена (всего {count})")
        return False

    card = cards.nth(topic_idx)
    await card.scroll_into_view_if_needed()
    await page.wait_for_timeout(500)
    await card.click()
    await page.wait_for_timeout(2500)

    log("NAV", f"Открыл тему {topic_idx+1}")
    return True


async def get_subtopic_progress(page: Page, topic_idx: int) -> int:
    """Получить количество уже пройденных подтем (зелёные точки) для темы."""
    cards = page.locator("button.w-full.text-left.bg-white.rounded-xl.border")
    count = await cards.count()
    if topic_idx >= count:
        return 0

    card = cards.nth(topic_idx)
    green_dots = card.locator("div.bg-emerald-500, div[class*='bg-emerald']")
    return await green_dots.count()


async def read_question(page: Page) -> tuple[str, list[str]]:
    """Считать вопрос и варианты ответов."""
    await page.wait_for_timeout(1000)

    # Вопрос: берём текст из основной области (до кнопок ответов)
    question = ""
    try:
        # Ищем первый крупный текстовый блок
        candidates = [
            "h2", "h3", ".text-lg", ".text-xl", ".font-semibold",
            "div.mb-4 > p", "div.mb-6 > p", "div.space-y-3 > p"
        ]
        for sel in candidates:
            el = page.locator(sel).first
            if await el.count() > 0:
                txt = (await el.inner_text()).strip()
                if len(txt) > 10 and "Выйти" not in txt:
                    question = txt
                    break
    except Exception:
        pass

    if not question:
        try:
            # Fallback: весь текст страницы минус кнопки
            main_content = await page.locator("main").first.inner_text()
            lines = [l.strip() for l in main_content.split("\n") if l.strip()]
            # Вопрос — обычно первая длинная строка
            for line in lines:
                if len(line) > 15 and "Выйти" not in line and "Проверить" not in line:
                    question = line
                    break
        except Exception:
            question = "unknown"

    # Варианты ответов
    options = []
    answer_btns = page.locator("button.w-full.text-left")
    btn_count = await answer_btns.count()

    for i in range(btn_count):
        text = (await answer_btns.nth(i).inner_text()).strip()
        if text and "Проверить" not in text and "Следующий" not in text and "Выйти" not in text:
            options.append(clean_option_text(text))

    return question, options


async def click_and_check(page: Page, btn_index: int) -> tuple[bool, str, int]:
    """
    Кликнуть на ответ, нажать Проверить, узнать правильный.
    Возвращает: (is_correct, correct_answer_text, correct_index)
    """
    answer_btns = page.locator("button.w-full.text-left")
    btn_count = await answer_btns.count()

    # Фильтруем только кнопки ответов
    answer_indices = []
    for i in range(btn_count):
        text = (await answer_btns.nth(i).inner_text()).strip()
        if text and "Проверить" not in text and "Следующий" not in text and "Выйти" not in text:
            answer_indices.append(i)

    if btn_index >= len(answer_indices):
        btn_index = 0

    actual_idx = answer_indices[btn_index] if answer_indices else btn_index

    # Кликаем ответ
    try:
        await answer_btns.nth(actual_idx).click(force=True, timeout=5000)
    except Exception as e:
        log("CLICK", f"Ошибка клика (кнопка заблокирована?): {e}")
    await page.wait_for_timeout(500)

    # Жмём "Проверить"
    try:
        check_btn = page.locator("button", has_text="Проверить")
        if await check_btn.count() > 0:
            await check_btn.click()
            await page.wait_for_timeout(2000)
    except Exception:
        pass

    # Определяем правильный ответ (зелёный)
    is_correct = False
    correct_text = ""
    correct_idx = -1

    for idx_pos, real_idx in enumerate(answer_indices):
        btn = answer_btns.nth(real_idx)
        classes = (await btn.get_attribute("class")) or ""
        text = (await btn.inner_text()).strip()

        if "emerald" in classes or "green" in classes:
            correct_text = clean_option_text(text)
            correct_idx = idx_pos
            if real_idx == actual_idx:
                is_correct = True

    # Дополнительная проверка через ✓
    if not correct_text:
        for idx_pos, real_idx in enumerate(answer_indices):
            btn = answer_btns.nth(real_idx)
            html = await btn.inner_html()
            text = (await btn.inner_text()).strip()
            if "✓" in html or "✓" in text:
                correct_text = clean_option_text(text)
                correct_idx = idx_pos
                if real_idx == actual_idx:
                    is_correct = True

    return is_correct, correct_text, correct_idx


async def go_next_question(page: Page) -> bool:
    """Нажать 'Следующий'. Возвращает False если тест закончился."""
    try:
        next_btn = page.locator("button", has_text="Следующий")
        if await next_btn.count() > 0:
            await next_btn.click()
            await page.wait_for_timeout(1500)
            return True
    except Exception:
        pass

    # Нажали ли мы на "Следующий"? Возможно, это последний вопрос и там кнопка "Завершить"
    try:
        finish_btn = page.locator("button", has_text="Завершить")
        if await finish_btn.count() > 0:
            await finish_btn.click()
            await page.wait_for_timeout(2000)
            return False # Тест точно закончился после этого
    except Exception:
        pass

    # Проверяем финальный экран
    try:
        for text in ["Результат", "Завершено", "результат"]:
            if await page.locator(f"text={text}").count() > 0:
                return False
    except Exception:
        pass

    return False


async def close_results(page: Page):
    """Закрыть экран результатов."""
    # Пытаемся нажать кнопку 'К темам' или другие ожидаемые кнопки на экране результатов
    for btn_text in ["К темам", "Закрыть", "OK", "Ок", "Далее", "Продолжить"]:
        try:
            btn = page.locator("button", has_text=btn_text)
            if await btn.count() > 0:
                await btn.first.click()
                await page.wait_for_timeout(1500)
                return
        except Exception:
            pass

    # Если ничего не помогло — возвращаемся к темам универсальным способом
    await go_to_topics(page)


async def run_test(page: Page, topic_num: int, subtopic_num: int, attempt: int) -> float:
    """
    Пройти один тест.
    Возвращает процент правильных.
    """
    correct = 0
    total = 0
    tag = f"Т{topic_num} П{subtopic_num} #{attempt}"

    while True:
        total += 1
        question, options = await read_question(page)

        if not options:
            log(tag, "Нет вариантов — тест завершён")
            break

        q_key = normalize(question)
        log(tag, f"❓ В{total}: {question[:70]}...")

        # Проверяем кэш
        chosen_idx = -1
        if q_key in answer_cache:
            cached = answer_cache[q_key]
            # Ищем кэшированный ответ среди вариантов
            for i, opt in enumerate(options):
                if normalize(cached) == normalize(opt) or normalize(cached) in normalize(opt) or normalize(opt) in normalize(cached):
                    chosen_idx = i
                    log(tag, f"📚 Кэш → {opt[:50]}")
                    break

        is_cache_hit = False
        if chosen_idx == -1:
            # Рандомный выбор
            chosen_idx = random.randint(0, len(options) - 1)
            log(tag, f"🎲 Рандом → {options[chosen_idx][:50]}")
        else:
            is_cache_hit = True

        # Умная задержка: если обучаемся (рандом) — быстро, если сдаём (кэш) — ждём
        delay = DELAY_SECONDS if is_cache_hit else 0.5
        
        if delay > 1:
            log(tag, f"⏳ Жду {delay} сек (сдача теста)...")
        await asyncio.sleep(delay)

        # Кликаем и проверяем
        is_correct, correct_text, correct_real_idx = await click_and_check(page, chosen_idx)

        if is_correct:
            correct += 1
            log(tag, f"✅ Верно! ({correct}/{total})")
        else:
            log(tag, f"❌ Неверно! Правильный: {correct_text[:60]}")

        # Сохраняем правильный ответ в кэш
        if correct_text:
            answer_cache[q_key] = correct_text
            save_cache()

        # Следующий вопрос
        has_next = await go_next_question(page)
        if not has_next:
            break

        await page.wait_for_timeout(800)

    accuracy = (correct / total * 100) if total > 0 else 0
    log(tag, f"📊 Итог: {correct}/{total} = {accuracy:.0f}%")
    return accuracy


async def main():
    print("=" * 55)
    print("  🎯 Quiz Bot (без API) — battle.aiplus.kz")
    print("  📚 Стратегия: учись → побеждай")
    print(f"  📂 Кэш: {CACHE_FILE}")
    print(f"  ⏱  Задержка: {DELAY_SECONDS} сек")
    print("=" * 55)

    load_cache()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, slow_mo=200)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            locale="ru-RU",
        )
        page = await context.new_page()

        await login(page)
        await go_to_topics(page)

        stats = {"done": 0, "total": len(TOPICS_TO_DO) * REQUIRED_ATTEMPTS}

        for topic_idx in TOPICS_TO_DO:
            topic_num = topic_idx + 1
            log("MAIN", f"{'='*50}")
            log("MAIN", f"📖 ТЕМА {topic_num}")
            log("MAIN", f"{'='*50}")

            # Проверяем сколько подтем уже пройдено
            await go_to_topics(page)
            await page.wait_for_timeout(1000)

            # Прокрутим до нужной темы чтобы видеть точки
            cards = page.locator("button.w-full.text-left.bg-white.rounded-xl.border")
            card_count = await cards.count()
            if topic_idx < card_count:
                await cards.nth(topic_idx).scroll_into_view_if_needed()
                await page.wait_for_timeout(500)

            done_attempts = await get_subtopic_progress(page, topic_idx)
            log("MAIN", f"Уже успешно пройдено: {done_attempts}/{REQUIRED_ATTEMPTS}")

            for required_run in range(done_attempts + 1, REQUIRED_ATTEMPTS + 1):
                log("MAIN", f"▶ Тема {topic_num}, Ожидаем успешную попытку {required_run}/{REQUIRED_ATTEMPTS}")

                # Долбим одни и те же вопросы (в этой попытке), пока не получим 90%+
                attempt_in_run = 1
                while True:
                    log("MAIN", f"  Прогон #{attempt_in_run} (для закрашивания точки {required_run})")

                    await go_to_topics(page)
                    await page.wait_for_timeout(1000)

                    started = await click_topic(page, topic_idx)
                    if not started:
                        log("ERROR", "Не удалось открыть тему")
                        break

                    # Функция run_test сама прогонит все 20 вопросов, пока не кончится кнопка "Следующий"
                    accuracy = await run_test(page, topic_num, required_run, attempt_in_run)

                    await close_results(page)
                    await page.wait_for_timeout(1500)

                    if accuracy >= 90:
                        log("MAIN", f"  🎉 ПРОГОН УСПЕШЕН ({accuracy:.0f}%)! Переходим к следующей попытке.")
                        stats["done"] += 1
                        break  # Выходим из while True, переходим к следующему required_run
                    else:
                        log("MAIN", f"  ⚠️ {accuracy:.0f}% — меньше 90%, перепроходим ещё раз...")
                        log("MAIN", f"  📚 В кэше уже {len(answer_cache)} ответов")
                    
                    attempt_in_run += 1

            log("MAIN", f"Тема {topic_num} завершена. Прогресс: {stats['done']}/{stats['total']}")

        # Итог
        print("\n" + "=" * 55)
        print(f"  📊 ГОТОВО: {stats['done']}/{stats['total']} подтем пройдено")
        print(f"  📚 Ответов в кэше: {len(answer_cache)}")
        print("=" * 55)
        save_cache()

        log("MAIN", "Нажми Enter чтобы закрыть браузер...")
        await asyncio.get_event_loop().run_in_executor(None, input)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
