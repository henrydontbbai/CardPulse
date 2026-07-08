param(
    [string]$Distro = "Ubuntu-24.04",
    [string]$BusId = "1-4",
    [string]$HostBind = "0.0.0.0",
    [int]$Port = 8765,
    [switch]$AllowSms,
    [string]$SudoPassword = $env:CARDPULSE_WSL_SUDO_PASSWORD
)

$ErrorActionPreference = "Stop"

function Quote-Bash {
    param([string]$Value)
    return "'" + ($Value -replace "'", "'\''") + "'"
}

function Invoke-Wsl {
    param([string]$Command)
    & wsl.exe -d $Distro -- bash -lc $Command
    if ($LASTEXITCODE -ne 0) {
        throw "WSL command failed with exit code $LASTEXITCODE"
    }
}

function Invoke-Usbipd {
    param([string[]]$Arguments)
    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & usbipd.exe @Arguments 2>&1
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }

    $outputText = ($output | ForEach-Object { $_.ToString() }) -join "`n"
    $output | ForEach-Object { Write-Host $_.ToString() }
    return [pscustomobject]@{
        ExitCode = $exitCode
        Output = $outputText
    }
}

function Invoke-UsbipdBind {
    param([string]$TargetBusId)
    $listResult = Invoke-Usbipd -Arguments @("list")
    if ($listResult.ExitCode -ne 0) {
        throw "usbipd list failed with exit code $($listResult.ExitCode)"
    }

    $targetLine = $listResult.Output -split "`n" | Where-Object { $_ -match "^\s*$([regex]::Escape($TargetBusId))\s+" } | Select-Object -First 1
    if ($targetLine -match "\b(Attached|Shared)\b") {
        Write-Host "usbipd device $TargetBusId is already shared or attached; skipping bind."
        return
    }

    $result = Invoke-Usbipd -Arguments @("bind", "--busid", $TargetBusId)
    if ($result.ExitCode -ne 0 -and ($result.Output -notmatch "already|shared|attached|client|busy")) {
        throw "usbipd bind failed with exit code $($result.ExitCode). Run this script from an elevated PowerShell once, or run: usbipd bind --busid $TargetBusId"
    }
}

function Invoke-UsbipdAttach {
    param([string]$TargetBusId)
    $result = Invoke-Usbipd -Arguments @("attach", "--wsl", "--busid", $TargetBusId)
    if ($result.ExitCode -ne 0 -and ($result.Output -notmatch "already attached|already|attached|busy|client")) {
        throw "usbipd attach failed with exit code $($result.ExitCode)"
    }
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$repoRootQ = Quote-Bash $repoRoot
$repoWsl = (& wsl.exe -d $Distro -- bash -lc "wslpath -a $repoRootQ" | Select-Object -Last 1).Trim()
if (-not $repoWsl) {
    throw "Unable to resolve repository path inside WSL."
}

$wslHome = (& wsl.exe -d $Distro -- bash -lc 'printf "%s" "$HOME"' | Select-Object -Last 1).Trim()
if (-not $wslHome) {
    throw "Unable to resolve WSL home directory."
}

$configDir = "$wslHome/.cardpulse-dji/config"
$stateDir = "$wslHome/.cardpulse-dji/state"
$repoQ = Quote-Bash $repoWsl
$configDirQ = Quote-Bash $configDir
$stateDirQ = Quote-Bash $stateDir

Write-Host "[1/6] Keeping WSL distro alive: $Distro"
Start-Process -FilePath "wsl.exe" -ArgumentList @("-d", $Distro, "--", "bash", "-lc", "while true; do sleep 3600; done") -WindowStyle Hidden
Start-Sleep -Seconds 1

Write-Host "[2/6] Sharing and attaching DJI/Baiwang USB device via usbipd: $BusId"
Invoke-UsbipdBind -TargetBusId $BusId
Invoke-UsbipdAttach -TargetBusId $BusId

Write-Host "[3/6] Preparing stable CardPulse config in WSL: $configDir"
$configScript = @"
set -euo pipefail
mkdir -p $configDirQ $stateDirQ
if [ ! -f $configDirQ/config.yaml ]; then
  if [ -f /tmp/cardpulse-dji-test/config/config.yaml ]; then
    cp /tmp/cardpulse-dji-test/config/config.yaml $configDirQ/config.yaml
  else
    cat > $configDirQ/config.yaml <<'YAML'
serial:
  port: /dev/ttyUSB2
  baudrate: 115200
  auto_detect: false
sms:
  phone: +8613800138000
  message: CardPulse DJI test
  interval_days: 179
  timeout: 30
retry:
  max_attempts: 1
  interval: 1
notify:
  enabled: false
YAML
  fi
fi
chmod 600 $configDirQ/config.yaml
"@
Invoke-Wsl -Command $configScript

Write-Host "[4/6] Binding DJI/Baiwang module to Linux option serial driver"
if ($SudoPassword) {
    $sudoCmd = "printf '%s\n' $(Quote-Bash $SudoPassword) | sudo -S bash scripts/dji-qdc507-wsl-prepare.sh"
} else {
    $sudoCmd = "sudo -n bash scripts/dji-qdc507-wsl-prepare.sh"
}

try {
    Invoke-Wsl -Command "cd $repoQ && $sudoCmd"
} catch {
    if (-not $SudoPassword) {
        throw "WSL sudo needs a cached password. Run sudo once in Ubuntu, or re-run this script with -SudoPassword for local testing."
    }
    throw
}

Write-Host "[5/6] Starting CardPulse Web on http://127.0.0.1:$Port"
Invoke-Wsl -Command "pkill -f '[s]cripts/cardpulse-web.py' 2>/dev/null || true"
$allowSmsArg = if ($AllowSms) { " --allow-sms" } else { "" }
$webCommand = "cd $repoQ; exec env CARDPULSE_CONFIG_DIR=$configDirQ CARDPULSE_STATE_DIR=$stateDirQ python3 scripts/cardpulse-web.py --host $HostBind --port $Port$allowSmsArg >> /tmp/cardpulse-web.log 2>&1"
Start-Process -FilePath "wsl.exe" -ArgumentList @("-d", $Distro, "--", "bash", "-lc", $webCommand) -WindowStyle Hidden
Start-Sleep -Seconds 2

Write-Host "[6/6] Verifying Web and modem status"
$health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 10
$info = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/info" -TimeoutSec 35

$summary = [ordered]@{
    web_url = "http://127.0.0.1:$Port"
    sms_enabled = $health.sms_enabled
    sim = $info.sim
    signal = $info.signal
    operator = $info.operator
    imei = $info.imei
    config = "$configDir/config.yaml"
}

[pscustomobject]$summary | Format-List

if (-not $AllowSms) {
    Write-Host "SMS test remains disabled. Start with -AllowSms only when you intentionally want the guarded SMS test endpoint."
}
