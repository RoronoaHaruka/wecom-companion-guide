#!/usr/bin/env bash
# Copyright (c) 2026 Roronoa & Haruka · From Raincove ♡
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
#
# Point a WeCom self-built application at your mini-app page:
#   1. agent/set      → application home page
#   2. menu/create    → one bottom-menu button
#   3. message/send   → a textcard with an "open" button
#
# Reads placeholders from the environment (see .env.example):
#   WECOM_CORP_ID, WECOM_APP_SECRET, WECOM_AGENT_ID, WECOM_TARGET_USER_ID,
#   MINI_APP_PAGE_URL  (e.g. https://bot.example.com/mini?k=<MINI_APP_TOKEN>)
# Optional: MINI_APP_MENU_NAME (default 面板, at most 16 bytes), MINI_APP_CARD_TITLE.
#
# Usage:  set -a; . /etc/wecom-agent.env; set +a; bash code/mini_app_setup.sh

set -euo pipefail

: "${WECOM_CORP_ID:?set WECOM_CORP_ID}"
: "${WECOM_APP_SECRET:?set WECOM_APP_SECRET}"
: "${WECOM_AGENT_ID:?set WECOM_AGENT_ID}"
: "${WECOM_TARGET_USER_ID:?set WECOM_TARGET_USER_ID}"
: "${MINI_APP_PAGE_URL:?set MINI_APP_PAGE_URL}"
MENU_NAME="${MINI_APP_MENU_NAME:-面板}"
CARD_TITLE="${MINI_APP_CARD_TITLE:-面板}"
API="https://qyapi.weixin.qq.com/cgi-bin"

json_field() {  # json_field <key>  (reads JSON on stdin, prints the value or empty)
  python3 -c 'import json,sys; d=json.load(sys.stdin); v=d.get(sys.argv[1], ""); print(v if not isinstance(v,(dict,list)) else json.dumps(v))' "$1"
}

check() {  # check <label> <response-json>
  local code; code="$(printf '%s' "$2" | json_field errcode)"
  local msg;  msg="$(printf '%s' "$2" | json_field errmsg)"
  if [ "$code" = "0" ]; then
    echo "[ok]   $1"
  else
    echo "[fail] $1 → errcode=$code errmsg=$msg" >&2
    exit 1
  fi
}

# JSON-escape the URL once so quotes or backslashes cannot break the payloads.
PAGE_URL_JSON="$(printf '%s' "$MINI_APP_PAGE_URL" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')"

# 0. access_token (valid two hours; cache it in a long-running service)
TOKEN_RESP="$(curl -sS "$API/gettoken?corpid=$WECOM_CORP_ID&corpsecret=$WECOM_APP_SECRET")"
check "gettoken" "$TOKEN_RESP"
ACCESS_TOKEN="$(printf '%s' "$TOKEN_RESP" | json_field access_token)"

# 1. application home page
RESP="$(curl -sS -X POST "$API/agent/set?access_token=$ACCESS_TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"agentid\": $WECOM_AGENT_ID, \"home_url\": $PAGE_URL_JSON}")"
check "agent/set home_url" "$RESP"

# 2. bottom menu with a single view button
RESP="$(curl -sS -X POST "$API/menu/create?access_token=$ACCESS_TOKEN&agentid=$WECOM_AGENT_ID" \
  -H 'Content-Type: application/json' \
  -d "{\"button\": [{\"type\": \"view\", \"name\": \"$MENU_NAME\", \"url\": $PAGE_URL_JSON}]}")"
check "menu/create" "$RESP"

# 3. a textcard so the link also sits in the conversation
RESP="$(curl -sS -X POST "$API/message/send?access_token=$ACCESS_TOKEN" \
  -H 'Content-Type: application/json' \
  -d "{\"touser\": \"$WECOM_TARGET_USER_ID\", \"msgtype\": \"textcard\", \"agentid\": $WECOM_AGENT_ID,
       \"textcard\": {\"title\": \"$CARD_TITLE\", \"description\": \"点开就是。长按可以收进浮窗。\",
                      \"url\": $PAGE_URL_JSON, \"btntxt\": \"打开\"}}")"
check "message/send textcard" "$RESP"

echo "done. open the bot conversation in WeChat: the bottom menu and the card both point at the page."
