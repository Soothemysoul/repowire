# beads-mhph — Telegram _poll_loop: at-least-once (offset-advance до доставки теряет сообщения)

**Тип:** bug (фрагильность Telegram-канала) · **Repo:** repowire-fork
**Файл:** `repowire/telegram/bot.py` · **База ветки:** `main` (origin/main, содержит 8lzb fix 5cd5ff2)
**Гейт:** mhph должен лечь ДО redeploy leak-fix — иначе message-safe рестарт демона снова теряет сообщения пользователя в окне реконнекта.

## Корень

`_poll_loop` (bot.py:213-228) сдвигает getUpdates-offset **до** подтверждения доставки:

```python
async def _poll_loop(self) -> None:
    while not self._stopping:
        try:
            r = await self._http.get(
                f"{self._bot_path}/getUpdates",
                params={"offset": self._tg_offset, "timeout": 30},
                timeout=35,
            )
            for u in r.json().get("result", []):
                self._tg_offset = u["update_id"] + 1   # ← offset ACK до доставки
                await self._on_update(u)               # ← если упадёт, offset уже сдвинут
        except asyncio.CancelledError:
            break
        except Exception:
            logger.warning("Poll error", exc_info=True)
            await asyncio.sleep(5)
```

Любой транзиентный сбой `_on_update` (напр. потеря коннекта к демону в окне рестарта)
→ exception ломает for-loop, но offset уже = `update_id+1`. Telegram считает апдейт
подтверждённым (offset продвинут на следующем getUpdates) → **redelivery не будет**,
сообщение пользователя потеряно безвозвратно.

ПРОЯВЛЕНИЕ (диагноз devops, 2026-06-20, двойной рестарт демона 8lzb forward+rollback):
сообщения из Telegram терялись через раз — бот показывал доставку (✓✓), но до director
не доходило. Отдельная фрагильность от guard-регрессии (beads-8lzb) и дубль-регистрации.

## Фикс (минимальный — переставить две строки, at-least-once)

Сдвигать offset **только ПОСЛЕ** успешного `_on_update`:

```python
            for u in r.json().get("result", []):
                await self._on_update(u)               # доставить первым
                self._tg_offset = u["update_id"] + 1   # ACK offset только при успехе
```

Семантика: при сбое `_on_update` exception ломает for-loop ДО сдвига offset для
упавшего апдейта → следующий getUpdates запросит с тем же offset → Telegram
передоставит. Уже успешно обработанные апдейты в батче (offset сдвинут) НЕ
передоставляются; упавший + последующие в батче — передоставляются (at-least-once).

ИДЕМПОТЕНТНОСТЬ: при ретрае возможны дубли (апдейт частично обработан, потом упал).
Это допустимо по решению director: «лучше дубль, чем потеря». Дедуп НЕ требуется в
рамках mhph; при желании отметить как deferred-item.

`except`-структура НЕ меняется — она уже снаружи for-loop и корректно роняет батч на
сбое; sleep(5)+retry на том же (несдвинутом для упавшего) offset = redelivery.

## TDD (обязательно — сначала тесты, потом фикс)

Файл тестов telegram-бота (создать/дополнить, напр. `tests/test_telegram_bot.py`).
Изолировать `_poll_loop`: мокнуть `self._http.get` (возвращает фиксированный батч
updates), мокнуть `self._on_update`, контролировать одну итерацию (выставить
`self._stopping` после первого батча, либо side_effect, ломающий while).

1. **FAIL→redelivery:** `_on_update` бросает на апдейте `update_id=N` → после итерации
   `self._tg_offset` НЕ сдвинут за пределы N (остаётся <= N, т.е. ACK не выдан для N);
   следующий getUpdates вызывается с offset, который снова отдаёт апдейт N (redelivery).
   Это прямой репро потери сообщения.
2. **happy-path цел:** успешный `_on_update` на батче [N, N+1] → `self._tg_offset ==
   (N+1)+1`; `_on_update` вызван по разу на каждый, в порядке.
3. (опц.) **частичный батч:** [N ok, N+1 fail] → offset == N+1 (ACK только за N), N+1
   передоставится; N не передоставляется.

Прогон: `pytest tests/test_telegram_bot.py` (+ полный suite зелёный, ruff+ty clean).

## DoD

- [ ] Фикс в `_poll_loop` (offset-advance ПОСЛЕ `_on_update`).
- [ ] TDD-тесты 1+2 (мин.) зелёные; полный suite + ruff + ty clean.
- [ ] PR от `fix/telegram-poll-at-least-once` → base `main`; title/body English,
      упомянуть beads-mhph + репро + at-least-once семантику.

## Redeploy (НЕ входит в задачу worker'а)

После merge mhph director координирует ОДИН message-safe рестарт демона, который
задеплоит main целиком (8lzb guard-fix 5cd5ff2 + mhph harden) и вернёт leak-fix без
риска для канала. Verify ОБЯЗАТЕЛЬНО включает live telegram→director. Координирует
director + devops-head.
