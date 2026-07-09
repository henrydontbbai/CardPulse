#!/bin/bash
# ============================================================================
# CardPulse - 通知推送模块
# 支持 7 种主流通讯软件通知
# ============================================================================

# 防止重复加载
[[ -n "${_NOTIFIER_LOADED:-}" ]] && return 0
_NOTIFIER_LOADED=1

# 安全调用 curl（避免敏感信息暴露于进程列表）
# 用法: _curl_secure "METHOD" "URL" [extra_args...]
_curl_secure() {
    local method="$1"
    local url="$2"
    shift 2

    if [[ "$url" != http://* && "$url" != https://* ]]; then
        echo "[WARN] curl URL 协议不受支持，仅允许 http/https" >&2
        return 1
    fi
    if [[ "$url" == *$'\n'* || "$url" == *$'\r'* || "$url" == *\"* || "$url" == *\\* ]]; then
        echo "[WARN] curl URL 包含非法字符，已拒绝请求" >&2
        return 1
    fi

    # 将 URL 写入私有临时目录传给 curl -K，避免出现在 ps aux 中。
    local tmpdir
    local tmpfile
    tmpdir=$(mktemp -d "${TMPDIR:-/tmp}/cardpulse_curl.XXXXXX") || return 1
    chmod 700 "$tmpdir" 2>/dev/null || true
    tmpfile="${tmpdir}/curl.conf"
    : > "$tmpfile"
    chmod 600 "$tmpfile" 2>/dev/null || true
    printf 'url = "%s"\n' "$url" > "$tmpfile"

    local rc
    local output
    if output=$(
        trap 'rm -rf "$tmpdir"' EXIT
        curl -s -X "$method" -K "$tmpfile" "$@" --connect-timeout 10 --max-time 30 2>/dev/null
    ); then
        rc=0
    else
        rc=$?
    fi

    rm -rf "$tmpdir"
    printf '%s' "$output"
    return "$rc"
}

# ============================================================================
# 通知入口
# ============================================================================

# 统一通知入口
# 用法: notify_send "success" "设备ID" "+8613800138000" "Hello"
notify_send() {
    local status="$1"
    local device="$2"
    local phone="$3"
    local message="$4"

    local enabled
    enabled=$(config_read ".notify.enabled" "false")
    if ! config_is_true "$enabled"; then
        return 0
    fi

    local status_text="成功"
    if [[ "$status" != "success" ]]; then
        status_text="失败"
    fi

    local phone_masked
    phone_masked=$(config_mask_string "$phone")
    local notify_msg
    notify_msg="【CardPulse】保号短信${status_text}\n设备: ${device}\n号码: ${phone_masked}\n时间: $(date '+%Y-%m-%d %H:%M:%S')"

    notify_telegram "$notify_msg" || true
    notify_wechat "$notify_msg" || true
    notify_wecom "$notify_msg" || true
    notify_qq "$notify_msg" || true
    notify_feishu "$notify_msg" || true
    notify_dingtalk "$notify_msg" || true
    notify_bark "$notify_msg" || true
    notify_email "$status_text" "$device" "$phone" "$message" || true

    return 0
}

# ============================================================================
# Telegram 通知
# ============================================================================

notify_telegram() {
    local msg="$1"
    local enabled
    enabled=$(config_read ".notify.telegram.enabled" "false")

    if ! config_is_true "$enabled"; then
        return 0
    fi

    local bot_token
    local chat_id
    bot_token=$(config_read ".notify.telegram.bot_token" "")
    chat_id=$(config_read ".notify.telegram.chat_id" "")

    if [[ -z "$bot_token" || -z "$chat_id" ]]; then
        return 1
    fi

    local api_url="https://api.telegram.org/bot${bot_token}/sendMessage"
    local response
    if ! response=$(_curl_secure "POST" "$api_url" \
        --data-urlencode "chat_id=${chat_id}" \
        --data-urlencode "text=${msg}" \
        --data-urlencode "parse_mode=HTML"); then
        echo "[WARN] Telegram 通知发送失败 (token: $(config_mask_string "$bot_token"))" >&2
        return 1
    fi

    if echo "$response" | grep -q '"ok":true'; then
        return 0
    else
        echo "[WARN] Telegram 通知发送失败 (token: $(config_mask_string "$bot_token"))" >&2
        return 1
    fi
}

# ============================================================================
# 微信通知（Server酱）
# ============================================================================

notify_wechat() {
    local msg="$1"
    local enabled
    enabled=$(config_read ".notify.wechat.enabled" "false")

    if ! config_is_true "$enabled"; then
        return 0
    fi

    local send_key
    send_key=$(config_read ".notify.wechat.send_key" "")

    if [[ -z "$send_key" ]]; then
        return 1
    fi

    local api_url="https://sctapi.ftqq.com/${send_key}.send"
    local response
    if ! response=$(_curl_secure "POST" "$api_url" \
        --data-urlencode "title=CardPulse 保号通知" \
        --data-urlencode "desp=${msg}"); then
        echo "[WARN] 微信通知发送失败 (key: $(config_mask_string "$send_key"))" >&2
        return 1
    fi

    if echo "$response" | grep -q '"code":0'; then
        return 0
    else
        echo "[WARN] 微信通知发送失败 (key: $(config_mask_string "$send_key"))" >&2
        return 1
    fi
}

# ============================================================================
# 企业微信机器人通知
# ============================================================================

notify_wecom() {
    local msg="$1"
    local enabled
    enabled=$(config_read ".notify.wecom.enabled" "false")

    if ! config_is_true "$enabled"; then
        return 0
    fi

    local webhook_url
    webhook_url=$(config_read ".notify.wecom.webhook_url" "")

    if [[ -z "$webhook_url" ]]; then
        return 1
    fi

    local json_payload
    json_payload=$(python3 -c "import json,sys; print(json.dumps({'msgtype':'text','text':{'content':sys.stdin.read()}}))" <<< "$msg" 2>/dev/null)
    if [[ -z "$json_payload" ]]; then
        echo "[WARN] 企业微信 JSON 编码失败" >&2
        return 1
    fi

    local response
    if ! response=$(_curl_secure "POST" "$webhook_url" \
        -H "Content-Type: application/json" \
        -d "$json_payload"); then
        echo "[WARN] 企业微信通知发送失败" >&2
        return 1
    fi

    if echo "$response" | grep -q '"errcode":0'; then
        return 0
    else
        echo "[WARN] 企业微信通知发送失败" >&2
        return 1
    fi
}

# ============================================================================
# QQ 通知（Qmsg）
# ============================================================================

notify_qq() {
    local msg="$1"
    local enabled
    enabled=$(config_read ".notify.qq.enabled" "false")

    if ! config_is_true "$enabled"; then
        return 0
    fi

    local qmsg_key
    qmsg_key=$(config_read ".notify.qq.qmsg_key" "")

    if [[ -z "$qmsg_key" ]]; then
        return 1
    fi

    local api_url="https://qmsg.zendee.cn/send/${qmsg_key}"
    local response
    if ! response=$(_curl_secure "POST" "$api_url" \
        --data-urlencode "msg=${msg}"); then
        echo "[WARN] QQ 通知发送失败 (key: $(config_mask_string "$qmsg_key"))" >&2
        return 1
    fi

    if echo "$response" | grep -q '"success":true'; then
        return 0
    else
        echo "[WARN] QQ 通知发送失败 (key: $(config_mask_string "$qmsg_key"))" >&2
        return 1
    fi
}

# ============================================================================
# 飞书通知
# ============================================================================

notify_feishu() {
    local msg="$1"
    local enabled
    enabled=$(config_read ".notify.feishu.enabled" "false")

    if ! config_is_true "$enabled"; then
        return 0
    fi

    local webhook_url
    webhook_url=$(config_read ".notify.feishu.webhook_url" "")

    if [[ -z "$webhook_url" ]]; then
        return 1
    fi

    local json_payload
    json_payload=$(python3 -c "import json,sys; print(json.dumps({'msg_type':'text','content':{'text':sys.stdin.read()}}))" <<< "$msg" 2>/dev/null)
    if [[ -z "$json_payload" ]]; then
        echo "[WARN] 飞书 JSON 编码失败" >&2
        return 1
    fi

    local response
    if ! response=$(_curl_secure "POST" "$webhook_url" \
        -H "Content-Type: application/json" \
        -d "$json_payload"); then
        echo "[WARN] 飞书通知发送失败" >&2
        return 1
    fi

    if echo "$response" | grep -q '"StatusCode":0'; then
        return 0
    else
        echo "[WARN] 飞书通知发送失败" >&2
        return 1
    fi
}

# ============================================================================
# 钉钉通知
# ============================================================================

notify_dingtalk() {
    local msg="$1"
    local enabled
    enabled=$(config_read ".notify.dingtalk.enabled" "false")

    if ! config_is_true "$enabled"; then
        return 0
    fi

    local webhook_url
    local secret
    webhook_url=$(config_read ".notify.dingtalk.webhook_url" "")
    secret=$(config_read ".notify.dingtalk.secret" "")

    if [[ -z "$webhook_url" ]]; then
        return 1
    fi

    local full_url="$webhook_url"
    if [[ -n "$secret" ]]; then
        local timestamp
        timestamp=$(date +%s%3N)
        local sign_encoded
        sign_encoded=$(printf '%s' "$secret" | DINGTALK_TIMESTAMP="$timestamp" python3 -c '
import base64
import hashlib
import hmac
import os
import sys
import urllib.parse

secret = sys.stdin.read()
timestamp = os.environ["DINGTALK_TIMESTAMP"]
string_to_sign = f"{timestamp}\n{secret}"
digest = hmac.new(secret.encode(), string_to_sign.encode(), hashlib.sha256).digest()
print(urllib.parse.quote_plus(base64.b64encode(digest).decode()))
' 2>/dev/null)
        if [[ -z "$sign_encoded" ]]; then
            echo "[WARN] 钉钉签名生成失败" >&2
            return 1
        fi
        local sep="&"
        [[ "$webhook_url" != *\?* ]] && sep="?"
        full_url="${webhook_url}${sep}timestamp=${timestamp}&sign=${sign_encoded}"
    fi

    local json_payload
    json_payload=$(python3 -c "import json,sys; print(json.dumps({'msgtype':'text','text':{'content':sys.stdin.read()}}))" <<< "$msg" 2>/dev/null)
    if [[ -z "$json_payload" ]]; then
        echo "[WARN] 钉钉 JSON 编码失败" >&2
        return 1
    fi

    local response
    if ! response=$(_curl_secure "POST" "$full_url" \
        -H "Content-Type: application/json" \
        -d "$json_payload"); then
        echo "[WARN] 钉钉通知发送失败" >&2
        return 1
    fi

    if echo "$response" | grep -q '"errcode":0'; then
        return 0
    else
        echo "[WARN] 钉钉通知发送失败" >&2
        return 1
    fi
}

# ============================================================================
# Bark 通知（iOS）
# ============================================================================

notify_bark() {
    local msg="$1"
    local enabled
    enabled=$(config_read ".notify.bark.enabled" "false")

    if ! config_is_true "$enabled"; then
        return 0
    fi

    local bark_url
    bark_url=$(config_read ".notify.bark.url" "")

    if [[ -z "$bark_url" ]]; then
        return 1
    fi

    local json_payload
    json_payload=$(python3 -c "import json,sys; print(json.dumps({'title':'CardPulse','body':sys.stdin.read(),'sound':'alert'}))" <<< "$msg" 2>/dev/null)
    if [[ -z "$json_payload" ]]; then
        echo "[WARN] Bark JSON 编码失败" >&2
        return 1
    fi

    local response
    if ! response=$(_curl_secure "POST" "$bark_url" \
        -H "Content-Type: application/json" \
        -d "$json_payload"); then
        echo "[WARN] Bark 通知发送失败" >&2
        return 1
    fi

    if echo "$response" | grep -qE '"code":200|"code":1'; then
        return 0
    else
        echo "[WARN] Bark 通知发送失败" >&2
        return 1
    fi
}

# ============================================================================
# Email 通知
# ============================================================================

notify_email() {
    local status="$1"
    local device="$2"
    local phone="$3"
    local message="$4"

    local enabled
    enabled=$(config_read ".notify.email.enabled" "false")
    if ! config_is_true "$enabled"; then
        return 0
    fi

    local smtp_host
    local smtp_port
    local username
    local password
    local from
    local to
    local use_ssl
    smtp_host=$(config_read ".notify.email.smtp_host" "")
    smtp_port=$(config_read ".notify.email.smtp_port" "465")
    username=$(config_read ".notify.email.username" "")
    password=$(config_read ".notify.email.password" "")
    from=$(config_read ".notify.email.from" "")
    to=$(config_read ".notify.email.to" "")
    use_ssl=$(config_read ".notify.email.use_ssl" "true")

    if [[ -z "$smtp_host" || -z "$username" || -z "$password" || -z "$to" ]]; then
        return 1
    fi

    [[ -z "$from" ]] && from="$username"

    local subject="CardPulse 保号短信${status}"
    local phone_masked
    phone_masked=$(config_mask_string "$phone")
    local body
    body="设备: ${device}\n号码: ${phone_masked}\n时间: $(date '+%Y-%m-%d %H:%M:%S')\n短信内容长度: ${#message} 字符"

    local netrc_dir
    local netrc_file
    netrc_dir=$(mktemp -d "${TMPDIR:-/tmp}/cardpulse_netrc.XXXXXX") || return 1
    chmod 700 "$netrc_dir" 2>/dev/null || true
    netrc_file="${netrc_dir}/netrc"
    : > "$netrc_file"
    chmod 600 "$netrc_file" 2>/dev/null || true
    printf 'machine %s login %s password %s\n' "$smtp_host" "$username" "$password" > "$netrc_file"

    local smtp_scheme="smtp"
    local ssl_args=()
    if config_is_true "$use_ssl"; then
        smtp_scheme="smtps"
        ssl_args+=(--ssl-reqd)
    fi
    local smtp_url="${smtp_scheme}://${smtp_host}:${smtp_port}"
    local rc
    if (
        trap 'rm -rf "$netrc_dir"' EXIT
        echo -e "Subject: ${subject}\nFrom: ${from}\nTo: ${to}\n\n${body}" | \
        curl -s "${ssl_args[@]}" \
            --url "$smtp_url" \
            --netrc-file "$netrc_file" \
            --mail-from "${from}" \
            --mail-rcpt "${to}" \
            -T - \
            --connect-timeout 10 \
            --max-time 30 >/dev/null 2>&1
    ); then
        rc=0
    else
        rc=$?
    fi
    rm -rf "$netrc_dir"

    if [[ $rc -eq 0 ]]; then
        return 0
    else
        echo "[WARN] Email 通知发送失败" >&2
        return 1
    fi
}

# ============================================================================
# 测试通知
# ============================================================================

notify_test() {
    local channel="${1:-}"

    echo "[INFO] 测试通知发送..." >&2

    local test_msg
    test_msg="【CardPulse】测试通知\n这是一条测试消息\n时间: $(date '+%Y-%m-%d %H:%M:%S')"

    if [[ -n "$channel" ]]; then
        case "$channel" in
            telegram) notify_telegram "$test_msg" ;;
            wechat)   notify_wechat "$test_msg" ;;
            wecom)    notify_wecom "$test_msg" ;;
            qq)       notify_qq "$test_msg" ;;
            feishu)   notify_feishu "$test_msg" ;;
            dingtalk) notify_dingtalk "$test_msg" ;;
            bark)     notify_bark "$test_msg" ;;
            email)    notify_email "测试" "测试设备" "+860000000000" "测试短信" ;;
            *)
                echo "[ERROR] 未知通知渠道: $channel" >&2
                return 1
                ;;
        esac
        local rc=$?
        if [[ $rc -eq 0 ]]; then
            echo "[INFO] ✓ $channel 通知发送成功" >&2
        else
            echo "[ERROR] ✗ $channel 通知发送失败" >&2
        fi
        return "$rc"
    fi

    notify_send "success" "测试设备" "+860000000000" "测试短信"
    echo "[INFO] 测试通知已发送" >&2
}
