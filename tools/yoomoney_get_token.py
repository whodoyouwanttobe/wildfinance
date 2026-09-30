"""
Получение OAuth-токена ЮMoney для автооплаты (запускается ОДИН раз на своём компьютере).

Перед запуском:
  1. Открой https://yoomoney.ru/myservices/new и зарегистрируй приложение:
       Название: WildFinance bot
       Адрес сайта: https://t.me/<имя_бота>
       Redirect URI: https://t.me/<имя_бота>   (любой https-адрес, он нужен только чтобы забрать код)
       Галочку «Проверять подлинность приложения (OAuth2 client_secret)» можно не ставить
  2. Скопируй client_id (и client_secret, если включил проверку).

Запуск:  python tools/yoomoney_get_token.py
Токен НИКОМУ не отправляй — впиши его только в .env на сервере (YOOMONEY_TOKEN=...).
Токен живёт 3 года; отозвать можно в настройках ЮMoney → «Приложения».
"""
import json
import urllib.parse
import urllib.request

BASE = "https://yoomoney.ru"
SCOPE = "account-info operation-history"


def post(url: str, data: dict, headers: dict | None = None):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers=headers or {}, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    return urllib.request.urlopen(req, timeout=30)


def main():
    client_id = input("client_id: ").strip()
    redirect_uri = input("Redirect URI (как указан в приложении): ").strip()
    client_secret = input("client_secret (Enter, если не включал): ").strip()

    # Ссылку открывает сам браузер: так авторизация идёт в твоей сессии ЮMoney
    auth_url = f"{BASE}/oauth/authorize?" + urllib.parse.urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": SCOPE,
    }, quote_via=urllib.parse.quote)
    print("\n1) Открой в браузере эту ссылку и нажми «Разрешить»:\n")
    print(auth_url)
    print("\n2) Браузер перейдёт на твой Redirect URI с ?code=... в адресе.")
    redirected = input("   Вставь сюда ПОЛНЫЙ адрес из браузера: ").strip()
    code = urllib.parse.parse_qs(urllib.parse.urlparse(redirected).query).get("code", [""])[0]
    if not code:
        raise SystemExit("В адресе нет параметра code — попробуй ещё раз.")

    data = {
        "code": code,
        "client_id": client_id,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }
    if client_secret:
        data["client_secret"] = client_secret
    token = json.loads(post(f"{BASE}/oauth/token", data).read()).get("access_token")
    if not token:
        raise SystemExit("ЮMoney не выдал токен (код одноразовый и живёт ~1 минуту) — повтори с шага 1.")

    info = json.loads(post(f"{BASE}/api/account-info", {},
                           {"Authorization": f"Bearer {token}"}).read())
    print("\n✅ Готово. Добавь в .env на сервере (/opt/wildfinance/.env):\n")
    print(f"YOOMONEY_WALLET={info.get('account', '<номер кошелька>')}")
    print(f"YOOMONEY_TOKEN={token}")
    print("\nЗатем на сервере: cd /opt/wildfinance && docker compose up -d")


if __name__ == "__main__":
    main()
