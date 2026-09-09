# ЭГОИСТ — Telegram astrology bot

Полная рабочая версия MVP:
- бесплатный «Я»;
- платные блоки Люблю / Хочу / Могу / Делаю / Имею / Меняюсь;
- «Мой год» с точным солнечным возвращением;
- «Сейчас»;
- детская карта потенциала;
- Telegram Stars;
- скрытая команда /tech для владельца (нужен ADMIN_ID);
- Swiss Ephemeris + tropical zodiac + Placidus;
- whole-sign aspects with degree strength weighting;
- intercepted signs and house rulers calculated internally.

Railway variables:
BOT_TOKEN=...
ADMIN_ID=your Telegram numeric user id (optional)

Важно: текущая версия хранит профили и покупки в оперативной памяти. Для публичного запуска нужно добавить постоянную БД, чтобы данные и покупки переживали redeploy/restart.
