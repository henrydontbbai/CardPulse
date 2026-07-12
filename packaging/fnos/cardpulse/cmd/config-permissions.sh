#!/bin/sh

cardpulse_config_directory_fail() {
    printf '%s\n' "CardPulse rejected an unsafe configuration directory." >&2
    return 1
}

cardpulse_prepare_config_directory() {
    package_dir=${TRIM_PKGVAR:?TRIM_PKGVAR is required}
    config_dir="$package_dir/config"

    if [ -L "$package_dir" ] || [ ! -d "$package_dir" ] || [ -L "$config_dir" ]; then
        cardpulse_config_directory_fail
        return 1
    fi
    if [ ! -e "$config_dir" ]; then
        mkdir "$config_dir" || return 1
    fi
    if [ -L "$config_dir" ] || [ ! -d "$config_dir" ]; then
        cardpulse_config_directory_fail
        return 1
    fi

    lifecycle_uid=$(id -u) || return 1
    package_uid=$(stat -c '%u' "$package_dir") || return 1
    package_gid=$(stat -c '%g' "$package_dir") || return 1
    config_uid=$(stat -c '%u' "$config_dir") || return 1
    config_gid=$(stat -c '%g' "$config_dir") || return 1
    if [ "$package_uid" != "$lifecycle_uid" ] || [ "$config_uid" != "$package_uid" ] || \
        [ "$config_gid" != "$package_gid" ]; then
        cardpulse_config_directory_fail
        return 1
    fi

    chmod 2750 "$config_dir" || return 1
    config_mode=$(stat -c '%a' "$config_dir") || return 1
    if [ "$config_mode" != "2750" ]; then
        cardpulse_config_directory_fail
        return 1
    fi
}
