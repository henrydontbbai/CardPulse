#!/bin/bash
# ============================================================================
# CardPulse - 通知推送模块
# 支持 7 种主流通讯软件通知
# ============================================================================

# 防止重复加载
[[ -n "${_NOTIFIER_LOADED:-}" ]] && return 0
_NOTIFIER_LOADED=1

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
    
    # 检查是否启用通知
    local enabled=$(config_read ".notify.enabled" "false")
    if [[ "$enabled" != "true" ]]; then
        return 0
    fi
    
    # 构建通知消息
    local status_text="成功"
    if [[ "$status" != "success" ]]; then
        status_text="失败"
    fi
    
    local notify_msg="【CardPulse】保号短信${status_text}\n设备: ${device}\n号码: ${phone}\n时间: $(date '+%Y-%m-%d %H:%M:%S')"
    
    # 发送各渠道通知
    notify_telegram "$notify_msg"
    notify_wechat "$notify_msg"
    notify_wecom "$notify_msg"
    notify_qq "$notify_msg"
    notify_feishu "$notify_msg"
    notify_dingtalk "$notify_msg"
    notify_bark "$notify_msg"
    notify_email "$status_text" "$device" "$phone" "$message"
    
    return 0
}

# ============================================================================
# Telegram 通知
# ============================================================================

# 发送 Telegram 通知
# 用法: notify_telegram "消息内容"
notify_telegram() {
    local msg="$1"
    local enabled=$(config_read ".notify.telegram.enabled" "false")
    
    if [[ "$enabled" != "true" ]]; then
        return 0
    fi
    
    local bot_token=$(config_read ".notify.telegram.bot_token" "")
    local chat_id=$(config_read ".notify.telegram.chat_id" "")
    
    if [[ -z "$bot_token" || -z "$chat_id" ]]; then
        return 1
    fi
    
    # 发送消息
    local response=$(curl -s -X POST \
        "https://api.telegram.org/bot${bot_token}/sendMessage" \
        -d "chat_id=${chat_id}" \
        -d "text=${msg}" \
        -d "parse_mode=HTML" \
        --connect-timeout 10 \
        --max-time 30)
    
    if echo "$response" | grep -q '"ok":true'; then
        return 0
    else
        echo "[WARN] Telegram 通知发送失败" >&2
        return 1
    fi
}

# ============================================================================
# 微信通知（Server酱）
# ============================================================================

# 发送微信通知（Server酱）
# 用法: notify_wechat "消息内容"
notify_wechat() {
    local msg="$1"
    local enabled=$(config_read ".notify.wechat.enabled" "false")
    
    if [[ "$enabled" != "true" ]]; then
        return 0
    fi
    
    local send_key=$(config_read ".notify.wechat.send_key" "")
    
    if [[ -z "$send_key" ]]; then
        return 1
    fi
    
    # 发送消息
    local response=$(curl -s -X POST \
        "https://sctapi.ftqq.com/${send_key}.send" \
        -d "title=CardPulse 保号通知" \
        -d "desp=${msg}" \
        --connect-timeout 10 \
        --max-time 30)
    
    if echo "$response" | grep -q '"code":0'; then
        return 0
    else
        echo "[WARN] 微信通知发送失败" >&2
        return 1
    fi
}

# ============================================================================
# 企业微信机器人通知
# ============================================================================

