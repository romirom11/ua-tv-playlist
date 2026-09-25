# UA TV playlist

Джерело для m3u-editor. GitHub Action двічі на день збирає потоки українських каналів з публічних плейлистів
(`sources.txt`), перевіряє їх ffprobe і генерує `playlist.m3u`:

- категорії, назви, номери, логотипи й tvg-id беруться з `channels.json`;
- на кожен канал 3 записи з однаковою назвою (`tvg-name` = `Назва`, `Назва #2`, `Назва #3`):
  перший — найкращий живий потік, решта — резервні (у m3u-editor вони звʼязані як failover);
- `status.json` — скільки живих потоків має кожен канал на момент останнього прогону.

Додати свій потік для каналу — `extra_streams.json`: `{"TET": ["http://..."]}` (ключ — поле `name` з `channels.json`).

Raw: `https://raw.githubusercontent.com/romirom11/ua-tv-playlist/main/playlist.m3u`
