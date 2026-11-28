# Настройки сервера (Caddy) для SEO и скорости

Эти правки делаются на сервере, в `Caddyfile` — в репозитории его нет.
Ниже готовый блок; адаптируйте `root` под ваш путь к сайту.

## Что сейчас не так (проверено 28.11.2026)

| Проблема | Как сейчас | Что даст правка |
|---|---|---|
| Несуществующие адреса | `/любая-страница` отдаёт главную с кодом **200** — поисковики видят дубли | честный **404** со страницей `404.html` |
| Сжатие | главная (117 КБ) отдаётся без gzip/zstd | ~25–30 КБ по сети, быстрее первый экран |
| Кэш | у картинок и шрифтов нет `Cache-Control` | повторные визиты почти без загрузки |
| `www.wcshome.ru` | ошибка **502** | постоянный редирект на `wcshome.ru` |
| HSTS | нет | браузер сразу идёт по HTTPS |

## Блок для Caddyfile

```caddy
www.wcshome.ru {
	redir https://wcshome.ru{uri} permanent
}

wcshome.ru {
	root * /srv/wcshome        # ← ваш путь к файлам сайта
	encode zstd gzip

	header {
		Strict-Transport-Security "max-age=31536000"
		X-Content-Type-Options "nosniff"
		Referrer-Policy "strict-origin-when-cross-origin"
		-Server
	}

	# Данные, которые бот обновляет каждые 5 минут, — без кэша
	@live path /week.json /groups.json /events.json /events-meta.json /schedule-meta.json /subscribers.json
	header @live Cache-Control "no-cache"

	# HTML — всегда проверять свежесть (сайт меняется ботом)
	@html path / /index.html /404.html
	header @html Cache-Control "no-cache"

	# Шрифты, логотип, иконки — меняются редко
	@static path *.woff *.woff2 /logo.png /favicon* /apple-touch-icon.png /og-image.jpg
	header @static Cache-Control "public, max-age=2592000"

	# Картинки расписания и новостей: адрес меняется через ?v=..., поэтому можно кэшировать
	@images path /schedule.webp /schedule.jpg /news/*
	header @images Cache-Control "public, max-age=86400"

	file_server

	handle_errors {
		@404 expression {err.status_code} == 404
		handle @404 {
			rewrite * /404.html
			file_server
		}
	}
}
```

Важно: если сейчас в конфиге есть `try_files {path} /index.html` — его нужно убрать,
именно он отдаёт главную на любой адрес. Сайт одностраничный, но навигация
работает через якоря (`#schedule`), а не через адреса, так что это ничего не сломает.

Для DNS-записи `www` нужен A/AAAA (или CNAME) на тот же сервер, иначе Caddy не получит сертификат.

## Проверка после перезапуска

```bash
caddy validate --config /etc/caddy/Caddyfile && sudo systemctl reload caddy

curl -sI https://wcshome.ru/nonexistent | head -1                     # HTTP/2 404
curl -sI -H 'Accept-Encoding: gzip' https://wcshome.ru/ | grep -i content-encoding   # gzip
curl -sI https://www.wcshome.ru/ | grep -i -E '^HTTP|location'        # 308 → https://wcshome.ru/
curl -sI https://wcshome.ru/nauryz.woff | grep -i cache-control       # max-age=2592000
```