# 发送企业微信机器人通知
# 用法: notify_wecom "消息内容"
notify_wecom() {
    local msg="$1"
    local enabled=$(config_read ".notify.wecom.enabled" "false")
    
    if [[ "$enabled" != "true" ]]; then
        return 0
    fi
    
    local webhook_url=$(config_read ".notify.wecom.webhook_url" "")
    
    if [[ -z "$webhook_url" ]]; then
        return 1
    fi
    
    # 发送消息
    local response=$(curl -s -X POST \
        "$webhook_url" \
        -H "Content-Type: application/json" \
        -d "{\"msgtype\":\"text\",\"text\":{\"content\":\"${msg}\"}}" \
        --connect-timeout 10 \
        --max-time 30)
    
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

# 发送 QQ 通知（Qmsg）
# 用法: notify_qq "消息内容"
notify_qq() {
    local msg="$1"
    local enabled=$(config_read ".notify.qq.enabled" "false")
    
    if [[ "$enabled" != "true" ]]; then
        return 0
    fi
    
    local qmsg_key=$(config_read ".notify.qq.qmsg_key" "")
    
    if [[ -z "$qmsg_key" ]]; then
        return 1
    fi
    
    # 发送消息
    local response=$(curl -s -X POST \
        "https://qmsg.zendee.cn/send/${qmsg_key}" \
        -d "msg=${msg}" \
        --connect-timeout 10 \
        --max-time 30)
    
    if echo "$response" | grep -q '"success":true'; then
        return 0
    else
        echo "[WARN] QQ 通知发送失败" >&2
        return 1
    fi
}

# ============================================================================
# 飞书通知
# ============================================================================

# 发送飞书通知
# 用法: notify_feishu "消息内容"
notify_feishu() {
    local msg="$1"
    local enabled=$(config_read ".notify.feishu.enabled" "false")
    
    if [[ "$enabled" != "true" ]]; then
        return 0
    fi
    
    local webhook_url=$(config_read ".notify.feishu.webhook_url" "")
    
    if [[ -z "$webhook_url" ]]; then
        return 1
    fi
    
    # 发送消息
    local response=$(curl -s -X POST \
        "$webhook_url" \
        -H "Content-Type: application/json" \
        -d "{\"msg_type\":\"text\",\"content\":{\"text\":\"${msg}\"}}" \
        --connect-timeout 10 \
        --max-time 30)
    
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

# 发送钉钉通知
# 用法: notify_dingtalk "消息内容"
notify_dingtalk() {
    local msg="$1"
    local enabled=$(config_read ".notify.dingtalk.enabled" "false")
    
    if [[ "$enabled" != "true" ]]; then
        return 0
    fi
    
    local webhook_url=$(config_read ".notify.dingtalk.webhook_url" "")
    local secret=$(config_read ".notify.dingtalk.secret" "")
    
    if [[ -z "$webhook_url" ]]; then
        return 1
    fi
    
    # 如果配置了密钥，添加签名
    local full_url="$webhook_url"
    if [[ -n "$secret" ]]; then
        local timestamp=$(date +%s%3N)
        local sign=$(echo -ne "${timestamp}\n${secret}" | openssl dgst -sha256 -hmac "" -binary | base64)
        local sign_encoded=$(python3 -c "import urllib.parse; print(urllib.parse.quote_plus('$sign'))" 2>/dev/null)
        full_url="${webhook_url}&timestamp=${timestamp}&sign=${sign_encoded}"
    fi
    
    # 发送消息
    local response=$(curl -s -X POST \
        "$full_url" \
        -H "Content-Type: application/json" \
        -d "{\"msgtype\":\"text\",\"text\":{\"content\":\"${msg}\"}}" \
        --connect-timeout 10 \
        --max-time 30)
    
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

# 发送 Bark 通知
# 用法: notify_bark "消息内容"
notify_bark() {
    local msg="$1"
    local enabled=$(config_read ".notify.bark.enabled" "false")
    
    if [[ "$enabled" != "true" ]]; then
        return 0
    fi
    
    local bark_url=$(config_read ".notify.bark.url" "")
    
    if [[ -z "$bark_url" ]]; then
        return 1
    fi
    
    # 发送消息
    local response=$(curl -s -X POST \
        "${bark_url}" \
        -H "Content-Type: application/json" \
        -d "{\"title\":\"CardPulse\",\"body\":\"${msg}\",\"sound\":\"alert\"}" \
        --connect-timeout 10 \
        --max-time 30)
    
    if echo "$response" | grep -q '"code":200'; then
        return 0
    else
        echo "[WARN] Bark 通知发送失败" >&2
        return 1
    fi
}

# ============================================================================
# Email 通知
# ============================================================================

# 发送 Email 通知
# 用法: notify_email "成功" "设备ID" "+8613800138000" "Hello"
notify_email() {
    local status="$1"
    local device="$2"
    local phone="$3"
    local message="$4"
    
    local enabled=$(config_read ".notify.email.enabled" "false")
    if [[ "$enabled" != "true" ]]; then
        return 0
    fi
    
    local smtp_host=$(config_read ".notify.email.smtp_host" "")
    local smtp_port=$(config_read ".notify.email.smtp_port" "465")
    local username=$(config_read ".notify.email.username" "")
    local password=$(config_read ".notify.email.password" "")
    local from=$(config_read ".notify.email.from" "")
    local to=$(config_read ".notify.email.to" "")
    local use_ssl=$(config_read ".notify.email.use_ssl" "true")
    
    if [[ -z "$smtp_host" || -z "$username" || -z "$password" || -z "$to" ]]; then
        return 1
    fi
    
    # 构建邮件内容
    local subject="CardPulse 保号短信${status}"
    local body="设备: ${device}\n号码: ${phone}\n时间: $(date '+%Y-%m-%d %H:%M:%S')\n短信内容: ${message}"
    
    # 使用 curl 发送邮件（通过 SMTP）
    # 注意：这里使用简单的 SMTP 发送，生产环境建议使用 mailx 等工具
    local response=$(echo -e "Subject: ${subject}\nFrom: ${from}\nTo: ${to}\n\n${body}" | \
        curl -s --ssl-reqd \
        --url "smtps://${smtp_host}:${smtp_port}" \
        --user "${username}:${password}" \
        --mail-from "${from}" \
        --mail-rcpt "${to}" \
        -T - \
        --connect-timeout 10 \
        --max-time 30 2>&1)
    
    if [[ $? -eq 0 ]]; then
        return 0
    else
        echo "[WARN] Email 通知发送失败" >&2
        return 1
    fi
}

# 测试通知
# 用法: notify_test
notify_test() {
    echo "[INFO] 测试通知发送..." >&2
    
    local test_msg="【CardPulse】测试通知\n这是一条测试消息\n时间: $(date '+%Y-%m-%d %H:%M:%S')"
    
    notify_send "success" "测试设备" "+860000000000" "测试短信"
    
    echo "[INFO] 测试通知已发送" >&2
}
