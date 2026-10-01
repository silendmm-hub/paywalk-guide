#!/bin/bash
# Заполняет .env по вопросам. Enter без ответа оставляет текущее значение.
set -e
cd "$(dirname "$0")"
[ -f .env ] || cp .env.example .env

get() { grep -E "^$1=" .env | head -1 | cut -d= -f2-; }
put() { local v="${2//\\/\\\\}"; v="${v//|/\\|}"; v="${v//&/\\&}"; sed -i "s|^$1=.*|$1=$v|" .env; }
ask() {
  local cur; cur=$(get "$1")
  local show="$cur"; [ ${#show} -gt 20 ] && show="${show:0:12}…"
  read -rp "$2 [${show:-пусто}]: " v
  [ -n "$v" ] && put "$1" "$v"
  return 0
}

echo "== Telegram"
ask BOT_TOKEN "Токен бота от @BotFather"
ask CLUB_CHAT_ID "id группы клуба (начинается с -100)"
ask ADMIN_IDS "Твой Telegram id"
ask MANAGER_USERNAME "Ник менеджера без @"
echo "== Тарифы (месяцев:цена через запятую, для теста 1:10)"
ask PLANS "Тарифы"
echo "== Точка"
ask TOCHKA_JWT "JWT-ключ Точки"
ask TOCHKA_CLIENT_ID "client_id Точки"
ask TOCHKA_CUSTOMER_CODE "customerCode (Enter, если пока не знаешь)"
ask TAX_SYSTEM "Налоги: usn_income / usn_income_outcome / osn / patent"

if [ -z "$(get DOMAIN)" ]; then
  ip=$(curl -s4 --max-time 10 https://ifconfig.me || true)
  if [ -n "$ip" ]; then put DOMAIN "${ip//./-}.sslip.io"; fi
fi
[ -n "$(get DOMAIN)" ] && [ -z "$(get PUBLIC_URL)" ] && put PUBLIC_URL "https://$(get DOMAIN)"
[ -z "$(get WEBHOOK_SECRET)" ] && put WEBHOOK_SECRET "$(openssl rand -hex 16)"
chmod 600 .env
echo
echo "Готово. Адрес бота: $(get PUBLIC_URL)"
